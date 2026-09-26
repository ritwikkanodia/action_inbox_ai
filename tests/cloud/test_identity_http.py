"""Real routes with signed synthetic provider tokens and isolated PostgreSQL."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit
import logging
import unittest
from unittest.mock import patch
from uuid import uuid4
from cloud.identity.config import WebSettings
from cloud.identity.sessions import Sessions
from cloud.identity.types import AuthenticationRequired
from cloud.recovery import Recovery
from tests.cloud.identity_support import test_settings
from tests.cloud.support import sandbox


class IdentityHttpTests(unittest.TestCase):
    def fixture(self,db,**kwargs):
        from tests.cloud.identity_support import web_fixture
        return web_fixture(db,**kwargs)

    def login(self,client,db,email='alice@gmail.com',subject='alice-sub'):
        from tests.cloud.identity_support import login_fixture
        return login_fixture(client,db,email,subject)

    def headers(self,proof):
        return {'Origin':'https://localhost','X-CSRF-Token':proof['csrf'],
                'X-Athena-Context':proof['context_id']}

    def start(self,client):
        import re
        response=client.get('/login')
        csrf=re.search(r'name="csrf" value="([^"]+)"',response.get_data(as_text=True))[1]
        response=client.post('/oauth/login',data={'csrf':csrf},headers={'Origin':'https://localhost'})
        self.assertEqual(response.status_code,302)
        return parse_qs(urlsplit(response.location).query)

    def test_production_has_no_injection_and_validates_direct_settings(self):
        from cloud.web import create_web_app
        from dataclasses import replace
        settings=WebSettings.from_mapping(test_settings())
        with self.assertRaises(TypeError): create_web_app(settings,google=object())
        with self.assertRaises(ValueError): create_web_app(replace(settings,public_origin='http://localhost'))
        app=create_web_app(settings)
        self.assertFalse(app.testing)
        self.assertFalse(app.debug)

    def test_signed_login_rotates_session_and_logout_has_host_cookie_flags(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client()
            proof=self.login(client,db)
            old=client.get_cookie('__Host-athena-session').value
            other=self.login(client,db)
            self.assertNotEqual(proof['context_id'],other['context_id'])
            with self.assertRaises(AuthenticationRequired): Sessions(db).resolve(old)
            self.assertEqual(client.get('/logout').status_code,405)
            response=client.post('/logout',headers=self.headers(other))
            self.assertEqual(response.status_code,303)
            cookies=response.headers.getlist('Set-Cookie')
            self.assertTrue(cookies)
            for cookie in cookies:
                for flag in ('Secure','HttpOnly','Path=/','SameSite=Lax'): self.assertIn(flag,cookie)
                self.assertNotIn('Domain=',cookie)
            self.assertIsNone(client.get_cookie('__Host-athena-session'))

    def test_origin_csrf_context_json_and_host_checks(self):
        with sandbox() as db:
            client=self.fixture(db).test_client(); proof=self.login(client,db)
            for headers in ({},{'Origin':'https://evil.invalid'},dict(self.headers(proof),Origin='null'),
                            dict(self.headers(proof),**{'X-CSRF-Token':'wrong'})):
                self.assertEqual(client.post('/logout',headers=headers).status_code,403)
            self.assertEqual(client.post('/logout',headers=dict(self.headers(proof),**{'X-Athena-Context':str(uuid4())})).status_code,409)
            self.assertEqual(client.post('/api/work/conversations',data='kind=chat',headers=self.headers(proof)).status_code,400)
            self.assertEqual(client.get('/live',headers={'Host':'evil.invalid','X-Forwarded-Host':'localhost'}).status_code,400)
            response=client.get('/login',headers={'X-Forwarded-Host':'evil.invalid','X-Forwarded-Proto':'http'})
            self.assertEqual(response.status_code,200)
            headers=self.headers(proof); headers.pop('Origin'); headers['Referer']='https://localhost/settings'
            self.assertEqual(client.post('/api/auth/logout-all',json={},headers=headers).status_code,200)

    def test_prelogin_csrf_denial_duplicate_and_replay_do_not_exchange(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client()
            self.assertEqual(client.post('/oauth/login',data={'csrf':'wrong'}).status_code,403)
            params=self.start(client); state=params['state'][0]
            for query in (f'state={state}&state={state}&code=a',f'state={state}&code=a&code=b','x='+'a'*8200):
                self.assertEqual(client.get('/oauth/login/callback?'+query).status_code,403)
            response=client.get('/oauth/login/callback',query_string={'state':state,'error':'access_denied','error_description':'SECRET-MARKER'})
            self.assertEqual(response.status_code,403)
            self.assertNotIn('SECRET-MARKER',response.get_data(as_text=True))
            self.assertEqual(app.extensions['fixture_transport'].calls,[])
            self.assertEqual(client.get('/oauth/login/callback',query_string={'state':state,'code':'a'}).status_code,403)
            old=client.get_cookie('__Host-athena-prelogin').value
            client.get('/login')
            self.assertNotEqual(client.get_cookie('__Host-athena-prelogin').value,old)
            self.assertEqual(db.read('SELECT * FROM auth_sessions'),[])

    def test_full_flow_fixed_redirect_and_no_gmail_or_secrets(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client()
            proof=self.login(client,db)
            self.assertIn('context_id',proof)
            self.assertEqual(len(db.read('SELECT * FROM auth_identities')),1)
            self.assertEqual(db.read('SELECT * FROM connections'),[])
            flow=db.read('SELECT * FROM auth_flows')[0]
            self.assertIsNone(flow['verifier_cipher'])
            self.assertEqual(flow['status'],'finished')
            response=client.get('/login?return_url=https://evil.invalid')
            self.assertNotIn('evil.invalid',response.get_data(as_text=True))
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
            self.assertNotIn('unsafe-',response.headers['Content-Security-Policy'])
            self.assertNotIn('Access-Control-Allow-Origin',response.headers)

    def test_logout_database_failure_never_reports_success(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client(); proof=self.login(client,db)
            with patch.object(db,'transaction',side_effect=ConnectionError('SECRET-DSN')):
                response=client.post('/logout',headers=self.headers(proof))
            self.assertEqual(response.status_code,503)
            self.assertNotIn('SECRET-DSN',response.get_data(as_text=True))
            self.assertIsNotNone(client.get_cookie('__Host-athena-session'))

    def test_callback_commit_failure_issues_no_session_or_account(self):
        from tests.cloud.identity_support import prepare_callback
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client()
            query=prepare_callback(client,db,'alice@gmail.com','alice-sub')
            original=db.transaction
            @contextmanager
            def failure():
                with original() as tx:
                    yield tx
                    if tx.execute('SELECT count(*) AS n FROM auth_identities').fetchone()['n']:
                        raise ConnectionError('SECRET-DSN')
            with patch.object(db,'transaction',failure):
                response=client.get('/oauth/login/callback',query_string=query)
            self.assertEqual(response.status_code,503)
            self.assertIsNone(client.get_cookie('__Host-athena-session'))
            self.assertEqual(db.read('SELECT * FROM auth_identities'),[])
            self.assertEqual(db.read('SELECT * FROM auth_sessions'),[])

    def test_cookie_expiry_and_account_disable_clear_browser_session(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client(); proof=self.login(client,db)
            raw=client.get_cookie('__Host-athena-session').value
            owner=Sessions(db).resolve(raw).owner_id
            Sessions(db).disable(owner,'fixture','test')
            response=client.get('/api/work/conversations/'+str(uuid4()))
            self.assertEqual(response.status_code,401)
            self.assertIsNone(client.get_cookie('__Host-athena-session'))

    def test_readiness_checks_schema_and_recovery_but_not_executor(self):
        with sandbox() as db:
            client=self.fixture(db).test_client()
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            self.assertEqual(client.get('/live').status_code,200)
            self.assertEqual(client.get('/ready').status_code,200)
            with db.transaction() as tx: tx.execute("UPDATE schema_migrations SET sha256='bad' WHERE version='008_identity.sql'")
            self.assertEqual(client.get('/ready').status_code,503)
        with sandbox() as db:
            client=self.fixture(db).test_client(); Recovery(db).begin_restore()
            self.assertEqual(client.get('/ready').status_code,503)
            self.assertEqual(client.get('/login').status_code,503)
            self.assertEqual(client.get('/live').status_code,200)

    def test_submission_remains_disabled_and_unknown_owner_is_not_authority(self):
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client(); proof=self.login(client,db)
            response=client.post('/api/work/conversations/'+str(uuid4())+'/messages',json={},headers=self.headers(proof))
            self.assertEqual(response.status_code,503)
            self.assertEqual(response.json['error'],'executor_not_integrated')
            stranger=app.test_client()
            self.assertEqual(stranger.get('/api/work/conversations/'+str(uuid4()),headers={'X-Owner-ID':'alice'}).status_code,401)
            self.assertEqual(db.read('SELECT * FROM jobs'),[])

    def test_expired_prelogin_and_missing_login_origin_are_denied(self):
        import re
        with sandbox() as db:
            client=self.fixture(db).test_client()
            page=client.get('/login').get_data(as_text=True)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page)[1]
            self.assertEqual(client.post('/oauth/login',data={'csrf':csrf},headers={'Referer':'https://localhost/login'}).status_code,403)
            with patch('itsdangerous.timed.time.time',return_value=1):
                client.delete_cookie('__Host-athena-prelogin')
                page=client.get('/login').get_data(as_text=True)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page)[1]
            self.assertEqual(client.post('/oauth/login',data={'csrf':csrf},headers={'Origin':'https://localhost'}).status_code,403)

    def test_logs_do_not_include_provider_codes_or_tokens(self):
        import io
        with sandbox() as db:
            app=self.fixture(db); client=app.test_client()
            output=io.StringIO(); handler=logging.StreamHandler(output)
            root=logging.getLogger(); root.addHandler(handler)
            level=root.level; root.setLevel(logging.DEBUG)
            try:
                self.login(client,db)
                client.get('/oauth/login/callback?state=SECRET-STATE&code=SECRET-CODE')
            finally:
                root.removeHandler(handler)
                root.setLevel(level)
            self.assertNotIn('SECRET-',output.getvalue())
            self.assertNotIn('synthetic-access',output.getvalue())
            self.assertNotIn('id_token',output.getvalue())
            from pathlib import Path
            config=Path('cloud/gunicorn.conf.py').read_text()
            self.assertIn("'%(m)s %(U)s %(s)s %(L)s'",config)
            self.assertNotIn('%(r)',config)
