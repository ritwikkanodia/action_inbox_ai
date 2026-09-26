"""Database authority, not broker delivery count, determines who may complete work."""
from datetime import timedelta
import random
import re

from cloud.config import DEFAULTS
from cloud.types import Claim, StaleLease
from cloud.work import ACTIVE, append_message, lock_owner, lock_conversation, notify


def locked_job(tx, job_id, connection_id=None):
    # This unlocked lookup determines lock order; immutable routing fields are
    # re-read under the owner lock before any decision is made.
    hint = tx.execute('SELECT * FROM jobs WHERE id=%s', (job_id,)).fetchone()
    if not hint:
        return None
    runtime, owner = lock_owner(tx, hint['owner_id'], require_enabled=False)
    selected_connection = hint.get('connection_id') or connection_id
    connection = tx.execute('SELECT * FROM connections WHERE owner_id=%s AND id=%s FOR UPDATE',
                            (hint['owner_id'], selected_connection)).fetchone() if selected_connection else None
    conversation = lock_conversation(tx, hint['owner_id'], hint['conversation_id']) if hint['conversation_id'] else None
    job = tx.execute('''SELECT j.*,clock_timestamp() AS now,
        (SELECT started_at FROM attempts a WHERE a.job_id=j.id AND a.fence=j.fence) AS started_at
        FROM jobs j WHERE j.id=%s FOR UPDATE''', (job_id,)).fetchone()
    job['_connection'] = connection
    return runtime, owner, conversation, job


def enabled(locked):
    runtime, owner, conversation, job = locked
    return (runtime['enabled'] and owner['enabled'] and job['epoch'] == runtime['epoch']
            and not job['cancel_requested'] and
            (not job.get('connection_id') or (job['_connection'] and job['_connection']['active']
                and job['_connection']['generation'] == job['connection_generation'])) and
            (not conversation or (conversation['generation'] == job['generation'] and not conversation['reconciliation_hold'])))


def authorized(locked, claim, config=DEFAULTS):
    if not locked or not enabled(locked):
        return False
    job = locked[3]
    return (job['state'] == 'running' and job['lease_until'] is not None
            and job['lease_until'] > job['now'] and job['expires_at'] > job['now']
            and job['started_at'] + timedelta(seconds=config.attempt_seconds) > job['now']
            and (job['id'], job['owner_id'], job['conversation_id'], job['generation'], job['fence'], job['epoch'])
            == (claim.job_id, claim.owner_id, claim.conversation_id, claim.generation, claim.attempt, claim.epoch))


def wake_next(tx, locked):
    runtime, owner, conversation, _ = locked
    if not conversation or conversation['reconciliation_hold'] or not runtime['enabled'] or not owner['enabled']:
        return
    job = tx.execute('''SELECT * FROM jobs WHERE owner_id=%s AND conversation_id=%s AND generation=%s
                         AND state=ANY(%s) ORDER BY job_order LIMIT 1''',
                     (owner['owner_id'], conversation['id'], conversation['generation'], list(ACTIVE))).fetchone()
    if job and job['state'] in ('queued', 'retry_pending') and job['epoch'] == runtime['epoch']:
        notify(tx, job['id'], runtime['epoch'])


def transition(tx, locked, state, reason=None, delay=0):
    job = locked[3]
    if state in ('cancelled', 'expired', 'failed', 'retry_pending') and tx.execute(
            'SELECT operation_id FROM effects WHERE job_id=%s LIMIT 1', (job['id'],)).fetchone():
        state, reason, delay = 'needs_reconciliation', 'effect_outcome_requires_review', 0
    if state == 'needs_reconciliation' and locked[2]:
        tx.execute('UPDATE conversations SET reconciliation_hold=true WHERE id=%s', (locked[2]['id'],))
        locked[2]['reconciliation_hold'] = True
    terminal = state not in ('queued', 'retry_pending', 'running')
    tx.execute('''UPDATE jobs SET state=%s,reason=%s,lease_until=NULL,worker_id=NULL,
                  due_at=clock_timestamp()+%s*interval '1 second',
                  finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END WHERE id=%s''',
               (state, reason, delay, terminal, job['id']))
    tx.execute('''UPDATE attempts SET finished_at=clock_timestamp(),outcome=%s,safe_reason=%s
                  WHERE job_id=%s AND fence=%s AND finished_at IS NULL''', (state, reason, job['id'], job['fence']))
    if state == 'retry_pending':
        notify(tx, job['id'], job['epoch'])
        tx.execute("UPDATE outbox SET due_at=clock_timestamp()+%s*interval '1 second' WHERE job_id=%s AND published_at IS NULL AND lease_until IS NULL", (delay, job['id']))
    elif terminal:
        wake_next(tx, locked)
    return state


