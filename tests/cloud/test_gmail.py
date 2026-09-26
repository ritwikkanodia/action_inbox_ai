"""Synthetic normalized pages against real SQL, not a live mailbox."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import unittest
from uuid import uuid4
import psycopg

from cloud.connections import Connections
from cloud.gmail import Gmail
from cloud.leases import Leases
from cloud.todos import Todos
from cloud.types import Actor, MailPage, Conflict, Rejected, StaleLease
from cloud.work import Work
from tests.cloud.support import sandbox
from tests.cloud.test_connections import connection
from tests.cloud.test_leases import accept, due


def event(mid='m1', kind='received', occurrence='h99'):
    return {'message_id':mid, 'event_type':kind, 'occurrence_id':occurrence,
            'thread_id':'t1', 'content':'synthetic mail'}


def page(mid='m1', **kwargs):
    kwargs.setdefault('baseline_cursor', '100')
    return MailPage('page-1', 'start', None, '100', (event(mid),), **kwargs)


def generate_claim(db, mid='m1'):
    cid = connection(db, account=mid+'@example.invalid')
    claim = Gmail(db).claim_poll(Actor('alice'), cid)
    Gmail(db).ingest_page(claim, page(mid))
    job = db.read('SELECT id FROM jobs WHERE connection_id=%s', (cid,))[0]['id']
    return Leases(db).claim(job, 'generator')


class GmailTests(unittest.TestCase):
    def test_arrivals_during_backfill_are_caught_up_from_baseline(self):
        with sandbox() as db:
            gmail, cid = Gmail(db), connection(db)
            claim = gmail.claim_poll(Actor('alice'), cid)
            gmail.ingest_page(claim, MailPage('first','start','last',None,(event('before'),),baseline_cursor='99'))
            gmail.ingest_page(claim, MailPage('last','last',None,'101',()))
            self.assertEqual(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'], '99')
            history = gmail.claim_poll(Actor('alice'), cid)
            gmail.ingest_page(history, MailPage('history','start',None,'101',(event('arrival-100'),event('arrival-101'))))
            self.assertEqual(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'], '101')
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'], 3)

    def test_single_page_backfill_also_requires_captured_baseline(self):
        with sandbox() as db:
            gmail, cid = Gmail(db), connection(db)
            claim = gmail.claim_poll(Actor('alice'), cid)
            with self.assertRaises(Rejected):
                gmail.ingest_page(claim, MailPage('one','start',None,'101',(event(),)))
            self.assertIsNone(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'])

    def test_duplicate_and_changed_pages(self):
        with sandbox() as db:
            gmail, actor, cid = Gmail(db), Actor('alice'), connection(db)
            claim = gmail.claim_poll(actor, cid)
            self.assertEqual(gmail.ingest_page(claim, page()), 1)
            self.assertEqual(gmail.ingest_page(claim, page()), 0)
            with self.assertRaises(Conflict): gmail.ingest_page(claim, page('different'))
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'], 1)
            # A new chain can use the same page names, and overlap still deduplicates.
            following = gmail.claim_poll(actor, cid)
            self.assertNotEqual(following.chain_id, claim.chain_id)
            self.assertEqual(gmail.ingest_page(following, page()), 0)

    def test_page_failure_rolls_back_jobs_events_and_cursor(self):
        with sandbox() as db:
            gmail, cid = Gmail(db), connection(db)
            claim = gmail.claim_poll(Actor('alice'), cid)
            with db.transaction() as tx:
                tx.execute("""CREATE FUNCTION fail_page() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'fixture crash'; END $$;
                    CREATE TRIGGER fixture BEFORE INSERT ON page_receipts FOR EACH ROW EXECUTE FUNCTION fail_page();""")
            with self.assertRaises(psycopg.Error): gmail.ingest_page(claim, page())
            for table in ('jobs', 'ingested_events', 'outbox'):
                self.assertEqual(db.read(f'SELECT count(*) AS n FROM {table}')[0]['n'], 0)
            self.assertIsNone(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'])

    def test_poll_fences_and_interleaved_pages(self):
        with sandbox() as db:
            gmail, cid, barrier = Gmail(db), connection(db), threading.Barrier(2)
            def claim(_):
                barrier.wait()
                return Gmail(db).claim_poll(Actor('alice'), cid)
            with ThreadPoolExecutor(2) as pool: claims = list(pool.map(claim, range(2)))
            live = next(c for c in claims if c)
            self.assertEqual(sum(c is not None for c in claims), 1)
            first = MailPage('page-1', 'start', 'next-2', None, (event(),), baseline_cursor='99')
            gmail.ingest_page(live, first)
            checkpoint = db.read('SELECT * FROM mailbox_checkpoints')[0]
            self.assertEqual(checkpoint['baseline_cursor'], '99')
            self.assertIsNone(checkpoint['cursor'])
            with self.assertRaises(Conflict):
                gmail.ingest_page(live, MailPage('page-3', 'wrong', None, '101', (event('m3'),)))
            checkpoint = db.read('SELECT * FROM mailbox_checkpoints')[0]
            self.assertTrue(checkpoint['resync_required'])
            self.assertIsNone(checkpoint['cursor'])
            self.assertEqual(checkpoint['next_page_key'], 'start')
            with self.assertRaises(StaleLease): gmail.ingest_page(live, replace(page('late'), page_key='late-new-page'))
            new = gmail.claim_poll(Actor('alice'), cid)
            self.assertNotEqual(new.chain_id, live.chain_id)

    def test_tenant_connection_and_label_occurrence_identities(self):
        with sandbox() as db:
            for owner, account in (('alice','one@example.invalid'), ('alice','two@example.invalid'), ('bob','one@example.invalid')):
                cid = connection(db, owner, account)
                claim = Gmail(db).claim_poll(Actor(owner), cid)
                mixed = replace(page(), events=(event(), event('m1','label_added','h1'), event('m1','label_added','h2')))
                self.assertEqual(Gmail(db).ingest_page(claim, mixed), 1)
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'], 3)
            self.assertEqual(db.read('SELECT count(*) AS n FROM ingested_events')[0]['n'], 9)

    def test_revocation_and_poll_lease_expiry_reject_new_pages(self):
        with sandbox() as db:
            cid, gmail = connection(db), Gmail(db)
            old = gmail.claim_poll(Actor('alice'), cid)
            with db.transaction() as tx: tx.execute("UPDATE mailbox_checkpoints SET lease_until=clock_timestamp()-interval '1s'")
            new = gmail.claim_poll(Actor('alice'), cid)
            with self.assertRaises(StaleLease): gmail.ingest_page(old, page())
            Connections(db).disconnect(Actor('alice'), cid)
            with self.assertRaises(StaleLease): gmail.ingest_page(new, page())

    def test_oversized_page_records_error_without_cursor_progress(self):
        with sandbox() as db:
            cid, gmail = connection(db), Gmail(db)
            claim = gmail.claim_poll(Actor('alice'), cid)
            bad = replace(page(), events=(dict(event(), content='x'*32769),))
            with self.assertRaises(Rejected): gmail.ingest_page(claim, bad)
            checkpoint = db.read('SELECT * FROM mailbox_checkpoints')[0]
            self.assertEqual(checkpoint['ingestion_error'], 'invalid_page_payload')
            self.assertIsNone(checkpoint['cursor'])
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'], 0)

    def test_failed_generation_remains_recoverable_after_cursor_advance(self):
        with sandbox() as db:
            claim = generate_claim(db)
            Leases(db).fail(claim, True, 'synthetic_failure')
            self.assertEqual(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'], '100')
            due(db, claim.job_id)
            retry = Leases(db).claim(claim.job_id, 'replacement')
            self.assertEqual(Leases(db).payload(retry)['input']['message_id'], 'm1')
            self.assertIsNone(Todos(db).record_generation(retry, 'skipped', None))
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'succeeded')

    def test_todo_decision_notice_and_retry_are_atomic(self):
        with sandbox() as db:
            chat = accept(db)
            running = Leases(db).claim(chat.job_id, 'chat')
            claim = generate_claim(db)
            todo = {'title':'Review synthetic launch notes','importance':'high','status':'open'}
            tid = Todos(db).record_generation(claim, 'created', todo)
            self.assertEqual(Todos(db).record_generation(claim, 'created', todo), tid)
            with self.assertRaises(Conflict): Todos(db).record_generation(claim, 'skipped', None)
            self.assertTrue(Leases(db).finish(running, 'reply'))
            messages = Work(db).snapshot(Actor('alice'), chat.conversation_id)['messages']
            self.assertEqual(len([m for m in messages if m['role']=='notice']), 1)
            self.assertEqual(len(Todos(db).list(Actor('alice'))), 1)
            self.assertEqual(Todos(db).list(Actor('bob')), [])

    def test_noise_near_duplicates_and_invalid_output(self):
        with sandbox() as db:
            first = generate_claim(db, 'first')
            Todos(db).record_generation(first, 'created', {'title':'Upload synthetic KYC documents'})
            second = generate_claim(db, 'second')
            self.assertIsNone(Todos(db).record_generation(second, 'created', {'title':'Upload KYC documents for synthetic order'}))
            third = generate_claim(db, 'third')
            self.assertIsNone(Todos(db).record_generation(third, 'created', {'title':'Sign in to synthetic account'}))
            fourth = generate_claim(db, 'fourth')
            for value in ({'title':''}, {'title':'Fixture','importance':'urgent'}, {'title':'Fixture','source':'whatsapp'}):
                with self.assertRaises(Rejected): Todos(db).record_generation(fourth, 'created', value)
            self.assertEqual(len(Todos(db).list(Actor('alice'))), 1)
            self.assertEqual([r['decision'] for r in db.read('SELECT decision FROM generation_decisions ORDER BY created_at')], ['created','duplicate','skipped'])

    def test_conflicting_baseline_or_self_loop_requires_resync(self):
        for invalid in ('baseline', 'self_loop'):
            with self.subTest(invalid=invalid), sandbox() as db:
                gmail, cid = Gmail(db), connection(db)
                claim = gmail.claim_poll(Actor('alice'), cid)
                gmail.ingest_page(claim, MailPage('one','start','two',None,(event(),),baseline_cursor='99'))
                bad = (MailPage('two','two',None,'100',(event('m2'),),baseline_cursor='98') if invalid=='baseline'
                       else MailPage('two','two','two',None,(event('m2'),),baseline_cursor='99'))
                with self.assertRaises(Conflict): gmail.ingest_page(claim, bad)
                self.assertIsNone(db.read('SELECT cursor FROM mailbox_checkpoints')[0]['cursor'])
                self.assertTrue(db.read('SELECT resync_required FROM mailbox_checkpoints')[0]['resync_required'])

    def test_notice_failure_rolls_back_generation_decision(self):
        with sandbox() as db:
            claim = generate_claim(db)
            with db.transaction() as tx:
                tx.execute("""CREATE FUNCTION fail_notice() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'fixture notice crash'; END $$;
                    CREATE TRIGGER fixture BEFORE INSERT ON messages FOR EACH ROW EXECUTE FUNCTION fail_notice();""")
            with self.assertRaises(psycopg.Error): Todos(db).record_generation(claim,'created',{'title':'Review synthetic budget'})
            self.assertEqual(db.read('SELECT count(*) AS n FROM todos')[0]['n'], 0)
            self.assertEqual(db.read('SELECT count(*) AS n FROM generation_decisions')[0]['n'], 0)
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'running')
