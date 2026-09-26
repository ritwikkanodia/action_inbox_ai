"""Local restore evidence: never an Azure disaster-recovery certification."""
import unittest
from cloud.identity.admission import Admission
from cloud.identity.sessions import Sessions
from cloud.identity.types import AuthenticationRequired, GoogleIdentity, AdmissionDenied
from cloud.recovery import Recovery, CHECKS
from cloud.types import Unavailable
from tests.cloud.identity_support import admit_fixture, seed_claim
from tests.cloud.support import sandbox, restored_copy


def reviewed_resume(db,epoch):
    recovery=Recovery(db)
    for check in CHECKS: recovery.review_check(epoch,check,'fixture-reviewer','local synthetic evidence')
    recovery.resume_after_review(epoch,'fixture-reviewer','local synthetic evidence')


class IdentityRecoveryTests(unittest.TestCase):
    def test_old_session_and_flow_fail_during_and_after_restore(self):
        with sandbox() as db:
            issued=admit_fixture(db); claim=seed_claim(db)
            epoch=Recovery(db).begin_restore()
            with self.assertRaises(Unavailable): Sessions(db).resolve(issued.token)
            with self.assertRaises(Unavailable): Admission(db).complete(GoogleIdentity('new','new@gmail.com','New',None),claim)
            reviewed_resume(db,epoch)
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(issued.token)
            with self.assertRaises(Unavailable): Admission(db).complete(GoogleIdentity('new','new@gmail.com','New',None),claim)
            fresh=admit_fixture(db)
            self.assertEqual(Sessions(db).resolve(fresh.token).owner_id,issued.proof.owner_id)
            self.assertEqual(len(db.read('SELECT * FROM auth_identities')),1)

    def test_restore_reapplies_independent_revocation_before_resume(self):
        with sandbox() as db:
            issued=admit_fixture(db)
            independently_retained_owner=issued.proof.owner_id
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            with restored_copy(db) as restored:
                epoch=Recovery(restored).begin_restore()
                # A revocation retained outside the backup must be reconciled
                # while ingress is still blocked, not after reopening login.
                Sessions(restored).disable(independently_retained_owner,'fixture-reviewer','independent revocation record')
                with self.assertRaises(Unavailable): Sessions(restored).resolve(issued.token)
                reviewed_resume(restored,epoch)
                with self.assertRaises(AuthenticationRequired): Sessions(restored).resolve(issued.token)
                with self.assertRaises(AdmissionDenied): admit_fixture(restored)
                self.assertEqual(restored.read("SELECT count(*) AS n FROM auth_audit WHERE action='identity_disabled'")[0]['n'],1)
