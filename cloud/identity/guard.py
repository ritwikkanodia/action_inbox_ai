"""Ordered identity locks shared by login, revocation and request transactions."""
from uuid import uuid4
from contextlib import contextmanager
from cloud.identity.types import AuthenticationRequired, SessionProof
from cloud.types import Rejected, Unavailable


def lock_runtime(tx):
    runtime = tx.execute('SELECT * FROM runtime WHERE singleton FOR SHARE').fetchone()
    if not runtime:
        raise Unavailable('runtime_unavailable')
    held = tx.execute("""SELECT 1 FROM recovery_audit b WHERE b.epoch=%s AND b.action='begin'
        AND NOT EXISTS (SELECT 1 FROM recovery_audit r WHERE r.epoch=b.epoch AND r.action='resume')
        LIMIT 1""", (runtime['epoch'],)).fetchone()
    if held:
        raise Unavailable('recovery_hold')
    return runtime


def lock_identity(tx, owner_id):
    owner = tx.execute('SELECT * FROM owners WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
    identity = tx.execute('SELECT * FROM auth_identities WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
    if not owner or not identity or not owner['enabled'] or identity['disabled_at'] is not None:
        raise AuthenticationRequired()
    return identity


def lock_session(tx, proof, *, touch=True):
    if not isinstance(proof, SessionProof):
        raise AuthenticationRequired()
    runtime = lock_runtime(tx)
    lock_identity(tx, proof.owner_id)
    session = tx.execute("""SELECT *,expires_at>clock_timestamp()
        AND last_seen_at+interval '24 hours'>clock_timestamp() AS live
        FROM auth_sessions WHERE token_hash=%s AND owner_id=%s FOR UPDATE""",
        (proof.token_hash, proof.owner_id)).fetchone()
    if (not session or not session['live'] or session['revoked_at'] is not None
            or session['epoch'] != runtime['epoch'] or session['epoch'] != proof.epoch
            or session['context_id'] != proof.context_id):
        raise AuthenticationRequired()
    if touch:
        tx.execute('UPDATE auth_sessions SET last_seen_at=clock_timestamp() WHERE token_hash=%s', (proof.token_hash,))
    return session


def validate_audit(actor, reason):
    if (not isinstance(actor, str) or not 1 <= len(actor.strip()) <= 128
            or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500
            or any(ord(c) < 32 for c in actor + reason)):
        raise Rejected('audit_reason_required')


def audit(tx, action, actor, reason, owner_id=None):
    tx.execute('INSERT INTO auth_audit(id,action,owner_id,actor,reason) VALUES (%s,%s,%s,%s,%s)',
               (uuid4(), action, owner_id, actor.strip(), reason.strip()))


class RequestDatabase:
    """Revalidate authority inside the very transaction doing the work."""
    def __init__(self, db, proof):
        self.db, self.proof = db, proof

    @contextmanager
    def transaction(self):
        proof = self.proof()
        with self.db.transaction() as tx:
            lock_session(tx, proof)
            yield tx

    def read(self, query, params=()):
        with self.transaction() as tx:
            return tx.execute(query, params).fetchall()
