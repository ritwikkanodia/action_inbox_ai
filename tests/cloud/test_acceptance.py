"""Breaks caught: duplicate acceptance, partial commits, cross-owner writes, lost notices."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from uuid import uuid4

import psycopg
from cloud.types import Actor, Submission, Conflict, NotFound, Rejected, Unavailable
from cloud.work import Work
from tests.cloud.support import sandbox


class AcceptanceTests(unittest.TestCase):
    def test_same_request_has_one_receipt(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            request = Submission(cid, 1, uuid4(), 'fixture request')
            one, two = work.accept(actor, request), work.accept(actor, request)
            self.assertEqual(one.job_id, two.job_id)
            self.assertFalse(one.replayed)
            self.assertTrue(two.replayed)
            self.assertEqual(len(work.snapshot(actor, cid)['messages']), 1)
            for text, suggestion in [('changed input', False), ('fixture request', True)]:
                with self.assertRaises(Conflict):
                    work.accept(actor, Submission(cid, 1, request.request_key, text, suggestion))

    def test_concurrent_duplicate(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            request = Submission(cid, 1, uuid4(), 'fixture')
            barrier = threading.Barrier(2)
            def submit(_):
                barrier.wait()
                return Work(db).accept(actor, request)
            with ThreadPoolExecutor(2) as pool:
                receipts = list(pool.map(submit, range(2)))
            self.assertEqual(receipts[0].job_id, receipts[1].job_id)
            self.assertEqual(sum(r.replayed for r in receipts), 1)
            for table in ('messages', 'jobs', 'outbox'):
                self.assertEqual(db.read(f'SELECT count(*) AS n FROM {table}')[0]['n'], 1)

    def test_owner_and_generation_boundaries(self):
        with sandbox() as db:
            work, alice, bob = Work(db), Actor('alice'), Actor('bob')
            cid = work.create_conversation(alice, 'chat')
            for call in (lambda: work.snapshot(bob, cid),
                         lambda: work.accept(bob, Submission(cid, 1, uuid4(), 'fixture')),
                         lambda: work.notice(bob, cid, 'notice', 'fixture')):
                with self.assertRaises(NotFound): call()
            with self.assertRaises(Conflict):
                work.accept(alice, Submission(cid, 2, uuid4(), 'fixture'))
            job = work.accept(alice, Submission(cid, 1, uuid4(), 'fixture'))
            with self.assertRaises(psycopg.IntegrityError), db.transaction() as tx:
                tx.execute('UPDATE jobs SET owner_id=%s WHERE id=%s', ('bob', job.job_id))
            with self.assertRaises(psycopg.IntegrityError), db.transaction() as tx:
                tx.execute('UPDATE messages SET owner_id=%s WHERE conversation_id=%s', ('bob', cid))

    def test_capacity_checks_happen_after_duplicate_lookup(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            first = Submission(cid, 1, uuid4(), 'fixture')
            work.accept(actor, first)
            for i in range(19): work.accept(actor, Submission(cid, 1, uuid4(), str(i)))
            self.assertTrue(work.accept(actor, first).replayed)
            with self.assertRaisesRegex(Rejected, 'conversation_capacity'):
                work.accept(actor, Submission(cid, 1, uuid4(), 'excess'))
            # Other conversations contribute to the owner-wide cap, too.
            for _ in range(4):
                another = work.create_conversation(actor, 'chat')
                for i in range(20): work.accept(actor, Submission(another, 1, uuid4(), str(i)))
            another = work.create_conversation(actor, 'chat')
            with self.assertRaisesRegex(Rejected, 'owner_capacity'):
                work.accept(actor, Submission(another, 1, uuid4(), 'excess'))

    def test_invalid_text_and_provenance(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            for text in ('', '  ', 'é' * 16385, None, 7):
                with self.assertRaises(Rejected):
                    work.accept(actor, Submission(cid, 1, uuid4(), text))
            with self.assertRaises(Rejected):
                work.accept(actor, Submission(cid, 1, uuid4(), 'fixture', 'true'))
            accepted = work.accept(actor, Submission(cid, 1, uuid4(), 'é' * 16384))
            self.assertEqual(accepted.state, 'queued')

    def test_outbox_abort_rolls_back_entire_acceptance(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            with db.transaction() as tx:
                tx.execute("""CREATE FUNCTION fail_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'fixture outbox crash'; END $$;
                    CREATE TRIGGER fixture BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION fail_outbox();""")
            with self.assertRaises(psycopg.Error):
                work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            snapshot = work.snapshot(actor, cid)
            self.assertEqual(snapshot['messages'], [])
            self.assertEqual(snapshot['jobs'], [])

    def test_notice_dedup_and_order(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            work.notice(actor, cid, 'source:m1', 'notice')
            work.notice(actor, cid, 'source:m1', 'notice')
            messages = work.snapshot(actor, cid)['messages']
            self.assertEqual([m['sequence'] for m in messages], [1, 2])
            self.assertEqual([m['content'] for m in messages], ['fixture', 'notice'])

    def test_disabled_runtime_or_owner_cannot_accept(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            for sql in ('UPDATE runtime SET enabled=false', "UPDATE owners SET enabled=false WHERE owner_id='alice'"):
                with db.transaction() as tx: tx.execute(sql)
                with self.assertRaises(Unavailable):
                    work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
                with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=true')

    def test_todo_conversations_require_owned_todo(self):
        with sandbox() as db:
            work, alice = Work(db), Actor('alice')
            tid = uuid4()
            with db.transaction() as tx:
                tx.execute("INSERT INTO todos(id,owner_id,title,source) VALUES (%s,'alice','fixture','gmail')", (tid,))
            self.assertIsNotNone(work.create_conversation(alice, 'todo', tid))
            with self.assertRaises(NotFound): work.create_conversation(Actor('bob'), 'todo', tid)
            with self.assertRaises(Rejected): work.create_conversation(alice, 'todo')
            with self.assertRaises(Rejected): work.create_conversation(alice, 'chat', tid)

    def test_database_rejects_null_chat_generation_and_order(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            job = work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            # Remove the referencing display row, so only the job's own CHECK can catch it.
            with db.transaction() as tx: tx.execute('DELETE FROM messages')
            for column in ('generation', 'job_order'):
                with self.subTest(column=column), self.assertRaises(psycopg.IntegrityError), db.transaction() as tx:
                    tx.execute(f'UPDATE jobs SET {column}=NULL WHERE id=%s', (job.job_id,))
