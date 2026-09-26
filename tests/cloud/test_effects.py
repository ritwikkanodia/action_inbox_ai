"""Synthetic intent only: no external write is performed by these tests."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from uuid import uuid4

from cloud.control import Control
from cloud.effects import Effects
from cloud.leases import Leases
from cloud.recovery import Recovery
from cloud.types import Actor, Conflict, NotFound, Rejected, StaleLease, Submission
from cloud.work import Work
from tests.cloud.support import sandbox
from tests.cloud.test_leases import accept, due, expire


class EffectTests(unittest.TestCase):
    def test_any_intent_prevents_whole_turn_retry(self):
        for outcome in (None, 'confirmed_succeeded', 'confirmed_no_effect', 'uncertain'):
            with self.subTest(outcome=outcome), sandbox() as db:
                actor, job, leases = Actor('alice'), accept(db), Leases(db)
                following = accept(db, job.conversation_id, 'later')
                claim, operation = leases.claim(job.job_id, 'worker'), uuid4()
                self.assertTrue(Effects(db).prepare(claim, operation, 'synthetic_write', 'fixture-hash'))
                if outcome: Effects(db).record(claim, operation, outcome, {'provider_id':'fixture'})
                expire(db, job.job_id)
                Recovery(db).sweep()
                due(db, job.job_id)
                self.assertIsNone(leases.claim(job.job_id, 'replacement'))
                self.assertIsNone(leases.claim(following.job_id, 'blocked'))
                self.assertTrue(Work(db).snapshot(actor, job.conversation_id)['reconciliation_hold'])
                self.assertEqual(Control(db).cancel(actor, job.job_id), 'needs_reconciliation')

    def test_duplicate_prepare_allows_one_dispatch(self):
        with sandbox() as db:
            job = accept(db)
            claim, operation = Leases(db).claim(job.job_id, 'worker'), uuid4()
            barrier = threading.Barrier(2)
            def prepare(_):
                barrier.wait()
                return Effects(db).prepare(claim, operation, 'synthetic_write', 'fixture')
            with ThreadPoolExecutor(2) as pool: results = list(pool.map(prepare, range(2)))
            self.assertEqual(sorted(results), [False, True])
            with self.assertRaises(Conflict): Effects(db).prepare(claim, operation, 'synthetic_write', 'changed')

    def test_cancel_before_and_after_prepare(self):
        with sandbox() as db:
            leases, actor = Leases(db), Actor('alice')
            queued = accept(db)
            self.assertEqual(Control(db).cancel(actor, queued.job_id), 'cancelled')
            self.assertIsNone(leases.claim(queued.job_id, 'late'))
            running = accept(db)
            claim = leases.claim(running.job_id, 'worker')
            self.assertEqual(Control(db).cancel(actor, running.job_id), 'cancelled')
            with self.assertRaises(StaleLease): Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture')
            possible = accept(db)
            claim = leases.claim(possible.job_id, 'worker')
            Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture')
            self.assertEqual(Control(db).cancel(actor, possible.job_id), 'needs_reconciliation')
            self.assertFalse(leases.finish(claim, 'stale'))

    def test_reset_keeps_hold_but_rejects_old_submission_and_reply(self):
        with sandbox() as db:
            actor, work, leases = Actor('alice'), Work(db), Leases(db)
            cid = work.create_conversation(actor, 'chat')
            submission = Submission(cid, 1, uuid4(), 'old text')
            job = work.accept(actor, submission)
            claim, operation = leases.claim(job.job_id, 'worker'), uuid4()
            Effects(db).prepare(claim, operation, 'synthetic_write', 'fixture')
            self.assertEqual(Control(db).reset(actor, cid, 1), 2)
            with self.assertRaises(Conflict): work.accept(actor, submission)
            self.assertFalse(leases.finish(claim, 'old reply'))
            Effects(db).record(claim, operation, 'confirmed_succeeded', {'status':'sent'})
            snapshot = work.snapshot(actor, cid)
            self.assertEqual(snapshot['messages'], [])
            self.assertTrue(snapshot['reconciliation_hold'])
            self.assertEqual(snapshot['outstanding'][0]['job_id'], job.job_id)
            self.assertEqual(db.read('SELECT count(*) AS n FROM effects')[0]['n'], 1)

    def test_reconcile_is_owner_scoped_audited_and_never_replays(self):
        with sandbox() as db:
            actor, job, leases = Actor('alice'), accept(db), Leases(db)
            claim = leases.claim(job.job_id, 'worker')
            Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture')
            Control(db).cancel(actor, job.job_id)
            later = accept(db, job.conversation_id, 'later')
            with self.assertRaises(NotFound): Control(db).reconcile(Actor('bob'), job.job_id, 'closed_without_retry', 'fixture review')
            with self.assertRaises(Rejected): Control(db).reconcile(actor, job.job_id, 'closed_without_retry', '')
            Control(db).reconcile(actor, job.job_id, 'closed_without_retry', 'fixture reviewed')
            self.assertFalse(Work(db).snapshot(actor, job.conversation_id)['reconciliation_hold'])
            self.assertIsNone(leases.claim(job.job_id, 'must-not-replay'))
            self.assertIsNotNone(leases.claim(later.job_id, 'next'))
            self.assertEqual(db.read('SELECT decision FROM reconciliations')[0]['decision'], 'closed_without_retry')

    def test_expiry_and_failure_after_intent_require_review(self):
        for action in ('expire', 'fail', 'finish_unknown'):
            with self.subTest(action=action), sandbox() as db:
                job, leases = accept(db), Leases(db)
                claim = leases.claim(job.job_id, 'worker')
                Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture')
                if action == 'expire':
                    with db.transaction() as tx: tx.execute("UPDATE jobs SET expires_at=clock_timestamp()-interval '1s'")
                    Recovery(db).sweep()
                elif action == 'fail': leases.fail(claim, True, 'synthetic_failure')
                else: self.assertFalse(leases.finish(claim, 'unverified outcome'))
                self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'needs_reconciliation')

    def test_receipts_are_bounded_and_late_evidence_preserved(self):
        with sandbox() as db:
            job = accept(db)
            claim, operation = Leases(db).claim(job.job_id, 'worker'), uuid4()
            Effects(db).prepare(claim, operation, 'synthetic_write', 'fixture')
            for receipt in ({'access_token':'never'}, {'status':{'body':'never'}}, {'provider_id':'x'*257}):
                with self.assertRaises(Rejected): Effects(db).record(claim, operation, 'uncertain', receipt)
            Effects(db).record(claim, operation, 'uncertain', {'status':'unknown'})
            Control(db).cancel(Actor('alice'), job.job_id)
            Effects(db).record(claim, operation, 'confirmed_succeeded', {'provider_id':'fixture'})
            self.assertEqual(db.read('SELECT count(*) AS n FROM effect_receipts')[0]['n'], 2)
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'needs_reconciliation')

    def test_uncertain_receipt_immediately_revokes_authority(self):
        with sandbox() as db:
            job, leases = accept(db), Leases(db)
            claim, operation = leases.claim(job.job_id, 'worker'), uuid4()
            Effects(db).prepare(claim, operation, 'synthetic_write', 'fixture')
            Effects(db).record(claim, operation, 'uncertain', {'status':'unknown'})
            self.assertFalse(leases.heartbeat(claim))
            with self.assertRaises(StaleLease): Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'another')
