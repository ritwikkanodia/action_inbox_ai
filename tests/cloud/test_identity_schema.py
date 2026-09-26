"""Identity migration/configuration must preserve old work and reject unsafe inputs."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet, InvalidToken

from cloud.migrate import migrate, MIGRATIONS
from cloud.types import Actor, Submission
from cloud.work import Work
from tests.cloud.support import sandbox
from tests.cloud.identity_support import test_settings


class IdentitySchemaTests(unittest.TestCase):
    def test_repeat_migration_preserves_owners_and_creates_identity_boundary(self):
        with sandbox() as db:
            before = db.read('SELECT owner_id FROM owners ORDER BY owner_id')
            migrate(db)
            self.assertEqual(before, db.read('SELECT owner_id FROM owners ORDER BY owner_id'))
            # Without the migration this assertion fails, rather than an import error.
            self.assertIsNotNone(db.read("SELECT to_regclass('auth_admission') AS t")[0]['t'])
            self.assertEqual(db.read('SELECT seat_limit FROM auth_admission')[0]['seat_limit'], 50)
            self.assertEqual(db.read('SELECT * FROM auth_identities'), [])

    def test_upgrade_keeps_accepted_work_and_migration_checksums(self):
        old = SimpleNamespace(glob=lambda pattern: [p for p in sorted(MIGRATIONS.glob(pattern))
                                                   if p.name < '008_identity.sql'])
        with patch('cloud.migrate.MIGRATIONS', old), sandbox() as db:
            actor = Actor('alice')
            cid = Work(db).create_conversation(actor, 'chat')
            receipt = Work(db).accept(actor, Submission(cid, 1, uuid4(), 'preserve me'))
            versions = db.read('SELECT version,sha256 FROM schema_migrations ORDER BY version')
            with patch('cloud.migrate.MIGRATIONS', MIGRATIONS):
                migrate(db)
            self.assertIsNotNone(db.read("SELECT to_regclass('auth_sessions') AS t")[0]['t'])
            self.assertEqual(Work(db).snapshot(actor, cid)['messages'][0]['content'], 'preserve me')
            self.assertEqual(db.read('SELECT state FROM jobs WHERE id=%s', (receipt.job_id,))[0]['state'], 'queued')
            self.assertEqual(versions, db.read("SELECT version,sha256 FROM schema_migrations WHERE version<'008_identity.sql' ORDER BY version"))

    def test_identity_and_binding_foreign_keys_reject_cross_owner_records(self):
        with sandbox() as db:
            self.assertIsNotNone(db.read("SELECT to_regclass('cloud_conversation_bindings') AS t")[0]['t'])
            cid = Work(db).create_conversation(Actor('alice'), 'chat')
            with self.assertRaises(psycopg.errors.ForeignKeyViolation), db.transaction() as tx:
                tx.execute("INSERT INTO cloud_conversation_bindings VALUES ('bob','chat',%s)", (cid,))
            with self.assertRaises(psycopg.errors.CheckViolation), db.transaction() as tx:
                tx.execute('UPDATE auth_admission SET seat_limit=51')

    def test_explicit_configuration_and_secret_safe_repr(self):
        from cloud.identity.config import WebSettings, DatabaseSettings
        values = test_settings()
        config = WebSettings.from_mapping(values)
        self.assertEqual(config.public_origin, 'https://athena.example.invalid')
        self.assertEqual(config.database.schema, 'athena')
        self.assertEqual(DatabaseSettings.from_mapping(values), config.database)
        for sensitive in (values['ATHENA_GOOGLE_CLIENT_SECRET'], values['ATHENA_DATABASE_URL'], values['ATHENA_AUTH_FLOW_KEY']):
            self.assertNotIn(sensitive, repr(config))
        for key in values:
            with self.subTest(key=key), self.assertRaises(ValueError):
                WebSettings.from_mapping({k: v for k, v in values.items() if k != key})

    def test_insecure_or_ambiguous_public_origin_is_rejected(self):
        from cloud.identity.config import WebSettings
        for origin in ('http://example.invalid', 'https://u:p@example.invalid',
                       'https://example.invalid/path', 'https://example.invalid?x=1',
                       'https://example.invalid#x', 'https://example.invalid/',
                       'https://example.invalid\\evil', 'https://example.invalid\n',
                       'https://example.invalid:bad', 'https://'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                WebSettings.from_mapping(dict(test_settings(), ATHENA_PUBLIC_ORIGIN=origin))

    def test_database_requires_explicit_remote_tls_and_safe_schema(self):
        from cloud.identity.config import WebSettings
        values = test_settings()
        for key, value in (
            ('ATHENA_ENVIRONMENT', 'development'),
            ('ATHENA_DATABASE_SCHEMA', 'public; SELECT 1'),
            ('ATHENA_DATABASE_SCHEMA', 'x' * 64),
            ('ATHENA_DATABASE_URL', 'postgresql://a:b@db.invalid/app'),
            ('ATHENA_DATABASE_URL', 'host=/tmp dbname=app sslmode=verify-full'),
            ('ATHENA_DATABASE_URL', 'host=127.0.0.1 dbname=app sslmode=verify-full'),
            ('ATHENA_DATABASE_URL', 'host=db.invalid dbname=app sslmode=verify-full options=-csearch_path=public'),
            ('ATHENA_AUTH_FLOW_KEY', 'not-a-key'),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError) as error:
                WebSettings.from_mapping(dict(values, **{key: value}))
            self.assertNotIn(value, str(error.exception))

    def test_tokens_csrf_and_verifier_cipher_are_bound_and_secret_safe(self):
        from cloud.identity.crypto import new_token, hash_token, csrf_token, seal_verifier, open_verifier
        from cloud.identity.types import FlowClaim, FlowStart, IssuedSession, SessionProof, ProviderResponse
        token, second, key = new_token(), new_token(), Fernet.generate_key()
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertNotEqual(token, second)
        self.assertEqual(len(hash_token(token)), 64)
        self.assertNotEqual(csrf_token(token), csrf_token(second))
        sealed = seal_verifier(key, token)
        self.assertNotIn(token.encode(), sealed)
        self.assertEqual(open_verifier(key, sealed), token)
        with self.assertRaises(InvalidToken):
            open_verifier(Fernet.generate_key(), sealed)
        for raw in ('', 'é', 'x' * 4097):
            with self.subTest(raw=raw[:10]), self.assertRaises(ValueError):
                hash_token(raw)
        epoch = uuid4()
        proof = SessionProof('opaque', hash_token(token), epoch, uuid4())
        values = (FlowClaim(hash_token(token), hash_token(second), hash_token(second), token, epoch),
                  FlowStart(token, second, 'challenge', epoch),
                  IssuedSession(token, proof, csrf_token(token)),
                  ProviderResponse(200, token.encode(), {'private': token}))
        for value in values:
            self.assertNotIn(token, repr(value))
            self.assertNotIn(hash_token(token), repr(value))

