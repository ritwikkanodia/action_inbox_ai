"""Connection generations and protected references, never raw OAuth credentials."""
import re
from uuid import uuid4

from cloud.leases import locked_job, transition
from cloud.types import NotFound, Rejected, Unavailable
from cloud.work import lock_owner


def validate_reference(reference):
    if not isinstance(reference, str) or not re.fullmatch(r'(fixture|keyvault):[A-Za-z0-9/._:\-]{1,400}', reference):
        raise Rejected('invalid_credential_reference')


def revoke_work(tx, connection_id):
    tx.execute('UPDATE capabilities SET revoked_at=clock_timestamp() WHERE connection_id=%s AND revoked_at IS NULL', (connection_id,))
    jobs = tx.execute('''SELECT j.id FROM jobs j WHERE j.state IN ('queued','running','retry_pending')
        AND (j.connection_id=%s OR EXISTS(SELECT 1 FROM capabilities c WHERE c.job_id=j.id AND c.connection_id=%s))
        ORDER BY j.id''', (connection_id, connection_id)).fetchall()
    for job in jobs:
        locked = locked_job(tx, job['id'])
        tx.execute('UPDATE jobs SET cancel_requested=true WHERE id=%s', (job['id'],))
        transition(tx, locked, 'cancelled', 'connection_revoked')


class Connections:
    def __init__(self, db): self.db = db

    def connect(self, actor, account, credential_ref):
        validate_reference(credential_ref)
        if not isinstance(account, str) or not account.strip() or len(account) > 320:
            raise Rejected('invalid_account')
        account = account.strip().casefold()
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id)
            old = tx.execute('SELECT * FROM connections WHERE owner_id=%s AND account=%s FOR UPDATE', (actor.owner_id, account)).fetchone()
            cid = old['id'] if old else uuid4()
            if old:
                tx.execute("""UPDATE connections SET generation=generation+1,active=true,credential_ref=%s,
                    granted_scopes='{}',updated_at=clock_timestamp() WHERE id=%s""", (credential_ref, cid))
                revoke_work(tx, cid)
            else:
                tx.execute('INSERT INTO connections(id,owner_id,account,credential_ref) VALUES (%s,%s,%s,%s)',
                           (cid, actor.owner_id, account, credential_ref))
        return cid

    def generation(self, actor, connection_id):
        rows = self.db.read('SELECT generation FROM connections WHERE owner_id=%s AND id=%s', (actor.owner_id, connection_id))
        if not rows: raise NotFound('connection_not_found')
        return rows[0]['generation']

    def disconnect(self, actor, connection_id):
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id, require_enabled=False)
            row = tx.execute('SELECT * FROM connections WHERE owner_id=%s AND id=%s FOR UPDATE', (actor.owner_id, connection_id)).fetchone()
            if not row: raise NotFound('connection_not_found')
            tx.execute("""UPDATE connections SET active=false,generation=generation+1,credential_ref=NULL,
                          granted_scopes='{}',updated_at=clock_timestamp() WHERE id=%s""", (connection_id,))
            revoke_work(tx, connection_id)

    def refresh_reference(self, actor, connection_id, generation, credential_ref):
        validate_reference(credential_ref)
        try:
            with self.db.transaction() as tx:
                lock_owner(tx, actor.owner_id)
                changed = tx.execute('''UPDATE connections SET credential_ref=%s,updated_at=clock_timestamp()
                    WHERE id=%s AND owner_id=%s AND active AND generation=%s RETURNING id''',
                    (credential_ref, connection_id, actor.owner_id, generation)).fetchone()
            return changed is not None
        except (Unavailable, NotFound):
            return False
