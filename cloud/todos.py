"""Generation decisions are transactions, never notification/provider side effects."""
from datetime import date
from uuid import uuid4
from psycopg.types.json import Jsonb

# Audited pure imports: db defines functions/constants only, and its GmailEvent
# dependency is a dataclass module. No connection or local entrypoint is invoked.
from db import titles_similar
from pollers.noise import is_auth_noise_title
from cloud.leases import authorized, enabled, locked_job, transition
from cloud.types import Conflict, Rejected, StaleLease
from cloud.work import append_message, digest, lock_owner


def validate_todo(todo):
    if not isinstance(todo, dict): raise Rejected('invalid_todo')
    if set(todo) - {'title','source','importance','status','suggested_action','reasoning','due_date'}: raise Rejected('invalid_todo')
    title = todo.get('title')
    if not isinstance(title, str) or not title.strip() or len(title) > 4096: raise Rejected('invalid_todo')
    if todo.get('source','gmail') != 'gmail' or todo.get('importance','medium') not in ('low','medium','high') or todo.get('status','open') not in ('open','ongoing','closed'): raise Rejected('invalid_todo')
    for key in ('suggested_action', 'reasoning'):
        if key in todo and (not isinstance(todo[key], str) or len(todo[key]) > 8192): raise Rejected('invalid_todo')
    try:
        if todo.get('due_date'): date.fromisoformat(todo['due_date'])
    except (ValueError, TypeError): raise Rejected('invalid_todo') from None


class Todos:
    def __init__(self, db): self.db = db

    def record_generation(self, claim, decision, todo):
        if decision not in ('created','duplicate','skipped') or (decision != 'created' and todo is not None): raise Rejected('invalid_generation_decision')
        if decision == 'created': validate_todo(todo)
        fingerprint, tid = digest([decision, todo]), None
        with self.db.transaction() as tx:
            lock_owner(tx, claim.owner_id)
            hint = tx.execute('SELECT connection_id FROM jobs WHERE id=%s AND owner_id=%s', (claim.job_id, claim.owner_id)).fetchone()
            if not hint: raise StaleLease('stale_claim')
            tx.execute('SELECT id FROM connections WHERE id=%s FOR UPDATE', (hint['connection_id'],)).fetchone()
            chat = tx.execute("SELECT * FROM conversations WHERE owner_id=%s AND kind='chat' ORDER BY id LIMIT 1 FOR UPDATE", (claim.owner_id,)).fetchone()
            locked = locked_job(tx, claim.job_id)
            job = locked[3]
            old = tx.execute('SELECT * FROM generation_decisions WHERE owner_id=%s AND job_id=%s', (claim.owner_id, claim.job_id)).fetchone()
            if old and enabled(locked) and (claim.attempt,claim.epoch) == (job['fence'],job['epoch']):
                if old['request_hash'] != fingerprint: raise Conflict('generation_result_conflict')
                return old['todo_id']
            if not authorized(locked, claim) or job['kind'] != 'gmail_generation' or not job['connection_id']: raise StaleLease('stale_claim')
            reason = 'generator_' + decision
            if decision == 'created':
                title = todo['title'].strip()
                recent = tx.execute("SELECT title FROM todos WHERE owner_id=%s AND created_at>=clock_timestamp()-interval '14 days'", (claim.owner_id,)).fetchall()
                if is_auth_noise_title(title): decision, reason = 'skipped', 'auth_noise'
                elif any(titles_similar(title, row['title']) for row in recent): decision, reason = 'duplicate', 'near_duplicate'
                else:
                    tid = uuid4()
                    payload = job['input']
                    tx.execute('''INSERT INTO todos(id,owner_id,title,source,status,dedup_key,connection_id,message_id,thread_id,
                        importance,suggested_action,reasoning,due_date,source_meta) VALUES (%s,%s,%s,'gmail',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (tid,claim.owner_id,title,todo.get('status','open'),str(job['connection_id'])+':'+payload['message_id'],
                         job['connection_id'],payload['message_id'],payload['thread_id'],todo.get('importance','medium'),
                         todo.get('suggested_action'),todo.get('reasoning'),todo.get('due_date') or None,
                         Jsonb({'message_id':payload['message_id'],'thread_id':payload['thread_id']})))
                    if not chat:
                        chat = tx.execute("INSERT INTO conversations(id,owner_id,kind) VALUES (%s,%s,'chat') RETURNING *", (uuid4(),claim.owner_id)).fetchone()
                    append_message(tx, chat, 'notice', 'New todo from Gmail: ' + title, 'generated:' + str(job['id']))
            tx.execute('''INSERT INTO generation_decisions(job_id,owner_id,decision,todo_id,safe_reason,request_hash)
                          VALUES (%s,%s,%s,%s,%s,%s)''', (claim.job_id,claim.owner_id,decision,tid,reason,fingerprint))
            transition(tx, locked, 'succeeded')
        return tid

    def list(self, actor):
        return self.db.read('SELECT * FROM todos WHERE owner_id=%s ORDER BY created_at,id', (actor.owner_id,))
