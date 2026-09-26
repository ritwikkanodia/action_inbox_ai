"""Bounded recovery scans. Each candidate is rechecked under ordered locks."""
from collections import Counter
from datetime import timedelta
import random

from cloud.config import DEFAULTS
from cloud.leases import locked_job, retry_or_fail, transition


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
                    transition(tx, locked, 'cancelled', 'authority_revoked')
                    counts['cancelled'] += 1
                elif job['expires_at'] <= job['now']:
                    transition(tx, locked, 'expired', 'deadline_expired')
                    counts['expired'] += 1
                elif job['state'] == 'running' and (job['lease_until'] <= job['now'] or
                        job['started_at'] + timedelta(seconds=self.config.attempt_seconds) <= job['now']):
                    counts[retry_or_fail(tx, locked, True, 'lease_expired', self.config, self.jitter)] += 1
                elif job['state'] in ('queued', 'retry_pending') and job['attempts'] >= self.config.max_attempts:
                    transition(tx, locked, 'failed', 'attempts_exhausted')
                    counts['failed'] += 1
        return dict(counts)
