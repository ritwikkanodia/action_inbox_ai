"""Process death and local restore proof; never Azure disaster-recovery sign-off."""
from dataclasses import replace
import json
import os
import selectors
import subprocess
import sys
import threading
import time
import unittest
from uuid import uuid4

from cloud.authorization import Authorization
from cloud.config import DEFAULTS
from cloud.control import Control
from cloud.dispatch import Dispatcher, Receiver
from cloud.leases import Leases
from cloud.recovery import Recovery
from cloud.scheduler import Scheduler
from cloud.synthetic import SyntheticExecutor, SyntheticGenerator
from cloud.telemetry import emit, metrics
from cloud.types import Actor, Conflict, Rejected
from cloud.worker import Worker
from cloud.work import Work
from tests.cloud.support import sandbox, restored_copy
from tests.cloud.test_connections import connection
from tests.cloud.test_dispatch import envelope
from tests.cloud.test_gmail import page, generate_claim
from tests.cloud.test_leases import accept, due, expire

CHECKS = ('deletions_revocations','source_checkpoints','uncertain_effects','credential_invalidation')

CRASH = '''import os,signal,sys
from uuid import UUID,uuid4
from cloud.database import Database
from cloud.leases import Leases
from cloud.effects import Effects
from tests.cloud.support import deny_remote
sys.addaudithook(deny_remote)
db=Database(os.environ['ATHENA_VERIFY_DSN'],sys.argv[1])
job,stage=UUID(sys.argv[2]),sys.argv[3]
if stage!='before_publish':
    claim=Leases(db).claim(job,'crashing-worker')
    assert claim is not None
    if stage in ('after_intent','after_provider'):
        operation=uuid4()
        Effects(db).prepare(claim,operation,'synthetic_write','fixture')
        if stage=='after_provider':
            with db.transaction() as tx: tx.execute('INSERT INTO synthetic_provider_effects(id) VALUES (%s)',(operation,))
    elif stage=='after_completion': assert Leases(db).finish(claim,'committed reply')
print('READY',flush=True)
signal.pause()
'''


