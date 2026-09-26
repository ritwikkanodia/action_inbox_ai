"""Bounded recovery scans. Each candidate is rechecked under ordered locks."""
from collections import Counter
from datetime import timedelta
import random

from cloud.config import DEFAULTS
from cloud.leases import enabled, locked_job, retry_or_fail, transition
from cloud.work import ACTIVE, notify


class Recovery:
    def __init__(self, db, config=DEFAULTS, jitter=random.random):
        self.db, self.config, self.jitter = db, config, jitter

    def sweep(self):
        # Bound actionable candidates, not the first 100 healthy jobs; otherwise
        # an old healthy queue can permanently starve later expired work.
        candidates = self.db.read("""SELECT j.id FROM jobs j
            JOIN owners o ON o.owner_id=j.owner_id
            LEFT JOIN conversations c ON c.id=j.conversation_id
            LEFT JOIN attempts a ON a.job_id=j.id AND a.fence=j.fence
            WHERE j.state IN ('queued','retry_pending','running')
            AND j.epoch=(SELECT epoch FROM runtime WHERE singleton AND enabled)
            AND (j.expires_at<=clock_timestamp() OR j.cancel_requested OR NOT o.enabled
                 OR c.generation<>j.generation
                 OR (j.state='running' AND (j.lease_until<=clock_timestamp()
                     OR a.started_at+%s*interval '1 second'<=clock_timestamp()))
                 OR (j.state IN ('queued','retry_pending') AND j.attempts>=%s))
            ORDER BY j.due_at LIMIT 100""", (self.config.attempt_seconds, self.config.max_attempts))
        counts = Counter()
        for candidate in candidates:
            with self.db.transaction() as tx:
                locked = locked_job(tx, candidate['id'])
                if not locked or not locked[0]['enabled']: continue
                job, conversation = locked[3], locked[2]
                if job['state'] not in ('queued', 'retry_pending', 'running'): continue
                if job['epoch'] != locked[0]['epoch']: continue
                if not locked[1]['enabled'] or job['cancel_requested'] or (conversation and conversation['generation'] != job['generation']):
                    counts[transition(tx, locked, 'cancelled', 'authority_revoked')] += 1
                elif job['expires_at'] <= job['now']:
                    counts[transition(tx, locked, 'expired', 'deadline_expired')] += 1
                elif job['state'] == 'running' and (job['lease_until'] <= job['now'] or
                        job['started_at'] + timedelta(seconds=self.config.attempt_seconds) <= job['now']):
                    counts[retry_or_fail(tx, locked, True, 'lease_expired', self.config, self.jitter)] += 1
                elif job['state'] in ('queued', 'retry_pending') and job['attempts'] >= self.config.max_attempts:
                    counts[transition(tx, locked, 'failed', 'attempts_exhausted')] += 1
        counts.update(self.redispatch())
        return dict(counts)

    def redispatch(self):
        counts = Counter()
        candidates = self.db.read('''SELECT j.id FROM jobs j JOIN owners u ON u.owner_id=j.owner_id AND u.enabled
            JOIN runtime r ON r.singleton AND r.enabled AND r.epoch=j.epoch
            LEFT JOIN conversations c ON c.id=j.conversation_id
            WHERE j.state IN ('queued','retry_pending') AND NOT j.cancel_requested AND j.due_at<=clock_timestamp()
            AND j.expires_at>clock_timestamp() AND (c.id IS NULL OR (c.generation=j.generation AND NOT c.reconciliation_hold))
            AND NOT EXISTS(SELECT 1 FROM jobs p WHERE p.conversation_id=j.conversation_id AND p.generation=j.generation
                AND p.job_order<j.job_order AND p.state=ANY(%s))
            AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.job_id=j.id AND
                (o.lease_until>clock_timestamp() OR greatest(o.created_at,o.published_at)>clock_timestamp()-%s*interval '1 second'))
            ORDER BY j.due_at LIMIT 100''', (list(ACTIVE), self.config.redispatch_seconds))
        for candidate in candidates:
            with self.db.transaction() as tx:
                locked = locked_job(tx, candidate['id'])
                if not locked or not enabled(locked): continue
                job = locked[3]
                if job['state'] not in ('queued','retry_pending') or job['due_at'] > job['now']: continue
                if job['conversation_id'] and tx.execute('''SELECT id FROM jobs WHERE conversation_id=%s AND generation=%s
                    AND job_order<%s AND state=ANY(%s)''', (job['conversation_id'], job['generation'], job['job_order'], list(ACTIVE))).fetchone(): continue
                if tx.execute('SELECT id FROM outbox WHERE job_id=%s AND quarantined LIMIT 1', (job['id'],)).fetchone():
                    counts[transition(tx, locked, 'failed', 'poisoned_dispatch')] += 1
                    continue
                recent = tx.execute('''SELECT id FROM outbox WHERE job_id=%s AND
                    (lease_until>clock_timestamp() OR greatest(created_at,published_at)>clock_timestamp()-%s*interval '1 second') LIMIT 1''',
                    (job['id'], self.config.redispatch_seconds)).fetchone()
                if not recent:
                    notify(tx, job['id'], job['epoch'])
                    counts['redispatched'] += 1
        return dict(counts)
