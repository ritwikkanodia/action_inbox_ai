"""Fenced, atomic normalized-page ingestion; no Gmail client or model imports."""
from dataclasses import asdict
import json
from uuid import uuid4, uuid5
from psycopg.types.json import Jsonb

from cloud.config import DEFAULTS
from cloud.types import Conflict, MailPage, PollClaim, Rejected, StaleLease, Unavailable
from cloud.work import ACTIVE, digest, lock_owner, notify


def valid_page(page):
    def key(value): return isinstance(value, str) and 0 < len(value) <= 2048
    if not isinstance(page, MailPage) or not key(page.page_key) or not key(page.expected_page_key): return False
    if page.next_page_key is not None and not key(page.next_page_key): return False
    if page.next_page_key is None and not key(page.final_cursor): return False
    if page.baseline_cursor is not None and not key(page.baseline_cursor): return False
    if page.final_cursor is not None and not key(page.final_cursor): return False
    if not isinstance(page.events, tuple) or len(page.events) > 100: return False
    for item in page.events:
        if not isinstance(item, dict) or set(item) != {'message_id','event_type','occurrence_id','thread_id','content'}: return False
        if item['event_type'] not in ('received','label_added','label_removed','deleted'): return False
        if any(not isinstance(v, str) for v in item.values()): return False
        if any(not item[k] or len(item[k]) > 512 for k in ('message_id','thread_id','occurrence_id')): return False
        if len(item['content'].encode()) > DEFAULTS.text_bytes: return False
    return len(json.dumps(asdict(page), ensure_ascii=False).encode()) <= 1048576


def locked_poll(tx, claim):
    runtime, owner = lock_owner(tx, claim.owner_id, require_enabled=False)
    connection = tx.execute('SELECT * FROM connections WHERE owner_id=%s AND id=%s FOR UPDATE', (claim.owner_id, claim.connection_id)).fetchone()
    checkpoint = tx.execute('SELECT *,clock_timestamp() AS now FROM mailbox_checkpoints WHERE owner_id=%s AND connection_id=%s FOR UPDATE',
                            (claim.owner_id, claim.connection_id)).fetchone()
    return runtime, owner, connection, checkpoint


def authorized_poll(locked, claim):
    runtime, owner, connection, checkpoint = locked
    return bool(runtime['enabled'] and owner['enabled'] and connection and connection['active'] and checkpoint
        and connection['generation'] == claim.generation == checkpoint['generation']
        and runtime['epoch'] == claim.epoch == checkpoint['epoch']
        and checkpoint['fence'] == claim.fence and checkpoint['chain_id'] == claim.chain_id
        and checkpoint['lease_until'] and checkpoint['lease_until'] > checkpoint['now'])


def restart_backfill(tx, connection_id):
    tx.execute("""UPDATE mailbox_checkpoints SET chain_id=%s,fence=fence+1,lease_until=NULL,
        next_page_key='start',baseline_cursor=NULL,mode='backfill',backfill_since=clock_timestamp()-interval '3 days',
        resync_required=true,ingestion_error='bounded_three_day_resync',updated_at=clock_timestamp()
        WHERE connection_id=%s""", (uuid4(), connection_id))