class RecoveryTests(unittest.TestCase):
    def test_restore_epoch_and_review_gate(self):
        with sandbox() as db:
            job, leases, recovery = accept(db), Leases(db), Recovery(db)
            old = leases.claim(job.job_id,'old-worker')
            old_envelope = envelope(db,job.job_id)
            cap = Authorization(db).issue(old,connection(db),frozenset({'gmail.read'}))
            epoch = recovery.begin_restore()
            self.assertNotEqual(epoch,old.epoch)
            self.assertFalse(leases.heartbeat(old))
            self.assertFalse(leases.finish(old,'late'))
            self.assertIsNone(leases.claim(job.job_id,'new-worker'))
            self.assertFalse(Authorization(db).validate(cap,'gmail.read'))
            with self.assertRaises(Conflict): recovery.resume_after_review(epoch,'synthetic-reviewer','fixture')
            with self.assertRaises(Rejected): recovery.review_check(epoch,'unknown','synthetic-reviewer','fixture')
            with self.assertRaises(Rejected): recovery.review_check(epoch,CHECKS[0],'','')
            with self.assertRaises(Conflict): recovery.review_check(uuid4(),CHECKS[0],'synthetic-reviewer','fixture')
            for check in CHECKS: recovery.review_check(epoch,check,'synthetic-reviewer','synthetic evidence only')
            recovery.resume_after_review(epoch,'synthetic-reviewer','synthetic evidence only')
            self.assertIsNone(Receiver(db).handle(old_envelope,'old-queue'))
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'],'needs_reconciliation')
            recovery.sweep()
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'],'needs_reconciliation')
            Control(db).reconcile(Actor('alice'),job.job_id,'closed_without_retry','synthetic reviewed')
            fresh = accept(db,job.conversation_id,'new request')
            self.assertIsNotNone(leases.claim(fresh.job_id,'fresh-worker'))
            self.assertFalse(leases.finish(old,'still late'))

    def test_real_process_kill_at_failure_boundaries(self):
        started = time.monotonic()
        for stage in ('before_publish','after_claim','after_intent','after_provider','after_completion'):
            with self.subTest(stage=stage), sandbox() as db:
                job = accept(db)
                with db.transaction() as tx: tx.execute('CREATE TABLE synthetic_provider_effects(id uuid PRIMARY KEY)')
                child = subprocess.Popen([sys.executable,'-c',CRASH,db.schema,str(job.job_id),stage],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(child.stdout,selectors.EVENT_READ)
                        self.assertTrue(selector.select(10),'child failed to reach crash boundary')
                        self.assertEqual(child.stdout.readline().strip(),'READY')
                    child.kill()
                    _, err = child.communicate(timeout=5)
                    self.assertLess(child.returncode,0,err)
                finally:
                    if child.poll() is None: child.kill(); child.communicate(timeout=5)
                expire(db,job.job_id)
                Recovery(db).sweep()
                if stage in ('after_intent','after_provider'):
                    due(db,job.job_id)
                    self.assertIsNone(Leases(db).claim(job.job_id,'must-not-replay'))
                    self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'],'needs_reconciliation')
                    self.assertEqual(db.read('SELECT count(*) AS n FROM synthetic_provider_effects')[0]['n'],int(stage=='after_provider'))
                elif stage=='after_completion':
                    self.assertIsNone(Receiver(db).handle(envelope(db,job.job_id),'redelivered'))
                    self.assertEqual(db.read("SELECT count(*) AS n FROM messages WHERE role='assistant'")[0]['n'],1)
                else:
                    due(db,job.job_id)
                    sent=[]
                    with db.transaction() as tx: tx.execute('UPDATE outbox SET due_at=clock_timestamp()')
                    Dispatcher(db,sent.append).once()
                    claim=Receiver(db).handle(sent[0],'replacement')
                    Worker(db,SyntheticExecutor(),SyntheticGenerator()).execute(claim)
                    self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'],'succeeded')
        print(f'local_crash_matrix_seconds={time.monotonic()-started:.3f}')

    def test_worker_generation_and_no_transaction_during_execution(self):
        with sandbox() as db:
            job=accept(db)
            class Executor:
                def run(self,claim,context):
                    with db.transaction() as tx:
                        tx.execute("SET LOCAL lock_timeout='100ms'")
                        tx.execute("SELECT * FROM owners WHERE owner_id='alice' FOR UPDATE")
                    return 'fixture reply'
            Worker(db,Executor(),SyntheticGenerator()).execute(Leases(db).claim(job.job_id,'worker'))
            generation=generate_claim(db)
            Worker(db,SyntheticExecutor(),SyntheticGenerator()).execute(generation)
            self.assertEqual(db.read('SELECT decision FROM generation_decisions')[0]['decision'],'skipped')

    def test_worker_loses_authority_at_attempt_deadline(self):
        with sandbox() as db:
            config=replace(DEFAULTS,heartbeat_seconds=.02,attempt_seconds=.1)
            stopped=threading.Event()
            class Blocked:
                def run(self,claim,context): stopped.wait(3); return 'late'
                def stop(self,claim): stopped.set()
            job=accept(db)
            claim=Leases(db,config).claim(job.job_id,'worker')
            Worker(db,Blocked(),SyntheticGenerator(),config=config).execute(claim)
            self.assertTrue(stopped.is_set())
            self.assertEqual(db.read("SELECT count(*) AS n FROM messages WHERE role='assistant'")[0]['n'],0)
            Recovery(db,config).sweep()
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'],'retry_pending')

    def test_scheduler_requires_explicit_provider(self):
        with sandbox() as db:
            connection(db)
            Scheduler(db).once()
            self.assertEqual(db.read('SELECT * FROM mailbox_checkpoints'),[])
            def poll(claim):
                with db.transaction() as tx: tx.execute('SELECT * FROM connections FOR UPDATE')
                return page()
            Scheduler(db,poll=poll).once()
            self.assertEqual(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'],'100')

    def test_safe_logs_and_metrics(self):
        with sandbox() as db:
            job=accept(db)
            with self.assertLogs('athena.cloud',level='INFO') as logs:
                emit('attempt_started',job_id=job.job_id,attempt=1,seconds=.2)
            record=json.loads(logs.records[0].message)
            self.assertEqual(set(record),{'event','job_id','attempt','seconds'})
            with self.assertRaises(ValueError): emit('attempt_started',prompt='synthetic secret')
            values=metrics(db)
            self.assertEqual(values['attempts'],0)
            self.assertEqual(values['reconciliation_holds'],0)
            self.assertGreaterEqual(values['oldest_queued_seconds'],0)
            self.assertNotIn('fixture',json.dumps(values))

    def test_local_dump_restore_keeps_execution_disabled(self):
        with sandbox() as db:
            job=accept(db)
            old=Leases(db).claim(job.job_id,'before-backup')
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            with restored_copy(db) as restored:
                self.assertFalse(restored.read('SELECT enabled FROM runtime')[0]['enabled'])
                recovery=Recovery(restored)
                epoch=recovery.begin_restore()
                self.assertFalse(Leases(restored).finish(old,'must not commit'))
                for check in CHECKS: recovery.review_check(epoch,check,'synthetic-reviewer','local rehearsal only')
                recovery.resume_after_review(epoch,'synthetic-reviewer','local rehearsal only')
                self.assertEqual(restored.read('SELECT state FROM jobs')[0]['state'],'needs_reconciliation')

    def test_cli_refuses_production_and_runs_explicit_local_roles(self):
        with sandbox() as db:
            refused=subprocess.run([sys.executable,'-m','cloud.cli','worker','--once'],capture_output=True,text=True)
            self.assertNotEqual(refused.returncode,0)
            self.assertIn('production_runner_not_integrated',refused.stderr)
            job=accept(db)
            for role in ('dispatcher','worker','scheduler'):
                result=subprocess.run([sys.executable,'-m','cloud.cli',role,'--synthetic','--schema',db.schema,'--once'],capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(db.read('SELECT state FROM jobs WHERE id=%s',(job.job_id,))[0]['state'],'succeeded')
