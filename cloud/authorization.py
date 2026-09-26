"""Fail-closed short-lived authority primitive; not a deployed tool server."""
from datetime import timedelta
import hashlib
import secrets

from cloud.leases import authorized, locked_job
from cloud.types import Capability, Claim, NotFound, Rejected, StaleLease


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Authorization:
    def __init__(self, db): self.db = db

    def issue(self, claim, connection_id, scopes):
        if not isinstance(scopes, frozenset) or not scopes or len(scopes) > 32 or any(not isinstance(s, str) or not 1 <= len(s) <= 256 for s in scopes):
            raise Rejected('invalid_scopes')
        token = secrets.token_urlsafe(32)
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id, connection_id)
            if not authorized(locked, claim): raise StaleLease('stale_claim')
            job = locked[3]
            if job['connection_id'] and job['connection_id'] != connection_id: raise Rejected('connection_mismatch')
            connection = job['_connection']
            if not connection or not connection['active']: raise NotFound('connection_not_found')
            if not scopes.issubset(connection['granted_scopes']): raise Rejected('scope_not_granted')
            expires = min(job['now'] + timedelta(seconds=60), job['lease_until'], job['expires_at'])
            tx.execute('''INSERT INTO capabilities(token_hash,owner_id,job_id,fence,epoch,connection_id,
                connection_generation,scopes,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (token_hash(token), claim.owner_id, claim.job_id, claim.attempt, claim.epoch, connection_id,
                 connection['generation'], sorted(scopes), expires))
        return Capability(token)

    def validate(self, capability, scope):
        try:
            if not isinstance(capability.token, str) or len(capability.token) != 43: return False
            with self.db.transaction() as tx:
                cap = tx.execute('SELECT * FROM capabilities WHERE token_hash=%s', (token_hash(capability.token),)).fetchone()
                if not cap: return False
                locked = locked_job(tx, cap['job_id'], cap['connection_id'])
                if not locked: return False
                job, connection = locked[3], locked[3]['_connection']
                claim = Claim(cap['job_id'], cap['owner_id'], job['conversation_id'], job['generation'],
                              cap['fence'], cap['epoch'], cap['expires_at'])
                return bool(authorized(locked, claim) and cap['revoked_at'] is None
                    and cap['expires_at'] > job['now'] and scope in cap['scopes']
                    and connection and connection['active'] and connection['id'] == cap['connection_id']
                    and connection['generation'] == cap['connection_generation'] and scope in connection['granted_scopes'])
        except Exception:
            return False
