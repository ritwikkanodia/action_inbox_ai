"""Real signed-token verification with only provider HTTP replaced."""
import base64
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from cloud.identity.crypto import hash_token, new_token
from cloud.identity.types import Forbidden, FlowStart, ProviderResponse
from cloud.types import Unavailable
from tests.cloud.identity_support import SignedGoogleFixture


class GoogleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = SignedGoogleFixture()

    def test_valid_signed_token_yields_identity_and_bounded_pkce_request(self):
        claims = self.fixture.valid_claims()
        adapter, claim = self.fixture.adapter_and_claim(claims,expected_nonce='expected')
        identity = adapter.exchange('synthetic-code',claim)
        self.assertEqual((identity.subject,identity.email,identity.name),('alice-sub','alice@gmail.com','Alice'))
        method,url,body = adapter.transport.calls[0]
        values = parse_qs(body.decode())
        self.assertEqual(url,'https://oauth2.googleapis.com/token')
        self.assertEqual(values['redirect_uri'],['https://athena.example.invalid/oauth/login/callback'])
        self.assertEqual(values['code_verifier'],[claim.verifier])
        self.assertEqual(values['grant_type'],['authorization_code'])
        self.assertNotIn('synthetic-access',repr(identity))

    def test_authorization_requests_identity_only_with_state_nonce_pkce(self):
        adapter,claim = self.fixture.adapter_and_claim(self.fixture.valid_claims(),expected_nonce='expected')
        start = FlowStart(new_token(),new_token(),'challenge',claim.epoch)
        parsed = urlsplit(adapter.authorization_url(start))
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname,'accounts.google.com')
        self.assertEqual(set(query['scope'][0].split()),{'openid','email','profile'})
        self.assertEqual(query['state'],[start.state])
        self.assertEqual(query['nonce'],[start.nonce])
        self.assertEqual(query['code_challenge_method'],['S256'])
        self.assertNotIn('access_type',query)
        self.assertNotIn('include_granted_scopes',query)

    def test_wrong_or_missing_security_claims_are_denied(self):
        cases = [('nonce','wrong'),('iss','https://evil.invalid'),('aud','other-client'),
                 ('aud',['fixture.apps.googleusercontent.com']),('azp','other-client'),
                 ('email_verified',False),('email_verified','true'),('sub',''),
                 ('sub','é'),('sub','x'*256),('iat',int(time.time())+3600),('iat',True),
                 ('exp',int(time.time())-1),('exp',True),('email','not-email'),
                 ('name','x'*201),('hd',[])]
        for key,value in cases:
            with self.subTest(key=key,value=value):
                claims = dict(self.fixture.valid_claims(),**{key:value})
                adapter,claim = self.fixture.adapter_and_claim(claims,expected_nonce='expected')
                with self.assertRaises(Forbidden): adapter.exchange('synthetic-code',claim)
        for key in ('iss','aud','sub','iat','exp','nonce','email','email_verified'):
            claims = self.fixture.valid_claims()
            del claims[key]
            with self.subTest(missing=key):
                adapter,claim = self.fixture.adapter_and_claim(claims,expected_nonce='expected')
                with self.assertRaises(Forbidden): adapter.exchange('synthetic-code',claim)

    def test_signature_unknown_kid_and_algorithm_are_denied(self):
        claims = self.fixture.valid_claims()
        other = SignedGoogleFixture()
        for variant in ('signature','kid','algorithm'):
            adapter,claim = self.fixture.adapter_and_claim(claims,expected_nonce='expected')
            if variant == 'signature':
                adapter.transport.token = other.token(claims)
            else:
                parts = adapter.transport.token.split('.')
                header = {'alg':'HS256' if variant=='algorithm' else 'RS256','kid':'missing' if variant=='kid' else 'fixture'}
                parts[0] = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b'=').decode()
                adapter.transport.token = '.'.join(parts)
            with self.subTest(variant=variant),self.assertRaises(Forbidden):
                adapter.exchange('synthetic-code',claim)

    def test_extra_claims_and_alternate_google_issuer_do_not_break_login(self):
        claims = self.fixture.valid_claims()
        claims.update(iss='accounts.google.com',unrelated={'field':'allowed'})
        del claims['name']
        adapter,claim = self.fixture.adapter_and_claim(claims,expected_nonce='expected')
        self.assertEqual(adapter.exchange('synthetic-code',claim).name,'alice@gmail.com')

    def test_bad_code_token_and_upstream_failure_are_sanitized(self):
        adapter,claim = self.fixture.adapter_and_claim(self.fixture.valid_claims(),expected_nonce='expected')
        for code in ('',None,'x'*4097,'secret\ncode'):
            with self.assertRaises(Forbidden): adapter.exchange(code,claim)
        adapter.transport.token = 'x'*16385
        with self.assertRaises(Forbidden): adapter.exchange('code',claim)
        adapter.transport.failure = Unavailable('identity_provider_unavailable')
        with self.assertRaises(Unavailable) as error: adapter.exchange('code',claim)
        self.assertNotIn('code',str(error.exception))


