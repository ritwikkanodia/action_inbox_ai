"""Serialized beta seats and stable-subject admission; no provider/network calls."""
import re
from uuid import uuid4
from cloud.identity.crypto import hash_token, valid_token
from cloud.identity.guard import lock_runtime, validate_audit, audit
from cloud.identity.sessions import issue_session
from cloud.identity.types import AdmissionDenied, GoogleIdentity, FlowClaim
from cloud.types import Rejected, Unavailable


def normalize_email(value):
    if not isinstance(value, str):
        raise Rejected('invalid_email')
    value = value.strip().lower()
    if (not 3 <= len(value) <= 320 or not value.isascii()
            or not re.fullmatch(r"[^\s@\x00-\x1f\x7f]+@[^\s@\x00-\x1f\x7f]+", value)):
        raise Rejected('invalid_email')
    return value


def seat_count(tx):
    return tx.execute("""SELECT (SELECT count(*) FROM auth_identities) +
        (SELECT count(*) FROM auth_invites WHERE redeemed_owner_id IS NULL
         AND revoked_at IS NULL AND expires_at>clock_timestamp()) AS n""").fetchone()['n']


class Admission:
    def __init__(self, db):
        self.db = db

    def invite(self, email, actor, reason):
        email = normalize_email(email)
        validate_audit(actor, reason)
        with self.db.transaction() as tx:
            lock_runtime(tx)
            guard = tx.execute('SELECT seat_limit FROM auth_admission WHERE singleton FOR UPDATE').fetchone()
            old = tx.execute("""SELECT *,revoked_at IS NULL AND expires_at>clock_timestamp() AS live
                FROM auth_invites WHERE email=%s FOR UPDATE""", (email,)).fetchone()
            if old and old['redeemed_owner_id'] is not None:
                raise AdmissionDenied()
            if not (old and old['live']) and seat_count(tx) >= guard['seat_limit']:
                raise AdmissionDenied()
            if old:
                invite_id = old['id']
                tx.execute("""UPDATE auth_invites SET revoked_at=NULL,expires_at=clock_timestamp()+interval '7 days'
                    WHERE id=%s""", (invite_id,))
            else:
                invite_id = uuid4()
                tx.execute("""INSERT INTO auth_invites(id,email,expires_at)
                    VALUES (%s,%s,clock_timestamp()+interval '7 days')""", (invite_id, email))
            audit(tx, 'invitation_reserved', actor, reason)
        return invite_id

    def revoke_invite(self, invite_id, actor, reason):
        validate_audit(actor, reason)
        with self.db.transaction() as tx:
            lock_runtime(tx, allow_recovery=True)
            tx.execute('SELECT singleton FROM auth_admission WHERE singleton FOR UPDATE')
            result = tx.execute("""UPDATE auth_invites SET revoked_at=coalesce(revoked_at,clock_timestamp())
                WHERE id=%s AND redeemed_owner_id IS NULL RETURNING id""", (invite_id,)).fetchone()
            if not result:
                raise AdmissionDenied()
            audit(tx, 'invitation_revoked', actor, reason)

    def complete(self, identity, flow, previous_token=None):
        if (not isinstance(identity, GoogleIdentity) or not isinstance(flow, FlowClaim)
                or not isinstance(identity.subject, str) or not 1 <= len(identity.subject) <= 255
                or not identity.subject.isascii() or not isinstance(identity.name, str)
                or len(identity.name) > 200):
            raise AdmissionDenied()
        email = normalize_email(identity.email)
        previous_hash = hash_token(previous_token) if valid_token(previous_token) else None
        with self.db.transaction() as tx:
            runtime = lock_runtime(tx)
            if runtime['epoch'] != flow.epoch:
                raise Unavailable('stale_login')
            limit = tx.execute('SELECT seat_limit FROM auth_admission WHERE singleton FOR UPDATE').fetchone()['seat_limit']
            known = tx.execute("SELECT * FROM auth_identities WHERE provider='google' AND subject=%s",
                               (identity.subject,)).fetchone()
            previous = tx.execute('SELECT owner_id FROM auth_sessions WHERE token_hash=%s',
                                  (previous_hash,)).fetchone() if previous_hash else None
            owner_id = known['owner_id'] if known else str(uuid4())
            invite = None
            if not known:
                authoritative = email.endswith('@gmail.com') or (isinstance(identity.hosted_domain, str) and bool(identity.hosted_domain.strip()))
                invite = tx.execute("""SELECT *,revoked_at IS NULL AND expires_at>clock_timestamp() AS live
                    FROM auth_invites WHERE email=%s FOR UPDATE""", (email,)).fetchone()
                if (not authoritative or not invite or not invite['live']
                        or invite['redeemed_owner_id'] is not None or seat_count(tx) > limit):
                    raise AdmissionDenied()
                tx.execute('INSERT INTO owners(owner_id) VALUES (%s)', (owner_id,))
            owners = {owner_id}
            if previous:
                owners.add(previous['owner_id'])
            for target in sorted(owners):
                tx.execute('SELECT owner_id FROM owners WHERE owner_id=%s FOR UPDATE', (target,)).fetchone()
            owner = tx.execute('SELECT enabled FROM owners WHERE owner_id=%s', (owner_id,)).fetchone()
            if not owner or not owner['enabled']:
                raise AdmissionDenied()
            if known:
                current = tx.execute('SELECT * FROM auth_identities WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
                if not current or current['disabled_at'] is not None:
                    raise AdmissionDenied()
            stored_flow = tx.execute("""SELECT *,expires_at>clock_timestamp() AS live FROM auth_flows
                WHERE state_hash=%s FOR UPDATE""", (flow.state_hash,)).fetchone()
            if (not stored_flow or stored_flow['status'] != 'claimed' or not stored_flow['live']
                    or stored_flow['epoch'] != runtime['epoch'] or stored_flow['browser_hash'] != flow.browser_hash
                    or stored_flow['nonce_hash'] != flow.nonce_hash):
                raise AdmissionDenied()
            if not known:
                tx.execute("""INSERT INTO auth_identities(owner_id,provider,subject,email,display_name)
                    VALUES (%s,'google',%s,%s,%s)""", (owner_id, identity.subject, email, identity.name))
                tx.execute('UPDATE auth_invites SET redeemed_owner_id=%s WHERE id=%s', (owner_id, invite['id']))
            else:
                tx.execute('UPDATE auth_identities SET email=%s,display_name=%s WHERE owner_id=%s', (email, identity.name, owner_id))
            if previous_hash:
                tx.execute('UPDATE auth_sessions SET revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE token_hash=%s', (previous_hash,))
            issued = issue_session(tx, owner_id, runtime['epoch'])
            tx.execute("UPDATE auth_flows SET status='finished' WHERE state_hash=%s", (flow.state_hash,))
        return issued
