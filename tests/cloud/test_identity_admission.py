"""Admission must be atomic, subject-bound and limited across replicas."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch

from cloud.identity.types import AdmissionDenied, AuthenticationRequired, GoogleIdentity
from cloud.types import Rejected, Unavailable
from tests.cloud.support import sandbox
from tests.cloud.identity_support import admit_fixture, seed_claim


class AdmissionTests(unittest.TestCase):
    def test_admission_is_stable_subject_not_email(self):
        from cloud.identity.admission import Admission
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            first = admit_fixture(db)
            with self.assertRaises(AdmissionDenied):
                Admission(db).complete(GoogleIdentity('other-sub','alice@gmail.com','Other',None), seed_claim(db))
            changed = Admission(db).complete(GoogleIdentity('alice-sub','changed@third.invalid','Changed',None), seed_claim(db))
            self.assertEqual(changed.proof.owner_id, first.proof.owner_id)
            self.assertEqual(Sessions(db).resolve(first.token).owner_id, first.proof.owner_id)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_identities')[0]['n'],1)

    def test_final_seat_is_reserved_atomically_across_services(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            for i in range(49):
                Admission(db).invite(f'u{i}@gmail.com','fixture','reserve')
            barrier = threading.Barrier(2)
            def invite(email):
                barrier.wait(timeout=10)
                try:
                    Admission(db).invite(email,'fixture','race')
                    return 'reserved'
                except AdmissionDenied:
                    return 'denied'
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(invite, email) for email in ('last@gmail.com','extra@gmail.com')]
                self.assertCountEqual([f.result(timeout=10) for f in futures], ['reserved','denied'])
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_invites')[0]['n'],50)

    def test_two_subjects_cannot_redeem_one_reservation(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            Admission(db).invite('alice@gmail.com','fixture','reserve')
            claims = [seed_claim(db),seed_claim(db)]
            barrier = threading.Barrier(2)
            def redeem(i):
                barrier.wait(timeout=10)
                try:
                    return Admission(db).complete(GoogleIdentity(f'sub-{i}','alice@gmail.com','Alice',None),claims[i]).proof.owner_id
                except AdmissionDenied:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(redeem,i) for i in (0,1)]
                results = [f.result(timeout=10) for f in futures]
            self.assertEqual(sum(value is not None for value in results),1)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_identities')[0]['n'],1)

    def test_parallel_callbacks_for_same_subject_create_one_account(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            Admission(db).invite('alice@gmail.com','fixture','reserve')
            claims = [seed_claim(db),seed_claim(db)]
            barrier = threading.Barrier(2)
            def redeem(i):
                barrier.wait(timeout=10)
                return Admission(db).complete(GoogleIdentity('same','alice@gmail.com','Alice',None),claims[i])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(redeem,i) for i in (0,1)]
                results = [f.result(timeout=10) for f in futures]
            self.assertEqual(results[0].proof.owner_id, results[1].proof.owner_id)
            self.assertNotEqual(results[0].token, results[1].token)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_sessions')[0]['n'],2)

    def test_expired_revoked_and_missing_invitations_are_denied(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            service = Admission(db)
            expired = service.invite('expired@gmail.com','fixture','reserve')
            revoked = service.invite('revoked@gmail.com','fixture','reserve')
            with db.transaction() as tx:
                tx.execute("UPDATE auth_invites SET created_at=clock_timestamp()-interval '8 days',expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(expired,))
            service.revoke_invite(revoked,'fixture','revoke')
            for email in ('expired@gmail.com','revoked@gmail.com','missing@gmail.com'):
                with self.subTest(email=email), self.assertRaises(AdmissionDenied):
                    service.complete(GoogleIdentity(email,email,'Name',None),seed_claim(db))
            self.assertEqual(db.read('SELECT * FROM auth_identities'),[])
            # Reissuing the same reservation does not allocate another row.
            self.assertEqual(service.invite(' EXPIRED@gmail.com ','fixture','renew'), expired)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_invites')[0]['n'],2)

    def test_email_aliases_are_not_folded_and_third_party_requires_proof(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            service = Admission(db)
            service.invite('a.lice+tag@gmail.com','fixture','reserve')
            for email in ('alice@gmail.com','a.lice@gmail.com'):
                with self.assertRaises(AdmissionDenied):
                    service.complete(GoogleIdentity(email,email,'Name',None),seed_claim(db))
            service.invite('external@third.invalid','fixture','reserve')
            with self.assertRaises(AdmissionDenied):
                service.complete(GoogleIdentity('external','external@third.invalid','Name',None),seed_claim(db))
            service.invite('work@workspace.invalid','fixture','reserve')
            result = service.complete(GoogleIdentity('work','work@workspace.invalid','Work','workspace.invalid'),seed_claim(db))
            self.assertTrue(result.proof.owner_id)

    def test_disabled_identity_cannot_sign_in_or_release_a_redeemed_seat(self):
        from cloud.identity.admission import Admission
        from cloud.identity.sessions import Sessions
        with sandbox() as db:
            issued = admit_fixture(db)
            Sessions(db).disable(issued.proof.owner_id,'fixture','disable')
            with self.assertRaises(AuthenticationRequired):
                Sessions(db).resolve(issued.token)
            with self.assertRaises(AdmissionDenied):
                Admission(db).complete(GoogleIdentity('alice-sub','alice@gmail.com','Alice',None),seed_claim(db))
            with self.assertRaises(AdmissionDenied):
                Admission(db).invite('alice@gmail.com','fixture','reuse')
            for i in range(49):
                Admission(db).invite(f'u{i}@gmail.com','fixture','reserve')
            with self.assertRaises(AdmissionDenied):
                Admission(db).invite('extra@gmail.com','fixture','reserve')

    def test_claim_is_one_use_current_epoch_and_bound(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            service = Admission(db)
            service.invite('alice@gmail.com','fixture','reserve')
            claim = seed_claim(db)
            identity = GoogleIdentity('alice-sub','alice@gmail.com','Alice',None)
            with self.assertRaises(AdmissionDenied):
                service.complete(identity,replace(claim,browser_hash='0'*64))
            issued = service.complete(identity,claim)
            with self.assertRaises(AdmissionDenied):
                service.complete(identity,claim)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_sessions')[0]['n'],1)

    def test_failed_commit_does_not_consume_invitation_or_create_session(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            service = Admission(db)
            service.invite('alice@gmail.com','fixture','reserve')
            claim = seed_claim(db)
            transaction = db.transaction
            @contextmanager
            def fail_commit():
                with transaction() as tx:
                    yield tx
                    raise ConnectionError('synthetic commit failure')
            with patch.object(db,'transaction',fail_commit), self.assertRaises(ConnectionError):
                service.complete(GoogleIdentity('alice-sub','alice@gmail.com','Alice',None),claim)
            self.assertEqual(db.read('SELECT * FROM auth_identities'),[])
            self.assertIsNone(db.read('SELECT redeemed_owner_id FROM auth_invites')[0]['redeemed_owner_id'])
            self.assertEqual(db.read('SELECT status FROM auth_flows')[0]['status'],'claimed')

    def test_email_and_audit_input_validation_precedes_writes(self):
        from cloud.identity.admission import Admission
        with sandbox() as db:
            for email in ('a','a@@gmail.com','a b@gmail.com','é@gmail.com','x'*321):
                with self.subTest(email=email), self.assertRaises(Rejected):
                    Admission(db).invite(email,'fixture','reserve')
            for actor,reason in (('', 'x'),('x',''),('x'*129,'x'),('x','x'*501)):
                with self.assertRaises(Rejected):
                    Admission(db).invite('alice@gmail.com',actor,reason)
            self.assertEqual(db.read('SELECT * FROM auth_invites'),[])
