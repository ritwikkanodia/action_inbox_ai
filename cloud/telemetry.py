"""Allowlisted structured operational data; no prompts, credentials or provider bodies."""
import json
import logging
import math
import re
from uuid import UUID

LOGGER = logging.getLogger('athena.cloud')
EVENTS = {'acceptance_committed','attempt_started','attempt_finished','lease_lost','recovery_finished','shutdown'}


def emit(event, **fields):
    if event not in EVENTS: raise ValueError('invalid_log_event')
    result={'event':event}
    for key,value in fields.items():
        if key in ('job_id','dispatch_id','epoch','connection_id') and isinstance(value,UUID): result[key]=str(value)
        elif key=='attempt' and type(value) is int and value>=0: result[key]=value
        elif key=='seconds' and type(value) in (int,float) and math.isfinite(value) and value>=0: result[key]=value
        elif key=='reason' and isinstance(value,str) and re.fullmatch('[a-z_]{1,64}',value): result[key]=value
        else: raise ValueError('unsafe_log_field')
    LOGGER.info(json.dumps(result,sort_keys=True))


def metrics(db):
    rows=db.read("""SELECT
        (SELECT coalesce(extract(epoch FROM clock_timestamp()-min(created_at)),0) FROM jobs WHERE state IN ('queued','retry_pending')) AS oldest_queued_seconds,
        (SELECT coalesce(extract(epoch FROM clock_timestamp()-min(created_at)),0) FROM outbox WHERE published_at IS NULL AND NOT quarantined) AS oldest_outbox_seconds,
        (SELECT count(*) FROM jobs WHERE state='running' AND lease_until<=clock_timestamp()) AS expired_leases,
        (SELECT count(*) FROM attempts) AS attempts,
        (SELECT coalesce(sum(greatest(attempts-1,0)),0) FROM jobs) AS retry_count,
        (SELECT count(*) FROM outbox WHERE quarantined) AS quarantined_dispatches,
        (SELECT count(*) FROM conversations WHERE reconciliation_hold) AS reconciliation_holds,
        (SELECT coalesce(extract(epoch FROM clock_timestamp()-min(updated_at)),0) FROM mailbox_checkpoints) AS ingestion_checkpoint_age_seconds""")
    return {key:float(value) if key.endswith('_seconds') else int(value) for key,value in rows[0].items()}
