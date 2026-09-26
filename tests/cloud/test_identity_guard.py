"""Session revocation is serialized with the protected transaction."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
import unittest
from uuid import uuid4
from cloud.control import Control
from cloud.identity.sessions import Sessions
from cloud.identity.types import AuthenticationRequired
from cloud.recovery import Recovery
from cloud.types import Actor, NotFound, Submission, Unavailable
from cloud.work import Work
from tests.cloud.identity_support import admit_fixture
from tests.cloud.support import sandbox


class GuardTests(unittest.TestCase):
    def setup_guard(self, db, email='alice@gmail.com', subject='alice-sub'):
        from cloud.identity.guard import RequestDatabase
        issued = admit_fixture(db,email,subject)
        guarded = RequestDatabase(db,lambda: issued.proof)
        actor = Actor(issued.proof.owner_id)
        return issued, guarded, actor, Work(guarded).create_conversation(actor,'chat')

    def test_resolved_session_cannot_write_after_logout(self):
        with sandbox() as db:
            issued, guarded, actor, cid = self.setup_guard(db)
            Sessions(db).revoke(issued.proof)
            with self.assertRaises(AuthenticationRequired):
                Work(guarded).accept(actor,Submission(cid,1,uuid4(),'must not commit'))
            self.assertEqual(db.read('SELECT * FROM jobs'),[])
            self.assertEqual(db.read('SELECT * FROM outbox'),[])

    def test_both_mutation_logout_orders(self):
        from cloud.identity.guard import RequestDatabase
        for first in ('mutation','logout'):
            with self.subTest(first=first), sandbox() as db:
                issued, guarded, actor, cid = self.setup_guard(db)
                held, release, second_started = threading.Event(), threading.Event(), threading.Event()
                original = guarded.transaction if first=='mutation' else db.transaction
                @contextmanager
                def paused():
                    with original() as tx:
                        if first=='logout':
                            # Hold exactly the same authority lock as revocation.
                            from cloud.identity.guard import lock_session
                            lock_session(tx,issued.proof)
                        held.set()
                        if not release.wait(10): raise AssertionError('not released')
                        yield tx
                if first=='mutation': guarded.transaction=paused
                # Separate db facade pauses only logout, not the other request.
                class LogoutDB:
                    transaction = staticmethod(paused)
                def mutate():
                    return Work(guarded).accept(actor,Submission(cid,1,uuid4(),'fixture'))
                def logout():
                    return Sessions(LogoutDB() if first=='logout' else db).revoke(issued.proof)
                def second():
                    second_started.set()
                    return logout() if first=='mutation' else mutate()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a=pool.submit(mutate if first=='mutation' else logout)
                    self.assertTrue(held.wait(10))
                    b=pool.submit(second)
                    self.assertTrue(second_started.wait(10))
                    self.assertFalse(b.done())
                    release.set()
                    a.result(timeout=10)
                    if first=='logout':
                        with self.assertRaises(AuthenticationRequired): b.result(timeout=10)
                    else: b.result(timeout=10)
                self.assertEqual(len(db.read('SELECT * FROM jobs')),1 if first=='mutation' else 0)

    def test_cross_owner_cancel_never_takes_foreign_owner_lock(self):
        from cloud.identity.guard import RequestDatabase
        with sandbox() as db:
            alice=self.setup_guard(db)
            bob=self.setup_guard(db,'bob@gmail.com','bob-sub')
            jobs=[Work(x[1]).accept(x[2],Submission(x[3],1,uuid4(),'fixture')).job_id for x in (alice,bob)]
            barrier=threading.Barrier(2)
            class Together(RequestDatabase):
                @contextmanager
                def transaction(self):
                    with super().transaction() as tx:
                        barrier.wait(timeout=10)
                        yield tx
            def cancel(person,target):
                with self.assertRaises(NotFound):
                    Control(Together(db,lambda:person[0].proof)).cancel(person[2],target)
            with ThreadPoolExecutor(max_workers=2) as pool:
                a=pool.submit(cancel,alice,jobs[1]); b=pool.submit(cancel,bob,jobs[0])
                a.result(timeout=10); b.result(timeout=10)
            self.assertTrue(all(j['state']=='queued' for j in db.read('SELECT state FROM jobs')))

    def test_disabled_account_and_restore_deny_reads_ordinary_pause_allows(self):
        with sandbox() as db:
            issued, guarded, actor, cid=self.setup_guard(db)
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            self.assertEqual(Work(guarded).snapshot(actor,cid)['messages'],[])
            from cloud.work import create_conversation_in
            with guarded.transaction() as tx:
                create_conversation_in(tx,actor,'chat',require_enabled=False)
            Sessions(db).disable(actor.owner_id,'operator','fixture')
            with self.assertRaises(AuthenticationRequired): Work(guarded).snapshot(actor,cid)
        with sandbox() as db:
            issued, guarded, actor, cid=self.setup_guard(db)
            Recovery(db).begin_restore()
            with self.assertRaises(Unavailable): guarded.read('SELECT * FROM todos')

    def test_http_gate_and_auth_error_mappings(self):
        from flask import Flask
        from cloud.http import register_routes
        from cloud.identity.types import Forbidden, AdmissionDenied, RateLimited
        with sandbox() as db:
            for error, status in ((AuthenticationRequired(),401),(Forbidden(),403),(AdmissionDenied(),403),(RateLimited(17),429),(Unavailable('executor_not_integrated'),503)):
                app=Flask(__name__)
                def gate(): raise error
                register_routes(app,db,lambda:Actor('alice'),submit_gate=gate,include_ready=False)
                response=app.test_client().post('/api/work/conversations/'+str(uuid4())+'/messages',json={})
                self.assertEqual(response.status_code,status)
                if status==429: self.assertEqual(response.headers['Retry-After'],'17')
                self.assertEqual(app.test_client().get('/ready').status_code,404)
