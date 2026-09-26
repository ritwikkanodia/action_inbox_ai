"""Durable cancellation/reset and explicit, audited resolution; never undo claims."""
from uuid import uuid4
from cloud.leases import locked_job, transition, wake_next
from cloud.types import Conflict, NotFound, Rejected
from cloud.work import lock_conversation, lock_owner


class Control:
    def __init__(self, db): self.db = db

    def cancel(self, actor, job_id):
        with self.db.transaction() as tx:
            if not tx.execute('SELECT id FROM jobs WHERE id=%s AND owner_id=%s',
                              (job_id, actor.owner_id)).fetchone():
                raise NotFound('job_not_found')
            locked = locked_job(tx, job_id)
            if not locked or locked[3]['owner_id'] != actor.owner_id: raise NotFound('job_not_found')
            if locked[3]['state'] in ('queued', 'running', 'retry_pending'):
                tx.execute('UPDATE jobs SET cancel_requested=true WHERE id=%s', (job_id,))
                transition(tx, locked, 'cancelled', 'cancel_requested')
            state = tx.execute('SELECT state FROM jobs WHERE id=%s', (job_id,)).fetchone()['state']
        return state

    def reset(self, actor, conversation_id, generation):
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id, require_enabled=False)
            # Preserve connection-before-conversation ordering for bound jobs.
            tx.execute('''SELECT id FROM connections WHERE owner_id=%s AND id IN
                (SELECT connection_id FROM jobs WHERE conversation_id=%s) ORDER BY id FOR UPDATE''',
                       (actor.owner_id, conversation_id)).fetchall()
            conversation = lock_conversation(tx, actor.owner_id, conversation_id)
            if type(generation) is not int or generation != conversation['generation']: raise Conflict('stale_generation')
            tx.execute('''UPDATE conversations SET generation=generation+1,next_message_seq=1,next_job_order=1
                          WHERE id=%s''', (conversation_id,))
            jobs = tx.execute('''SELECT id FROM jobs WHERE owner_id=%s AND conversation_id=%s
                AND state IN ('queued','running','retry_pending') ORDER BY job_order''', (actor.owner_id, conversation_id)).fetchall()
            for job in jobs:
                locked = locked_job(tx, job['id'])
                tx.execute('UPDATE jobs SET cancel_requested=true WHERE id=%s', (job['id'],))
                transition(tx, locked, 'cancelled', 'conversation_reset')
        return generation + 1

    def reconcile(self, actor, job_id, decision, reason):
        if decision not in ('confirmed_succeeded', 'closed_without_retry') or not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise Rejected('invalid_reconciliation')
        with self.db.transaction() as tx:
            locked = locked_job(tx, job_id)
            if not locked or locked[3]['owner_id'] != actor.owner_id: raise NotFound('job_not_found')
            if locked[3]['state'] != 'needs_reconciliation': raise Conflict('not_awaiting_reconciliation')
            tx.execute('INSERT INTO reconciliations(id,job_id,owner_id,actor,decision,reason) VALUES (%s,%s,%s,%s,%s,%s)',
                       (uuid4(), job_id, actor.owner_id, actor.owner_id, decision, reason.strip()))
            tx.execute('''UPDATE jobs SET state=%s,reason='reviewed_without_replay',finished_at=clock_timestamp(),
                lease_until=NULL,cancel_requested=true WHERE id=%s''',
                       ('succeeded' if decision == 'confirmed_succeeded' else 'cancelled', job_id))
            conversation = locked[2]
            if conversation:
                outstanding = tx.execute("SELECT id FROM jobs WHERE conversation_id=%s AND state='needs_reconciliation' LIMIT 1", (conversation['id'],)).fetchone()
                if not outstanding:
                    tx.execute('UPDATE conversations SET reconciliation_hold=false WHERE id=%s', (conversation['id'],))
                    conversation['reconciliation_hold'] = False
                    wake_next(tx, locked)
