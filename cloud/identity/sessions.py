"""Opaque browser sessions, checked/revoked in PostgreSQL rather than process memory."""
from uuid import uuid4
from psycopg import sql

from cloud.identity.crypto import new_token, valid_token, hash_token, csrf_token
from cloud.identity.guard import lock_runtime, lock_identity, lock_session, validate_audit, audit
from cloud.identity.types import AuthenticationRequired, SessionProof, IssuedSession
from cloud.types import NotFound


def issue_session(tx, owner_id, epoch):
    # Caller holds runtime/owner/identity locks. Expired sessions do not occupy slots.
    active = tx.execute("""SELECT token_hash FROM auth_sessions
        WHERE owner_id=%s AND epoch=%s AND revoked_at IS NULL
        AND expires_at>clock_timestamp() AND last_seen_at+interval '24 hours'>clock_timestamp()
        ORDER BY created_at,token_hash FOR UPDATE""", (owner_id, epoch)).fetchall()
    for old in active[:max(0, len(active) - 4)]:
        tx.execute('UPDATE auth_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s', (old['token_hash'],))
    token, context = new_token(), uuid4()
    digest = hash_token(token)
    tx.execute("""INSERT INTO auth_sessions(token_hash,owner_id,epoch,context_id,expires_at)
        VALUES (%s,%s,%s,%s,clock_timestamp()+interval '7 days')""", (digest, owner_id, epoch, context))
    return IssuedSession(token, SessionProof(owner_id, digest, epoch, context), csrf_token(token))


class Sessions:
    def __init__(self, db):
        self.db = db

    def resolve(self, raw_token):
        if not valid_token(raw_token):
            raise AuthenticationRequired()
        digest = hash_token(raw_token)
        # Unlocked immutable routing hint; lock_session rereads every authority field.
        hint = self.db.read('SELECT owner_id,epoch,context_id FROM auth_sessions WHERE token_hash=%s', (digest,))
        if not hint:
            raise AuthenticationRequired()
        proof = SessionProof(hint[0]['owner_id'], digest, hint[0]['epoch'], hint[0]['context_id'])
        with self.db.transaction() as tx:
            lock_session(tx, proof)
        return proof

    def revoke(self, proof, *, all_sessions=False):
        with self.db.transaction() as tx:
            lock_session(tx, proof, touch=False)
            if all_sessions:
                tx.execute('UPDATE auth_sessions SET revoked_at=clock_timestamp() WHERE owner_id=%s AND revoked_at IS NULL', (proof.owner_id,))
            else:
                tx.execute('UPDATE auth_sessions SET revoked_at=clock_timestamp() WHERE token_hash=%s', (proof.token_hash,))

    def disable(self, owner_id, actor, reason):
        self._operator(owner_id, actor, reason, disable=True)

    def revoke_owner(self, owner_id, actor, reason):
        self._operator(owner_id, actor, reason, disable=False)

    def _operator(self, owner_id, actor, reason, *, disable):
        validate_audit(actor, reason)
        if not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 128:
            raise NotFound('owner_not_found')
        with self.db.transaction() as tx:
            lock_runtime(tx, allow_recovery=True)
            owner = tx.execute('SELECT owner_id FROM owners WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
            identity = tx.execute('SELECT owner_id FROM auth_identities WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
            if not owner or not identity:
                raise NotFound('owner_not_found')
            if disable:
                tx.execute('UPDATE owners SET enabled=false WHERE owner_id=%s', (owner_id,))
                tx.execute('UPDATE auth_identities SET disabled_at=coalesce(disabled_at,clock_timestamp()) WHERE owner_id=%s', (owner_id,))
            tx.execute('UPDATE auth_sessions SET revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE owner_id=%s', (owner_id,))
            audit(tx, 'identity_disabled' if disable else 'sessions_revoked', actor, reason, owner_id)

    def purge(self):
        counts = {}
        # Static identifiers/conditions, bounded rows; never purge identities or seats.
        targets = (
            ('sessions', 'auth_sessions', "expires_at<=clock_timestamp() OR revoked_at IS NOT NULL OR last_seen_at+interval '24 hours'<=clock_timestamp()"),
            ('flows', 'auth_flows', 'expires_at<=clock_timestamp()'),
            ('limits', 'auth_limits', "bucket_start<clock_timestamp()-interval '20 minutes'"),
        )
        for name, table, condition in targets:
            with self.db.transaction() as tx:
                lock_runtime(tx)
                result = tx.execute(sql.SQL("""WITH doomed AS (
                    SELECT ctid FROM {} WHERE {} LIMIT 500 FOR UPDATE SKIP LOCKED)
                    DELETE FROM {} WHERE ctid IN (SELECT ctid FROM doomed)""").format(
                    sql.Identifier(table), sql.SQL(condition), sql.Identifier(table)))
                counts[name] = result.rowcount
        return counts
