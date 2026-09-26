"""Breaks caught: concurrent ownership, stale writes, skipped turns and lost input after death."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import subprocess
import sys
import threading
import unittest
from uuid import uuid4

from cloud.types import Actor, Submission, StaleLease
from cloud.work import Work
from cloud.leases import Leases
from cloud.recovery import Recovery
from tests.cloud.support import sandbox


def accept(db, cid=None, text='fixture'):
    actor, work = Actor('alice'), Work(db)
    cid = cid or work.create_conversation(actor, 'chat')
    return work.accept(actor, Submission(cid, 1, uuid4(), text))


def expire(db, job_id):
    with db.transaction() as tx:
        tx.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1s' WHERE id=%s", (job_id,))


def due(db, job_id):
    with db.transaction() as tx:
        tx.execute('UPDATE jobs SET due_at=clock_timestamp() WHERE id=%s', (job_id,))


class LeaseTests(unittest.TestCase):
    def test_late_attempt_cannot_commit(self):
        with sandbox() as db:
            job, leases = accept(db), Leases(db)
            old = leases.claim(job.job_id, 'worker-a')
            expire(db, job.job_id)
            Recovery(db).sweep()
            due(db, job.job_id)
            new = leases.claim(job.job_id, 'worker-b')
            self.assertIsNotNone(new)
            self.assertGreater(new.attempt, old.attempt)
            self.assertFalse(leases.heartbeat(old))
            self.assertFalse(leases.finish(old, 'stale result'))
            with self.assertRaises(StaleLease): leases.context(old)
            with self.assertRaises(StaleLease): leases.payload(old)
            self.assertTrue(leases.finish(new, 'current result'))
            self.assertFalse(leases.finish(new, 'duplicate result'))
            rows = db.read('SELECT outcome FROM attempts ORDER BY fence')
            self.assertEqual([r['outcome'] for r in rows], ['retry_pending', 'succeeded'])

    def test_concurrent_claim_only_one_wins(self):
        with sandbox() as db:
            job = accept(db)
            barrier = threading.Barrier(2)
            def claim(n):
                barrier.wait()
                return Leases(db).claim(job.job_id, f'worker-{n}')
            with ThreadPoolExecutor(2) as pool:
                claims = list(pool.map(claim, range(2)))
            self.assertEqual(sum(c is not None for c in claims), 1)
            self.assertEqual(db.read('SELECT attempts FROM jobs')[0]['attempts'], 1)

    def test_context_order_and_notice_survive_completion(self):
        with sandbox() as db:
            leases, work, actor = Leases(db), Work(db), Actor('alice')
            first = accept(db, text='first')
            second = accept(db, first.conversation_id, 'second')
            self.assertIsNone(leases.claim(second.job_id, 'early'))
            claim = leases.claim(first.job_id, 'first')
            self.assertEqual([m['content'] for m in leases.context(claim)], ['first'])
            work.notice(actor, first.conversation_id, 'event', 'notice')
            self.assertTrue(leases.finish(claim, 'reply'))
            following = leases.claim(second.job_id, 'second')
            self.assertEqual([m['content'] for m in leases.context(following)], ['first', 'second', 'notice', 'reply'])
            messages = work.snapshot(actor, first.conversation_id)['messages']
            self.assertEqual([m['content'] for m in messages], ['first', 'second', 'notice', 'reply'])
            self.assertEqual(db.read('SELECT count(*) AS n FROM outbox WHERE job_id=%s', (second.job_id,))[0]['n'], 2)

    def test_exhaustion_expiry_and_permanent_failure(self):
        with sandbox() as db:
            leases, job = Leases(db), accept(db)
            for n in range(3):
                due(db, job.job_id)
                claim = leases.claim(job.job_id, f'worker-{n}')
                self.assertIsNotNone(claim)
                leases.fail(claim, True, 'synthetic_failure')
            self.assertIsNone(leases.claim(job.job_id, 'fourth'))
            self.assertEqual(db.read('SELECT state,attempts FROM jobs WHERE id=%s', (job.job_id,))[0], {'state':'failed','attempts':3})
            expired = accept(db)
            with db.transaction() as tx: tx.execute("UPDATE jobs SET expires_at=clock_timestamp()-interval '1s' WHERE id=%s", (expired.job_id,))
            self.assertIsNone(leases.claim(expired.job_id, 'expired'))
            self.assertEqual(db.read('SELECT state FROM jobs WHERE id=%s', (expired.job_id,))[0]['state'], 'expired')
            permanent = accept(db)
            leases.fail(leases.claim(permanent.job_id, 'worker'), False, 'permanent_failure')
            self.assertEqual(db.read('SELECT state FROM jobs WHERE id=%s', (permanent.job_id,))[0]['state'], 'failed')

    def test_identity_deadline_and_disable_fences(self):
        with sandbox() as db:
            leases, job = Leases(db), accept(db)
            claim = leases.claim(job.job_id, 'worker')
            for forged in (replace(claim, owner_id='bob'), replace(claim, epoch=uuid4()),
                           replace(claim, generation=2), replace(claim, conversation_id=uuid4())):
                self.assertFalse(leases.heartbeat(forged))
                self.assertFalse(leases.finish(forged, 'forged'))
            self.assertTrue(leases.heartbeat(claim))
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            self.assertFalse(leases.heartbeat(claim))
            self.assertFalse(leases.finish(claim, 'disabled'))
            with db.transaction() as tx:
                tx.execute('UPDATE runtime SET enabled=true')
                tx.execute("UPDATE owners SET enabled=false WHERE owner_id='alice'")
            self.assertFalse(leases.heartbeat(claim))
            with db.transaction() as tx:
                tx.execute('UPDATE owners SET enabled=true')
                tx.execute("UPDATE jobs SET expires_at=clock_timestamp()-interval '1s'")
            self.assertFalse(leases.finish(claim, 'expired'))
            Recovery(db).sweep()
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'expired')

    def test_process_exit_after_claim_is_recoverable(self):
        with sandbox() as db:
            job = accept(db)
            code = '''import os,sys
from uuid import UUID
from cloud.database import Database
from cloud.leases import Leases
from tests.cloud.support import deny_remote
sys.addaudithook(deny_remote)
db=Database(os.environ['ATHENA_VERIFY_DSN'],sys.argv[1])
assert Leases(db).claim(UUID(sys.argv[2]),'crashed-process') is not None
os._exit(23)
'''
            child = subprocess.run([sys.executable, '-c', code, db.schema, str(job.job_id)], capture_output=True)
            self.assertEqual(child.returncode, 23, child.stderr.decode())
            expire(db, job.job_id)
            Recovery(db).sweep()
            due(db, job.job_id)
            recovered = Leases(db).claim(job.job_id, 'replacement')
            self.assertEqual(Leases(db).payload(recovered)['input']['text'], 'fixture')

    def test_attempt_wall_time_is_bounded(self):
        with sandbox() as db:
            leases, job = Leases(db), accept(db)
            claim = leases.claim(job.job_id, 'worker')
            with db.transaction() as tx:
                tx.execute("UPDATE attempts SET started_at=clock_timestamp()-interval '11 minutes'")
            self.assertFalse(leases.heartbeat(claim))
            Recovery(db).sweep()
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'retry_pending')

    def test_healthy_queue_cannot_starve_expiry_sweep(self):
        with sandbox() as db:
            with db.transaction() as tx:
                tx.execute("""INSERT INTO jobs(id,owner_id,kind,request_key,input_hash,input,epoch,expires_at,due_at)
                    SELECT gen_random_uuid(),'alice','gmail_generation',gen_random_uuid(),'fixture','{}',
                    (SELECT epoch FROM runtime),clock_timestamp()+interval '1 day',clock_timestamp()-interval '1 hour'
                    FROM generate_series(1,100)""")
            bob = Actor('bob')
            cid = Work(db).create_conversation(bob, 'chat')
            job = Work(db).accept(bob, Submission(cid, 1, uuid4(), 'expires'))
            with db.transaction() as tx:
                tx.execute("UPDATE jobs SET expires_at=clock_timestamp()-interval '1s' WHERE id=%s", (job.job_id,))
            Recovery(db).sweep()
            self.assertEqual(db.read('SELECT state FROM jobs WHERE id=%s', (job.job_id,))[0]['state'], 'expired')