class Gmail:
    def __init__(self, db): self.db = db

    def claim_poll(self, actor, connection_id):
        try:
            with self.db.transaction() as tx:
                runtime, _ = lock_owner(tx, actor.owner_id)
                connection = tx.execute('SELECT * FROM connections WHERE owner_id=%s AND id=%s FOR UPDATE', (actor.owner_id, connection_id)).fetchone()
                if not connection or not connection['active']: return None
                checkpoint = tx.execute('SELECT *,clock_timestamp() AS now FROM mailbox_checkpoints WHERE connection_id=%s FOR UPDATE', (connection_id,)).fetchone()
                if not checkpoint:
                    tx.execute("""INSERT INTO mailbox_checkpoints(connection_id,owner_id,generation,epoch,chain_id,next_page_key,backfill_since)
                        VALUES (%s,%s,%s,%s,%s,'start',clock_timestamp()-interval '3 days')""",
                        (connection_id, actor.owner_id, connection['generation'], runtime['epoch'], uuid4()))
                elif checkpoint['generation'] != connection['generation']:
                    restart_backfill(tx, connection_id)
                    tx.execute('UPDATE mailbox_checkpoints SET generation=%s,cursor=NULL WHERE connection_id=%s', (connection['generation'], connection_id))
                elif checkpoint['lease_until'] and checkpoint['lease_until'] > checkpoint['now']:
                    return None
                elif checkpoint['next_page_key'] is None:
                    tx.execute("UPDATE mailbox_checkpoints SET chain_id=%s,next_page_key='start',baseline_cursor=NULL WHERE connection_id=%s", (uuid4(), connection_id))
                row = tx.execute("""UPDATE mailbox_checkpoints SET fence=fence+1,epoch=%s,
                    lease_until=clock_timestamp()+interval '60 seconds' WHERE connection_id=%s RETURNING *""", (runtime['epoch'], connection_id)).fetchone()
                result = PollClaim(connection_id, actor.owner_id, row['generation'], row['fence'], row['epoch'], row['chain_id'])
            return result
        except Unavailable:
            return None

    def ingest_page(self, claim, page):
        valid = valid_page(page)
        fingerprint = digest(asdict(page)) if valid else None
        problem, count = None, 0
        with self.db.transaction() as tx:
            locked = locked_poll(tx, claim)
            if valid:
                old = tx.execute('''SELECT payload_hash FROM page_receipts WHERE owner_id=%s AND connection_id=%s AND chain_id=%s AND page_key=%s''',
                                 (claim.owner_id, claim.connection_id, claim.chain_id, page.page_key)).fetchone()
                if old:
                    if old['payload_hash'] != fingerprint: raise Conflict('page_content_conflict')
                    return 0
            if not authorized_poll(locked, claim): raise StaleLease('stale_poll')
            checkpoint = locked[3]
            if not valid:
                problem = Rejected('invalid_page_payload')
            elif (page.expected_page_key != checkpoint['next_page_key']
                  or page.next_page_key == page.expected_page_key
                  or (page.baseline_cursor is not None and checkpoint['baseline_cursor'] is not None
                      and page.baseline_cursor != checkpoint['baseline_cursor'])):
                restart_backfill(tx, claim.connection_id)
                problem = Conflict('invalid_page_token')
            elif (checkpoint['mode'] == 'backfill' and checkpoint['baseline_cursor'] is None
                  and page.baseline_cursor is None):
                problem = Rejected('missing_backfill_baseline')
            if not problem:
                new_keys = {uuid5(claim.connection_id, 'received:' + e['message_id']) for e in page.events if e['event_type']=='received'}
                existing = tx.execute('SELECT request_key FROM jobs WHERE owner_id=%s AND connection_id=%s AND request_key=ANY(%s)',
                                      (claim.owner_id, claim.connection_id, list(new_keys))).fetchall() if new_keys else []
                new_count = len(new_keys - {r['request_key'] for r in existing})
                pending = tx.execute('SELECT count(*) AS n FROM jobs WHERE owner_id=%s AND state=ANY(%s)', (claim.owner_id, list(ACTIVE))).fetchone()['n']
                if pending + new_count > DEFAULTS.owner_limit: problem = Rejected('owner_capacity')
            if problem:
                tx.execute('UPDATE mailbox_checkpoints SET ingestion_error=%s WHERE connection_id=%s', (problem.code, claim.connection_id))
            else:
                for item in page.events:
                    received = item['event_type'] == 'received'
                    identity = json.dumps([item['event_type'], item['message_id'], '' if received else item['occurrence_id']], separators=(',', ':'))
                    job_id = None
                    if received:
                        key = uuid5(claim.connection_id, 'received:' + item['message_id'])
                        job_id = uuid4()
                        row = tx.execute("""INSERT INTO jobs(id,owner_id,kind,request_key,input_hash,input,epoch,expires_at,connection_id,connection_generation)
                            VALUES (%s,%s,'gmail_generation',%s,%s,%s,%s,clock_timestamp()+interval '72 hours',%s,%s)
                            ON CONFLICT DO NOTHING RETURNING id""", (job_id, claim.owner_id, key, digest(item), Jsonb(item), claim.epoch, claim.connection_id, claim.generation)).fetchone()
                        if row:
                            notify(tx, job_id, claim.epoch)
                            count += 1
                        else:
                            job_id = tx.execute("SELECT id FROM jobs WHERE owner_id=%s AND connection_id=%s AND kind='gmail_generation' AND request_key=%s",
                                                (claim.owner_id, claim.connection_id, key)).fetchone()['id']
                    tx.execute('''INSERT INTO ingested_events(id,owner_id,connection_id,identity,payload,job_id,decision)
                        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (uuid4(), claim.owner_id, claim.connection_id, identity, Jsonb(item), job_id, 'generation_queued' if received else 'no_generation_required'))
                tx.execute('INSERT INTO page_receipts(owner_id,connection_id,chain_id,page_key,payload_hash) VALUES (%s,%s,%s,%s,%s)',
                           (claim.owner_id, claim.connection_id, claim.chain_id, page.page_key, fingerprint))
                # A listing's final history ID does not prove coverage of mail
                # arriving during backfill. Resume history at the pre-list baseline.
                completed_cursor = ((checkpoint['baseline_cursor'] or page.baseline_cursor)
                                    if checkpoint['mode'] == 'backfill' else page.final_cursor)
                tx.execute("""UPDATE mailbox_checkpoints SET next_page_key=%s,
                    baseline_cursor=coalesce(baseline_cursor,%s),cursor=CASE WHEN %s THEN %s ELSE cursor END,
                    mode=CASE WHEN %s THEN 'history' ELSE mode END,
                    lease_until=CASE WHEN %s THEN NULL ELSE clock_timestamp()+interval '60 seconds' END,
                    updated_at=clock_timestamp() WHERE connection_id=%s""",
                    (page.next_page_key, page.baseline_cursor, page.next_page_key is None, completed_cursor,
                     page.next_page_key is None, page.next_page_key is None, claim.connection_id))
        if problem: raise problem
        return count

    def resync(self, claim):
        with self.db.transaction() as tx:
            if not authorized_poll(locked_poll(tx, claim), claim): raise StaleLease('stale_poll')
            restart_backfill(tx, claim.connection_id)
