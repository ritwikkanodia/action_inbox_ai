"""At-least-once wake-ups. Transport is always outside a database transaction."""
from cloud.config import DEFAULTS
from cloud.leases import Leases, enabled, locked_job, transition
from cloud.types import Envelope


class PoisonEnvelope(ValueError):
    pass


class Dispatcher:
    def __init__(self, db, send, config=DEFAULTS):
        self.db, self.send, self.config = db, send, config

    def once(self):
        candidates = self.db.read('''SELECT o.id,o.job_id FROM outbox o JOIN jobs j ON j.id=o.job_id
            JOIN runtime r ON r.singleton AND r.enabled AND r.epoch=o.epoch
            JOIN owners u ON u.owner_id=j.owner_id AND u.enabled
            LEFT JOIN conversations c ON c.id=j.conversation_id
            WHERE o.published_at IS NULL AND NOT o.quarantined AND o.due_at<=clock_timestamp()
            AND (o.lease_until IS NULL OR o.lease_until<=clock_timestamp())
            AND j.state IN ('queued','retry_pending') AND NOT j.cancel_requested AND j.epoch=r.epoch
            AND (c.id IS NULL OR (c.generation=j.generation AND NOT c.reconciliation_hold))
            ORDER BY o.due_at LIMIT 100''')
        envelope = None
        for candidate in candidates:
            with self.db.transaction() as tx:
                locked = locked_job(tx, candidate['job_id'])
                if not locked or not enabled(locked) or locked[3]['state'] not in ('queued', 'retry_pending'): continue
                row = tx.execute('''UPDATE outbox SET fence=fence+1,
                    lease_until=clock_timestamp()+%s*interval '1 second'
                    WHERE id=%s AND published_at IS NULL AND NOT quarantined AND epoch=%s
                    AND due_at<=clock_timestamp() AND (lease_until IS NULL OR lease_until<=clock_timestamp())
                    RETURNING *''', (self.config.lease_seconds, candidate['id'], locked[0]['epoch'])).fetchone()
                if not row: continue
                envelope = Envelope(row['version'], row['id'], row['job_id'], row['epoch'])
                fence = row['fence']
            break
        if envelope is None: return False
        try:
            self.send(envelope)
        except Exception:
            # Lost transport acknowledgement is not a failed job. Same ID retries.
            with self.db.transaction() as tx:
                tx.execute("""UPDATE outbox SET lease_until=NULL,due_at=clock_timestamp()+interval '5 seconds'
                    WHERE id=%s AND fence=%s AND lease_until>clock_timestamp()""", (envelope.dispatch_id, fence))
        else:
            with self.db.transaction() as tx:
                runtime = tx.execute('SELECT * FROM runtime WHERE singleton FOR SHARE').fetchone()
                if runtime['enabled'] and runtime['epoch'] == envelope.epoch:
                    tx.execute('''UPDATE outbox SET published_at=clock_timestamp(),lease_until=NULL
                        WHERE id=%s AND fence=%s AND lease_until>clock_timestamp() AND NOT quarantined''',
                               (envelope.dispatch_id, fence))
        return True


class Receiver:
    def __init__(self, db): self.db = db

    def handle(self, envelope, worker_id):
        poisoned = False
        with self.db.transaction() as tx:
            locked = locked_job(tx, envelope.job_id)
            if not locked or envelope.epoch != locked[0]['epoch'] or envelope.epoch != locked[3]['epoch']:
                return None
            row = tx.execute('SELECT * FROM outbox WHERE id=%s AND job_id=%s AND epoch=%s FOR UPDATE',
                             (envelope.dispatch_id, envelope.job_id, envelope.epoch)).fetchone()
            if not row or row['quarantined']: return None
            if type(envelope.version) is not int or envelope.version != 1 or row['version'] != 1:
                tx.execute('UPDATE outbox SET quarantined=true,lease_until=NULL WHERE id=%s', (row['id'],))
                if locked[3]['state'] in ('queued', 'retry_pending'):
                    transition(tx, locked, 'failed', 'poisoned_dispatch')
                poisoned = True
        if poisoned: raise PoisonEnvelope('unsupported_envelope')
        # An epoch can rotate between the two transactions; compare again at claim.
        return Leases(self.db).claim(envelope.job_id, worker_id, epoch=envelope.epoch)
