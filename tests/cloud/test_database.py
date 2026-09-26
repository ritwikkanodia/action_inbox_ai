"""Real SQL checks: missing rollback, unsafe target, or changed schema must fail."""
import socket
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cloud.migrate import migrate
from tests.cloud.support import sandbox, validate_target


class DatabaseTests(unittest.TestCase):
    def test_transaction_rollback(self):
        with sandbox() as db:
            with self.assertRaisesRegex(RuntimeError, 'fixture crash'):
                with db.transaction() as tx:
                    tx.execute("INSERT INTO owners(owner_id) VALUES (%s)", ('rolled-back',))
                    raise RuntimeError('fixture crash')
            self.assertEqual(db.read("SELECT owner_id FROM owners WHERE owner_id=%s", ('rolled-back',)), [])

    def test_repeat_migration_and_changed_checksum(self):
        with sandbox() as db:
            before = db.read('SELECT * FROM schema_migrations ORDER BY version')
            migrate(db)
            self.assertEqual(before, db.read('SELECT * FROM schema_migrations ORDER BY version'))
            with db.transaction() as tx:
                tx.execute("UPDATE schema_migrations SET sha256='changed' WHERE version='001_core.sql'")
            with self.assertRaisesRegex(ValueError, 'migration_checksum_mismatch'):
                migrate(db)

    def test_cleanup_on_failure(self):
        with sandbox() as observer:
            with self.assertRaisesRegex(RuntimeError, 'fixture crash'):
                with sandbox() as doomed:
                    schema = doomed.schema
                    raise RuntimeError('fixture crash')
            self.assertEqual(observer.read('SELECT nspname FROM pg_namespace WHERE nspname=%s', (schema,)), [])

    def test_unsafe_targets_rejected(self):
        for dsn in ('postgresql://x:y@example.com/athena_verify',
                    'postgresql://x:y@127.0.0.1/personal',
                    'host=localhost dbname=athena_verify',
                    'host=/tmp dbname=athena_verify'):
            with self.subTest(dsn=dsn), self.assertRaises(ValueError):
                validate_target(dsn, 'not-the-runner')
        with self.assertRaises(ValueError):
            validate_target(os.environ['ATHENA_VERIFY_DSN'], 'not-the-runner')

    def test_failed_migration_rolls_back_schema_and_version(self):
        with sandbox() as db, tempfile.TemporaryDirectory(prefix='athena-migrations-') as directory:
            path = Path(directory)
            (path / '999_failure.sql').write_text('CREATE TABLE partial(id int); SELECT unknown_column;')
            with patch('cloud.migrate.MIGRATIONS', path):
                with self.assertRaises(Exception):
                    migrate(db)
            self.assertEqual(db.read("SELECT version FROM schema_migrations WHERE version='999_failure.sql'"), [])
            self.assertEqual(db.read("SELECT tablename FROM pg_tables WHERE schemaname=%s AND tablename='partial'", (db.schema,)), [])

    def test_provider_egress_denied(self):
        with self.assertRaisesRegex(OSError, 'test_egress_denied'):
            socket.create_connection(('example.com', 443))

    def test_database_isolation(self):
        with sandbox() as one, sandbox() as two:
            with one.transaction() as tx:
                tx.execute("INSERT INTO owners(owner_id) VALUES ('only-one')")
            self.assertEqual(two.read("SELECT owner_id FROM owners WHERE owner_id='only-one'"), [])
