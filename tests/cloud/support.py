"""Synthetic fixtures only. Never point these helpers at a supplied database."""
from contextlib import contextmanager
import os
import socket
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from cloud.database import Database
from cloud.migrate import migrate


def validate_target(dsn, marker):
    fields = conninfo_to_dict(dsn)
    if (not marker or marker != os.environ.get('ATHENA_VERIFY_MARKER')
            or dsn != os.environ.get('ATHENA_VERIFY_DSN')
            or fields.get('host') != '127.0.0.1'
            or fields.get('dbname') != 'athena_verify'
            or fields.get('user') != 'athena_verify'
            or not fields.get('password') or not fields.get('port')):
        raise ValueError('unsafe_test_target')


def deny_remote(event, args):
    if event == 'socket.connect':
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            raise OSError('test_egress_denied')
    if event == 'socket.getaddrinfo' and args[0] not in ('localhost', '127.0.0.1', '::1', None):
        raise OSError('test_egress_denied')


@contextmanager
def sandbox():
    dsn, marker = os.environ.get('ATHENA_VERIFY_DSN', ''), os.environ.get('ATHENA_VERIFY_MARKER', '')
    validate_target(dsn, marker)
    schema = 'verify_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        db = Database(dsn, schema)
        migrate(db)
        with db.transaction() as tx:
            tx.execute("INSERT INTO owners(owner_id) VALUES ('alice'),('bob')")
            tx.execute('UPDATE runtime SET enabled=true')
        yield db
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def browser_fixture(app, conversation_id):
    """Register UI fixture endpoints only in an explicitly testing factory."""
    if not app.testing: raise ValueError('fixture_requires_testing')
    from flask import render_template
    def shell():
        return render_template('index.html', todos=[], todos_json='[]',
            user={'name':'Synthetic User','email':'fixture@example.invalid','picture_url':None},
            gmail_connected=True, gmail_auth_url='#', fresh_signup=False, initial_view='chat',
            durable_work=True, durable_conversations={'chat':str(conversation_id)})
    for name, path in (('index','/'),('chat_page','/chat'),('settings_page','/settings')):
        app.add_url_rule(path,name,shell)
    app.add_url_rule('/logout','logout',lambda: ('',204), methods=['POST'])
    app.add_url_rule('/manifest.webmanifest','manifest',lambda: {'name':'Synthetic Athena','start_url':'/'})
