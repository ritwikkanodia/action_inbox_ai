"""Operator tools use explicit cloud DB configuration and emit no secrets."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from cloud.identity.sessions import Sessions
from cloud.identity.types import AuthenticationRequired
from tests.cloud.identity_support import admit_fixture
from tests.cloud.support import sandbox


class IdentityCliTests(unittest.TestCase):
    def call(self,args,db):
        from cloud.identity.cli import main
        out=io.StringIO()
        with redirect_stdout(out): result=main(args,db=db)
        return result,out.getvalue()

    def test_mutations_require_actor_and_reason(self):
        from cloud.identity.cli import main
        for args in (['invite','--email','alice@gmail.com','--actor','operator'],
                     ['disable-user','--owner-id','fixture','--reason','fixture']):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                main(args)

    def test_invite_revoke_and_no_automatic_migration(self):
        with sandbox() as db:
            with patch('cloud.identity.cli.migrate') as migration:
                result,out=self.call(['invite','--email','alice@gmail.com','--actor','operator','--reason','fixture'],db)
                self.assertEqual(result,0); invitation=json.loads(out)['id']
                for _ in range(2):
                    self.assertEqual(self.call(['revoke-invite','--invite-id',invitation,'--actor','operator','--reason','fixture'],db)[0],0)
                migration.assert_not_called()
            self.assertEqual(len(db.read('SELECT * FROM auth_audit')),3)
            self.assertNotIn('alice@gmail.com',out)
            self.assertEqual(self.call(['migrate'],db)[0],0)

    def test_disable_and_revoke_sessions_do_not_delete_account(self):
        with sandbox() as db:
            issued=admit_fixture(db); owner=issued.proof.owner_id
            for command in ('revoke-sessions','disable-user','disable-user'):
                result,out=self.call([command,'--owner-id',owner,'--actor','operator','--reason','fixture'],db)
                self.assertEqual(result,0)
                self.assertNotIn(issued.token,out)
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(issued.token)
            self.assertEqual(len(db.read('SELECT * FROM auth_identities')),1)

    def test_invalid_values_and_database_error_are_safe(self):
        with sandbox() as db:
            for args in (['invite','--email','bad-email'],['revoke-invite','--invite-id','not-a-uuid'],
                         ['disable-user','--owner-id',str(uuid4())]):
                result,out=self.call(args+['--actor','operator','--reason','fixture'],db)
                self.assertNotEqual(result,0)
                self.assertNotIn(args[-1],out)
            with patch.object(db,'transaction',side_effect=ConnectionError('SECRET-DSN')):
                result,out=self.call(['purge'],db)
                self.assertNotEqual(result,0)
                self.assertNotIn('SECRET',out)

    def test_bounded_purge_keeps_audits_and_accounts(self):
        with sandbox() as db:
            issued=admit_fixture(db)
            with db.transaction() as tx:
                tx.execute("""INSERT INTO auth_limits(key,bucket_start,attempts)
                    SELECT 'expired-'||n,clock_timestamp()-interval '1 day',1 FROM generate_series(1,700) n""")
            result,out=self.call(['purge'],db)
            self.assertEqual(result,0)
            self.assertEqual(json.loads(out)['counts']['limits'],500)
            self.assertEqual(len(db.read('SELECT * FROM auth_identities')),1)
            self.assertTrue(db.read('SELECT * FROM auth_audit'))
            self.assertEqual(Sessions(db).resolve(issued.token),issued.proof)

    def test_cli_configuration_does_not_read_google_or_inherit_local_database(self):
        from cloud.identity.cli import main
        with patch.dict('os.environ',{'DATABASE_URL':'SECRET-local-database'},clear=True),redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(main(['purge']),0)
        self.assertNotIn('SECRET',out.getvalue())

    def test_revoke_invite_is_available_before_recovery_resume(self):
        from cloud.recovery import Recovery
        with sandbox() as db:
            _,out=self.call(['invite','--email','alice@gmail.com','--actor','operator','--reason','fixture'],db)
            identifier=json.loads(out)['id']
            Recovery(db).begin_restore()
            result,_=self.call(['revoke-invite','--invite-id',identifier,'--actor','operator','--reason','independent record'],db)
            self.assertEqual(result,0)
            self.assertIsNotNone(db.read('SELECT revoked_at FROM auth_invites')[0]['revoked_at'])
