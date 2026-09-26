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
