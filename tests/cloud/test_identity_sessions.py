"""Session authority must survive restarts but not revocation or expiry."""
import unittest
from dataclasses import replace

from cloud.identity.crypto import hash_token
from cloud.identity.types import AuthenticationRequired, GoogleIdentity
from cloud.recovery import Recovery
from cloud.types import Unavailable
from tests.cloud.support import sandbox
from tests.cloud.identity_support import admit_fixture, seed_claim


class SessionTests(unittest.TestCase):
    def test_restart_logout_and_logout_all(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            one = admit_fixture(db)
            two = admit_fixture(db)
            self.assertEqual(Sessions(db).resolve(one.token),one.proof)
            Sessions(db).revoke(one.proof)
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(one.token)
            self.assertEqual(Sessions(db).resolve(two.token),two.proof)
            three = admit_fixture(db)
            Sessions(db).revoke(two.proof,all_sessions=True)
            for token in (two.token,three.token):
                with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(token)

    def test_sixth_session_evicts_oldest_only(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            issued = [admit_fixture(db) for _ in range(6)]
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(issued[0].token)
            for session in issued[1:]:
                self.assertEqual(Sessions(db).resolve(session.token),session.proof)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_sessions WHERE revoked_at IS NULL')[0]['n'],5)

    def test_idle_and_absolute_expiry_are_enforced_server_side(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            for kind in ('idle','absolute'):
                session = admit_fixture(db)
                with db.transaction() as tx:
                    if kind == 'idle':
                        tx.execute("UPDATE auth_sessions SET last_seen_at=clock_timestamp()-interval '24 hours' WHERE token_hash=%s",(hash_token(session.token),))
                    else:
                        tx.execute("UPDATE auth_sessions SET created_at=clock_timestamp()-interval '8 days',expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",(hash_token(session.token),))
                with self.subTest(kind=kind),self.assertRaises(AuthenticationRequired):
                    Sessions(db).resolve(session.token)

    def test_previous_browser_session_is_revoked_on_account_switch(self):
        from cloud.identity.sessions import Sessions
        from cloud.identity.admission import Admission
        with sandbox() as db:
            alice = admit_fixture(db)
            Admission(db).invite('bob@gmail.com','fixture','reserve')
            bob = Admission(db).complete(GoogleIdentity('bob-sub','bob@gmail.com','Bob',None),seed_claim(db),previous_token=alice.token)
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(alice.token)
            self.assertEqual(Sessions(db).resolve(bob.token).owner_id,bob.proof.owner_id)

    def test_owner_disable_and_epoch_change_invalidate_authority(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            session = admit_fixture(db)
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            self.assertEqual(Sessions(db).resolve(session.token),session.proof)
            Recovery(db).begin_restore()
            with self.assertRaises(Unavailable): Sessions(db).resolve(session.token)

    def test_malformed_unknown_tokens_and_tampered_proof_are_denied(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            session = admit_fixture(db)
            for token in ('',None,'é','x'*44,'0'*43):
                with self.subTest(token=token),self.assertRaises(AuthenticationRequired):
                    Sessions(db).resolve(token)
            with self.assertRaises(AuthenticationRequired):
                Sessions(db).revoke(replace(session.proof,owner_id='bob'))
            self.assertEqual(Sessions(db).resolve(session.token),session.proof)

    def test_operator_revoke_is_scoped_and_audited(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            alice = admit_fixture(db)
            bob = admit_fixture(db,'bob@gmail.com','bob-sub')
            Sessions(db).revoke_owner(alice.proof.owner_id,'fixture','lost device')
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(alice.token)
            self.assertEqual(Sessions(db).resolve(bob.token),bob.proof)
            audit = db.read("SELECT * FROM auth_audit WHERE action='sessions_revoked'")
            self.assertEqual(audit[-1]['owner_id'],alice.proof.owner_id)

    def test_purge_removes_expired_authority_not_accounts(self):
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            session = admit_fixture(db)
            Sessions(db).revoke(session.proof)
            with db.transaction() as tx:
                tx.execute("UPDATE auth_flows SET expires_at=clock_timestamp()-interval '1 second'")
                tx.execute("INSERT INTO auth_limits VALUES ('old',clock_timestamp()-interval '1 day',1)")
            counts = Sessions(db).purge()
            self.assertEqual(counts,{'sessions':1,'flows':1,'limits':1})
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_identities')[0]['n'],1)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_invites')[0]['n'],1)