def retry_or_fail(tx, locked, retryable, reason, config=DEFAULTS, jitter=random.random):
    job = locked[3]
    if job['expires_at'] <= job['now']:
        state, delay = 'expired', 0
    elif retryable and job['attempts'] < config.max_attempts:
        base = config.retry_seconds[min(max(job['attempts'] - 1, 0), 1)]
        state, delay = 'retry_pending', base * (1 + .2 * max(0, min(1, jitter())))
    else:
        state, delay = 'failed', 0
    return transition(tx, locked, state, reason, delay)


class Leases:
    def __init__(self, db, config=DEFAULTS, jitter=random.random):
        self.db, self.config, self.jitter = db, config, jitter

    def claim(self, job_id, worker_id, *, epoch=None):
        if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
            return None
        with self.db.transaction() as tx:
            locked = locked_job(tx, job_id)
            if not locked or not enabled(locked): return None
            if epoch is not None and epoch != locked[0]['epoch']: return None
            job = locked[3]
            if job['state'] not in ('queued', 'retry_pending'): return None
            if tx.execute('SELECT operation_id FROM effects WHERE job_id=%s LIMIT 1', (job_id,)).fetchone():
                transition(tx, locked, 'needs_reconciliation', 'effect_outcome_requires_review')
                return None
            if job['expires_at'] <= job['now']:
                transition(tx, locked, 'expired', 'deadline_expired')
                return None
            if job['attempts'] >= self.config.max_attempts:
                transition(tx, locked, 'failed', 'attempts_exhausted')
                return None
            if job['due_at'] > job['now']: return None
            if job['conversation_id'] and tx.execute('''SELECT id FROM jobs WHERE owner_id=%s
                    AND conversation_id=%s AND generation=%s AND job_order<%s AND state=ANY(%s) LIMIT 1''',
                    (job['owner_id'], job['conversation_id'], job['generation'], job['job_order'], list(ACTIVE))).fetchone():
                return None
            current = tx.execute('''UPDATE jobs SET state='running',fence=fence+1,attempts=attempts+1,
                worker_id=%s,reason=NULL,lease_until=least(expires_at,clock_timestamp()+%s*interval '1 second')
                WHERE id=%s RETURNING *''', (worker_id, self.config.lease_seconds, job_id)).fetchone()
            tx.execute('''INSERT INTO attempts(job_id,fence,owner_id,epoch,worker_id,lease_until)
                          VALUES (%s,%s,%s,%s,%s,%s)''',
                       (job_id, current['fence'], current['owner_id'], current['epoch'], worker_id, current['lease_until']))
            claim = Claim(job_id, current['owner_id'], current['conversation_id'], current['generation'],
                          current['fence'], current['epoch'], current['lease_until'])
        return claim

    def heartbeat(self, claim):
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not authorized(locked, claim, self.config): return False
            job = locked[3]
            until = tx.execute('''UPDATE jobs SET lease_until=least(expires_at,%s,
                clock_timestamp()+%s*interval '1 second') WHERE id=%s RETURNING lease_until''',
                (job['started_at'] + timedelta(seconds=self.config.attempt_seconds), self.config.lease_seconds, claim.job_id)).fetchone()['lease_until']
            tx.execute('UPDATE attempts SET lease_until=%s WHERE job_id=%s AND fence=%s', (until, claim.job_id, claim.attempt))
        return True

    def finish(self, claim, reply):
        if not isinstance(reply, str): return False
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not authorized(locked, claim, self.config) or not locked[2]: return False
            if tx.execute("SELECT operation_id FROM effects WHERE job_id=%s AND state IN ('prepared','uncertain') LIMIT 1", (claim.job_id,)).fetchone():
                transition(tx, locked, 'needs_reconciliation', 'effect_outcome_requires_review')
                return False
            append_message(tx, locked[2], 'assistant', reply, 'completion:' + str(claim.job_id), claim.job_id)
            transition(tx, locked, 'succeeded')
        return True

    def fail(self, claim, retryable, reason):
        safe_reason = reason if isinstance(reason, str) and re.fullmatch('[a-z_]{1,64}', reason) else 'execution_failed'
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if authorized(locked, claim, self.config):
                retry_or_fail(tx, locked, retryable, safe_reason, self.config, self.jitter)

    def context(self, claim):
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not authorized(locked, claim, self.config): raise StaleLease('stale_claim')
            job = locked[3]
            return tx.execute('''SELECT m.id,m.role,m.content,m.job_id,m.sequence FROM messages m
                LEFT JOIN jobs j ON j.id=m.job_id WHERE m.owner_id=%s AND m.conversation_id=%s AND m.generation=%s
                AND (m.role='notice' OR j.id=%s OR (j.state='succeeded' AND j.job_order<%s)) ORDER BY m.sequence''',
                (job['owner_id'], job['conversation_id'], job['generation'], job['id'], job['job_order'])).fetchall()

    def payload(self, claim):
        with self.db.transaction() as tx:
            locked = locked_job(tx, claim.job_id)
            if not authorized(locked, claim, self.config): raise StaleLease('stale_claim')
            return {'kind': locked[3]['kind'], 'input': locked[3]['input']}