class HTTPResponse:
    def __init__(self, data=b'{}', status=200, headers=None):
        self.data,self.status_code,self.headers = data,status,headers or {}
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_content(self,chunk_size=1):
        for i in range(len(self.data)): yield self.data[i:i+1]


class HTTPSession:
    def __init__(self,response=None,error=None):
        self.response,self.error,self.calls = response,error,[]
        self.trust_env = True
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        if self.error: raise self.error
        return self.response


class TransportTests(unittest.TestCase):
    def test_transport_rejects_unknown_endpoints_and_redirects(self):
        from cloud.identity.transport import BoundedGoogleTransport
        for url in ('http://oauth2.googleapis.com/token','https://evil.invalid/token',
                    'https://oauth2.googleapis.com/token?next=evil','https://oauth2.googleapis.com@evil.invalid/token'):
            with self.subTest(url=url),self.assertRaises(Unavailable):
                BoundedGoogleTransport().request('POST',url)
        session = HTTPSession(HTTPResponse(status=302,headers={'Location':'https://evil.invalid'}))
        with patch('cloud.identity.transport.requests.Session',return_value=session):
            with self.assertRaises(Unavailable):
                BoundedGoogleTransport().request('POST','https://oauth2.googleapis.com/token',body=b'x')
        self.assertFalse(session.trust_env)
        self.assertFalse(session.calls[0][2]['allow_redirects'])
        self.assertEqual(session.calls[0][2]['timeout'],(3,3))

    def test_size_timeout_and_malformed_json_are_bounded(self):
        import requests
        from cloud.identity.transport import BoundedGoogleTransport
        for response in (HTTPResponse(b'x'*65537),HTTPResponse(b'{}',headers={'Content-Length':'65537'}),
                         HTTPResponse(b'not json'),HTTPResponse(b'[]'),HTTPResponse(b'{}',status=500)):
            with self.subTest(status=response.status_code,length=len(response.data)):
                with patch('cloud.identity.transport.requests.Session',return_value=HTTPSession(response)):
                    with self.assertRaises(Unavailable):
                        BoundedGoogleTransport().request('POST','https://oauth2.googleapis.com/token')
        with patch('cloud.identity.transport.requests.Session',return_value=HTTPSession(error=requests.Timeout('private failure'))):
            with self.assertRaises(Unavailable) as error:
                BoundedGoogleTransport().request('POST','https://oauth2.googleapis.com/token')
            self.assertNotIn('private',str(error.exception))

    def test_total_stream_budget_and_certificate_expiry(self):
        from cloud.identity.transport import BoundedGoogleTransport
        session = HTTPSession(HTTPResponse(b'{"fixture":"certificate"}',headers={'Cache-Control':'max-age=1'}))
        with patch('cloud.identity.transport.requests.Session',return_value=session):
            transport = BoundedGoogleTransport()
            with patch('cloud.identity.transport.time.monotonic',return_value=10):
                one = transport.certificate_request('https://www.googleapis.com/oauth2/v1/certs')
                self.assertEqual(transport.certificate_request('https://www.googleapis.com/oauth2/v1/certs'),one)
            self.assertEqual(len(session.calls),1)
            session.error = OSError('private certificate error')
            with patch('cloud.identity.transport.time.monotonic',return_value=12),self.assertRaises(Unavailable):
                transport.certificate_request('https://www.googleapis.com/oauth2/v1/certs')
            self.assertEqual(len(session.calls),2)
        session = HTTPSession(HTTPResponse())
        with patch('cloud.identity.transport.requests.Session',return_value=session), \
             patch('cloud.identity.transport.time.monotonic',side_effect=[0,16]):
            with self.assertRaises(Unavailable):
                BoundedGoogleTransport().request('POST','https://oauth2.googleapis.com/token')
