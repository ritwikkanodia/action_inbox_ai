"""Atomic acceptance and append-only messages. Helpers never commit transactions."""
import hashlib
import json
from uuid import UUID, uuid4
from psycopg.types.json import Jsonb

from cloud.config import DEFAULTS
from cloud.types import Conflict, NotFound, Receipt, Rejected, Unavailable

ACTIVE = ('queued', 'running', 'retry_pending', 'needs_reconciliation')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def lock_owner(tx, owner_id, *, require_enabled=True):
    runtime = tx.execute('SELECT * FROM runtime WHERE singleton FOR SHARE').fetchone()
    owner = tx.execute('SELECT * FROM owners WHERE owner_id=%s FOR UPDATE', (owner_id,)).fetchone()
    if not owner:
        raise NotFound('owner_not_found')
    if require_enabled and (not owner['enabled'] or not runtime['enabled']):
        raise Unavailable('work_disabled')
    return runtime, owner


def lock_conversation(tx, owner_id, cid):
    conversation = tx.execute('SELECT * FROM conversations WHERE owner_id=%s AND id=%s FOR UPDATE', (owner_id, cid)).fetchone()
    if not conversation:
        raise NotFound('conversation_not_found')
    return conversation


def notify(tx, job_id, epoch):
    tx.execute('INSERT INTO outbox(id,job_id,epoch) VALUES (%s,%s,%s)', (uuid4(), job_id, epoch))


def append_message(tx, conversation, role, content, origin, job_id=None):
    owner, cid, generation = (conversation[k] for k in ('owner_id', 'id', 'generation'))
    existing = tx.execute('SELECT id FROM messages WHERE owner_id=%s AND conversation_id=%s AND generation=%s AND origin=%s',
                          (owner, cid, generation, origin)).fetchone()
    if existing:
        return
    tx.execute('''INSERT INTO messages(id,owner_id,conversation_id,generation,sequence,role,content,origin,job_id)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
               (uuid4(), owner, cid, generation, conversation['next_message_seq'], role, content, origin, job_id))
    tx.execute('UPDATE conversations SET next_message_seq=next_message_seq+1 WHERE id=%s', (cid,))
    conversation['next_message_seq'] += 1


class Work:
    def __init__(self, db, config=DEFAULTS):
        self.db, self.config = db, config

    def create_conversation(self, actor, kind, todo_id=None):
        if kind not in ('chat', 'todo') or (kind == 'todo') != (todo_id is not None):
            raise Rejected('invalid_conversation')
        cid = uuid4()
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id)
            if todo_id is not None and not tx.execute('SELECT id FROM todos WHERE owner_id=%s AND id=%s', (actor.owner_id, todo_id)).fetchone():
                raise NotFound('todo_not_found')
            tx.execute('INSERT INTO conversations(id,owner_id,kind,todo_id) VALUES (%s,%s,%s,%s)', (cid, actor.owner_id, kind, todo_id))
        return cid

    def accept(self, actor, request):
        if (not isinstance(request.text, str) or not request.text.strip()
                or len(request.text.encode()) > self.config.text_bytes
                or type(request.from_suggestion) is not bool
                or type(request.generation) is not int or request.generation < 1
                or not isinstance(request.request_key, UUID) or not isinstance(request.conversation_id, UUID)):
            raise Rejected('invalid_submission')
        text = request.text.strip()
        with self.db.transaction() as tx:
            runtime, _ = lock_owner(tx, actor.owner_id)
            conversation = lock_conversation(tx, actor.owner_id, request.conversation_id)
            if conversation['generation'] != request.generation:
                raise Conflict('stale_generation')
            payload = {'text': text, 'generation': request.generation, 'kind': conversation['kind'],
                       'from_suggestion': request.from_suggestion}
            fingerprint = digest(payload)
            old = tx.execute('''SELECT * FROM jobs WHERE owner_id=%s AND conversation_id=%s
                                AND generation=%s AND request_key=%s''',
                             (actor.owner_id, request.conversation_id, request.generation, request.request_key)).fetchone()
            if old:
                if old['input_hash'] != fingerprint:
                    raise Conflict('request_key_conflict')
                receipt = Receipt(old['id'], request.conversation_id, request.generation, old['state'], True)
            else:
                counts = tx.execute('''SELECT count(*) AS owner_count,
                    count(*) FILTER(WHERE conversation_id=%s) AS conversation_count
                    FROM jobs WHERE owner_id=%s AND state=ANY(%s)''',
                                    (request.conversation_id, actor.owner_id, list(ACTIVE))).fetchone()
                if counts['conversation_count'] >= self.config.conversation_limit:
                    raise Rejected('conversation_capacity')
                if counts['owner_count'] >= self.config.owner_limit:
                    raise Rejected('owner_capacity')
                job_id = uuid4()
                tx.execute('''INSERT INTO jobs(id,owner_id,conversation_id,generation,job_order,kind,
                    request_key,input_hash,input,epoch,expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    clock_timestamp()+%s*interval '1 second')''',
                    (job_id, actor.owner_id, request.conversation_id, request.generation,
                     conversation['next_job_order'], conversation['kind'], request.request_key, fingerprint,
                     Jsonb(payload), runtime['epoch'], self.config.chat_ttl_seconds))
                append_message(tx, conversation, 'user', text, 'submission:' + str(job_id), job_id)
                notify(tx, job_id, runtime['epoch'])
                tx.execute('UPDATE conversations SET next_job_order=next_job_order+1 WHERE id=%s', (request.conversation_id,))
                receipt = Receipt(job_id, request.conversation_id, request.generation, 'queued')
        return receipt

    def snapshot(self, actor, conversation_id):
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id, require_enabled=False)
            conversation = lock_conversation(tx, actor.owner_id, conversation_id)
            params = (actor.owner_id, conversation_id, conversation['generation'])
            messages = tx.execute('''SELECT id,sequence,role,content,job_id,created_at FROM messages
                WHERE owner_id=%s AND conversation_id=%s AND generation=%s ORDER BY sequence''', params).fetchall()
            jobs = tx.execute('''SELECT id AS job_id,state,reason,job_order AS "order",created_at,finished_at
                FROM jobs WHERE owner_id=%s AND conversation_id=%s AND generation=%s ORDER BY job_order''', params).fetchall()
            outstanding = tx.execute('''SELECT id AS job_id,state,reason FROM jobs WHERE owner_id=%s
                AND conversation_id=%s AND state='needs_reconciliation' ORDER BY created_at''', params[:2]).fetchall()
        return {'generation': conversation['generation'], 'messages': messages, 'jobs': jobs,
                'reconciliation_hold': conversation['reconciliation_hold'], 'outstanding': outstanding}

    def notice(self, actor, conversation_id, origin, text):
        if not isinstance(origin, str) or not origin or len(origin) > 512 or not isinstance(text, str) or len(text.encode()) > self.config.text_bytes:
            raise Rejected('invalid_notice')
        with self.db.transaction() as tx:
            lock_owner(tx, actor.owner_id)
            conversation = lock_conversation(tx, actor.owner_id, conversation_id)
            append_message(tx, conversation, 'notice', text, 'notice:' + origin)
