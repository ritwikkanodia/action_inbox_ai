"""Forward-only, checksum-verified migrations; invoked explicitly, never per request."""
import hashlib
from pathlib import Path

MIGRATIONS = Path(__file__).with_name('migrations')


def migrate(db):
    with db.transaction() as tx:
        tx.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', (db.schema + ':migrate',))
        tx.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY, sha256 text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp())''')
        for path in sorted(MIGRATIONS.glob('[0-9]*.sql')):
            source = path.read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            old = tx.execute('SELECT sha256 FROM schema_migrations WHERE version=%s', (path.name,)).fetchone()
            if old:
                if old['sha256'] != digest:
                    raise ValueError('migration_checksum_mismatch')
                continue
            tx.execute(source.decode())
            tx.execute('INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)', (path.name, digest))
