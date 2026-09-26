"""Synthetic identity configuration only; never reads local credentials."""
from cryptography.fernet import Fernet
from uuid import uuid4

from cloud.identity.crypto import hash_token, new_token
from cloud.identity.types import FlowClaim, GoogleIdentity


def test_settings():
    return {
        'ATHENA_DATABASE_URL': 'postgresql://fixture:synthetic@db.example.invalid/athena?sslmode=verify-full',
        'ATHENA_DATABASE_SCHEMA': 'athena',
        'ATHENA_ENVIRONMENT': 'staging',
        'ATHENA_PUBLIC_ORIGIN': 'https://athena.example.invalid',
        'ATHENA_GOOGLE_CLIENT_ID': 'fixture.apps.googleusercontent.com',
        'ATHENA_GOOGLE_CLIENT_SECRET': 'synthetic-client-secret',
        'ATHENA_AUTH_FLOW_KEY': Fernet.generate_key().decode(),
    }


def seed_claim(db):
    state, browser, nonce, verifier = (new_token() for _ in range(4))
    epoch = db.read('SELECT epoch FROM runtime')[0]['epoch']
    with db.transaction() as tx:
        tx.execute('''INSERT INTO auth_flows(state_hash,browser_hash,nonce_hash,epoch,status,expires_at)
            VALUES (%s,%s,%s,%s,'claimed',clock_timestamp()+interval '10 minutes')''',
            (hash_token(state), hash_token(browser), hash_token(nonce), epoch))
    return FlowClaim(hash_token(state), hash_token(browser), hash_token(nonce), verifier, epoch)


def admit_fixture(db, email='alice@gmail.com', subject='alice-sub'):
    from cloud.identity.admission import Admission
    admission = Admission(db)
    if not db.read("SELECT owner_id FROM auth_identities WHERE provider='google' AND subject=%s", (subject,)):
        admission.invite(email, 'fixture-operator', 'synthetic admission')
    return admission.complete(GoogleIdentity(subject, email, email.split('@')[0].title(), None), seed_claim(db))


class FakeGoogleTransport:
    """External HTTP boundary only: token verification remains real."""
    def __init__(self, token, certificate):
        self.token, self.certificate = token, certificate
        self.calls = []
        self.failure = None

    def request(self, method, url, *, body=None, headers=None):
        import json
        from cloud.identity.types import ProviderResponse
        self.calls.append((method, url, body))
        if self.failure:
            raise self.failure
        if (method, url) == ('POST', 'https://oauth2.googleapis.com/token'):
            value = {'access_token':'synthetic-access','token_type':'Bearer','expires_in':3600,
                     'scope':'openid email profile','id_token':self.token}
        elif (method, url) == ('GET', 'https://www.googleapis.com/oauth2/v1/certs'):
            value = {'fixture':self.certificate}
        else:
            raise AssertionError('unexpected provider endpoint')
        return ProviderResponse(200, json.dumps(value).encode(), {'Cache-Control':'max-age=3600'})

    def certificate_request(self, url, method='GET', body=None, headers=None, timeout=None):
        return self.request(method, url, body=body, headers=headers)


class SignedGoogleFixture:
    def __init__(self):
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'synthetic-google')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
                .sign(self.key, hashes.SHA256()))
        self.certificate = cert.public_bytes(serialization.Encoding.PEM).decode()

    def valid_claims(self, subject='alice-sub', email='alice@gmail.com', nonce='expected'):
        import time
        now = int(time.time())
        return {'iss':'https://accounts.google.com','aud':'fixture.apps.googleusercontent.com',
                'sub':subject,'email':email,'email_verified':True,'name':'Alice',
                'iat':now-1,'exp':now+3600,'nonce':nonce}

    def token(self, claims):
        from cryptography.hazmat.primitives import serialization
        from google.auth import crypt, jwt
        private = self.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        return jwt.encode(crypt.RSASigner.from_string(private, key_id='fixture'), claims).decode()

    def adapter_and_claim(self, claims, *, expected_nonce):
        from cloud.identity.config import WebSettings
        from cloud.identity.google import GoogleAdapter
        transport = FakeGoogleTransport(self.token(claims), self.certificate)
        claim = FlowClaim(hash_token(new_token()), hash_token(new_token()), hash_token(expected_nonce),
                          new_token(), uuid4())
        return GoogleAdapter(WebSettings.from_mapping(test_settings()), transport), claim
