"""Cloud read models and idempotent thread bindings; no local data imports."""
from datetime import datetime
from uuid import UUID
from flask import jsonify, render_template, request, current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature
from cloud.http import body
from cloud.identity.crypto import csrf_token
from cloud.identity.security import resolve_request, cookie_names
from cloud.types import Actor, NotFound, Rejected
from cloud.work import create_conversation_in, lock_owner

TODO_COLUMNS = 'id,title,status,source,created_at'


def public_todo(row):
    return {key: str(row[key]) if key=='id' else row[key] for key in ('id','title','status','source')} | {
        'importance':None,'due_date':None,'suggested_action':None}


class Pages:
    def __init__(self, request_db, cursor_key):
        self.db=request_db
        self.signer=URLSafeTimedSerializer(cursor_key,salt='athena-todo-cursor-v1')

    def _cursor(self, cursor, owner):
        if cursor is None: return None
        try:
            if not isinstance(cursor,str) or not 1<=len(cursor)<=1024: raise ValueError()
            data=self.signer.loads(cursor,max_age=86400)
            if (not isinstance(data,dict) or set(data)!={'version','owner','timestamp','id'}
                    or type(data['version']) is not int or data['version']!=1 or data['owner']!=owner
                    or not isinstance(data['timestamp'],str) or not isinstance(data['id'],str)
                    or len(data['timestamp'])>64 or len(data['id'])!=36): raise ValueError()
            stamp=datetime.fromisoformat(data['timestamp'])
            if stamp.tzinfo is None: raise ValueError()
            return stamp,UUID(data['id'])
        except (BadSignature,ValueError,TypeError,OverflowError):
            raise Rejected('invalid_cursor') from None

    def bootstrap(self, actor, proof, cursor=None):
        after=self._cursor(cursor,actor.owner_id)
        with self.db.transaction() as tx:
            profile=tx.execute('SELECT display_name AS name,email FROM auth_identities WHERE owner_id=%s',
                               (actor.owner_id,)).fetchone()
            if not profile: raise NotFound('profile_not_found')
            query='SELECT '+TODO_COLUMNS+' FROM todos WHERE owner_id=%s'
            params=(actor.owner_id,)
            if after:
                query+=' AND (created_at,id)>(%s,%s)'
                params+=after
            rows=tx.execute(query+' ORDER BY created_at,id LIMIT 51',params).fetchall()
            binding=tx.execute("SELECT conversation_id FROM cloud_conversation_bindings WHERE owner_id=%s AND slot='chat'",
                               (actor.owner_id,)).fetchone()
        next_cursor=None
        if len(rows)>50:
            last=rows[49]
            next_cursor=self.signer.dumps({'version':1,'owner':actor.owner_id,
                                           'timestamp':last['created_at'].isoformat(),'id':str(last['id'])})
        return {'profile':profile,'context_id':str(proof.context_id),'csrf':None,
                'capabilities':{'chat_execute':False,'gmail_connect':False},
                'todos':[public_todo(row) for row in rows[:50]],'next_cursor':next_cursor,
                'conversation_id':str(binding['conversation_id']) if binding else None}

    def detail(self, actor, todo_id):
        with self.db.transaction() as tx:
            row=tx.execute('SELECT '+TODO_COLUMNS+' FROM todos WHERE owner_id=%s AND id=%s',
                           (actor.owner_id,todo_id)).fetchone()
            if not row: raise NotFound('todo_not_found')
            return public_todo(row)

    def ensure(self, actor, kind, todo_id=None):
        if kind not in ('chat','todo') or (kind=='todo')!=(todo_id is not None):
            raise Rejected('invalid_conversation')
        slot='chat' if kind=='chat' else 'todo:'+str(todo_id)
        with self.db.transaction() as tx:
            lock_owner(tx,actor.owner_id,require_enabled=False)
            if todo_id is not None and not tx.execute('SELECT id FROM todos WHERE owner_id=%s AND id=%s',
                                                      (actor.owner_id,todo_id)).fetchone():
                raise NotFound('todo_not_found')
            bound=tx.execute('SELECT conversation_id FROM cloud_conversation_bindings WHERE owner_id=%s AND slot=%s',
                             (actor.owner_id,slot)).fetchone()
            if bound: return bound['conversation_id']
            old=tx.execute('''SELECT id FROM conversations WHERE owner_id=%s AND kind=%s
                AND todo_id IS NOT DISTINCT FROM %s ORDER BY id LIMIT 1''',(actor.owner_id,kind,todo_id)).fetchone()
            cid=old['id'] if old else create_conversation_in(tx,actor,kind,todo_id,require_enabled=False)
            tx.execute('INSERT INTO cloud_conversation_bindings(owner_id,slot,conversation_id) VALUES (%s,%s,%s)',
                       (actor.owner_id,slot,cid))
            return cid


def register_page_routes(app, pages, sessions):
    def actor():
        return Actor(resolve_request(sessions).owner_id)

    def shell():
        proof=resolve_request(sessions)
        # Guard again for account disable/revocation between resolve and HTML.
        with pages.db.transaction():
            return render_template('cloud/shell.html')

    for endpoint,path in (('cloud_home','/'),('cloud_chat','/chat'),('cloud_settings','/settings')):
        app.add_url_rule(path,endpoint,shell)

    @app.get('/api/cloud/bootstrap')
    def bootstrap():
        proof=resolve_request(sessions)
        data=pages.bootstrap(actor(),proof,request.args.get('cursor'))
        data['csrf']=csrf_token(request.cookies[cookie_names(current_app)[0]])
        if app.testing and app.extensions.get('fixture_executable'):
            data['capabilities']['chat_execute']=True
        return jsonify(data)

    @app.get('/api/cloud/todos/<uuid:todo_id>')
    def detail(todo_id):
        return jsonify(pages.detail(actor(),todo_id))

    @app.post('/api/cloud/conversations/ensure')
    def ensure():
        value=body({'kind','todo_id'},{'kind'})
        todo=UUID(value['todo_id']) if value.get('todo_id') is not None else None
        return jsonify(id=str(pages.ensure(actor(),value['kind'],todo)))
