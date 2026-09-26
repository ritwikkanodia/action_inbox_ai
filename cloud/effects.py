"""Records intent/evidence only. No provider calls and no replay authorization."""
import re
from uuid import UUID
from psycopg.types.json import Jsonb

from cloud.leases import authorized, locked_job, transition
from cloud.types import Conflict, NotFound, Rejected, StaleLease
from cloud.work import digest


class Effects:
    def __init__(self, db): self.db = db

    def prepare(self, claim, operation_id, kind, fingerprint):
        if (not isinstance(operation_id, UUID) or not isinstance(kind, str) or not re.fullmatch('[a-z_]{1,64}', kind)
                or not isinstance(fingerprint, str) or not 1 <= len(fingerprint) <= 256):
            raise Rejected('invalid_effect')
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not authorized(locked, claim): raise StaleLease('stale_claim')
            old = tx.execute('SELECT * FROM effects WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
            if old:
                if (old['job_id'], old['owner_id'], old['attempt_fence'], old['epoch'], old['kind'], old['fingerprint']) != (
                        claim.job_id, claim.owner_id, claim.attempt, claim.epoch, kind, fingerprint):
                    raise Conflict('operation_conflict')
                return False
            tx.execute('''INSERT INTO effects(operation_id,job_id,owner_id,attempt_fence,epoch,kind,fingerprint)
                          VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                       (operation_id, claim.job_id, claim.owner_id, claim.attempt, claim.epoch, kind, fingerprint))
        return True

    def record(self, claim, operation_id, outcome, receipt):
        if outcome not in ('confirmed_succeeded', 'confirmed_no_effect', 'uncertain'):
            raise Rejected('invalid_effect_outcome')
        if (not isinstance(receipt, dict) or set(receipt) - {'provider_id', 'status'}
                or any(type(v) not in (str, int, bool, type(None)) or len(str(v)) > 256 for v in receipt.values())):
            raise Rejected('invalid_effect_receipt')
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not locked: raise NotFound('effect_not_found')
            effect = tx.execute('SELECT * FROM effects WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
            if not effect or (effect['job_id'], effect['owner_id'], effect['attempt_fence'], effect['epoch']) != (
                    claim.job_id, claim.owner_id, claim.attempt, claim.epoch):
                raise NotFound('effect_not_found')
            tx.execute('''INSERT INTO effect_receipts(operation_id,owner_id,receipt_hash,outcome,receipt)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                       (operation_id, claim.owner_id, digest([outcome, receipt]), outcome, Jsonb(receipt)))
            tx.execute('UPDATE effects SET state=%s,receipt=%s,recorded_at=clock_timestamp() WHERE operation_id=%s',
                       (outcome, Jsonb(receipt), operation_id))
            if outcome == 'uncertain' and authorized(locked, claim):
                transition(tx, locked, 'needs_reconciliation', 'effect_outcome_requires_review')
