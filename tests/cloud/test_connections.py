"""Breaks caught: revoked grants resurrected, leaked bearer tokens, stale capability authority."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
import psycopg

from cloud.authorization import Authorization
from cloud.connections import Connections
from cloud.control import Control
from cloud.effects import Effects
from cloud.leases import Leases
from cloud.types import Actor, Capability, NotFound, Rejected, StaleLease
from tests.cloud.support import sandbox
from tests.cloud.test_leases import accept, expire


def connection(db, owner='alice', account='fixture@example.invalid'):
    cid = Connections(db).connect(Actor(owner), account, 'fixture:key')
    with db.transaction() as tx:
        tx.execute('UPDATE connections SET granted_scopes=%s WHERE id=%s', (['gmail.read'], cid))
    return cid


class ConnectionTests(unittest.TestCase):
    def test_refresh_cannot_recreate_revoked_connection(self):
        with sandbox() as db:
            connections, actor = Connections(db), Actor('alice')
            cid = connection(db)
            generation = connections.generation(actor, cid)
            self.assertTrue(connections.refresh_reference(actor, cid, generation, 'fixture:key-two'))
            connections.disconnect(actor, cid)
            self.assertFalse(connections.refresh_reference(actor, cid, generation, 'fixture:key-three'))
            self.assertFalse(db.read('SELECT active FROM connections WHERE id=%s', (cid,))[0]['active'])
            reconnected = connections.connect(actor, ' FIXTURE@EXAMPLE.INVALID ', 'fixture:new')
            self.assertEqual(reconnected, cid)
            self.assertGreater(connections.generation(actor, cid), generation)
            self.assertFalse(connections.refresh_reference(actor, cid, generation, 'fixture:old'))
            self.assertEqual(db.read('SELECT granted_scopes FROM connections')[0]['granted_scopes'], [])

    def test_capability_scope_owner_and_storage(self):
        with sandbox() as db:
            cid, job, auth = connection(db), accept(db), Authorization(db)
            claim = Leases(db).claim(job.job_id, 'worker')
            cap = auth.issue(claim, cid, frozenset({'gmail.read'}))
            self.assertTrue(auth.validate(cap, 'gmail.read'))
            self.assertFalse(auth.validate(cap, 'gmail.send'))
            self.assertFalse(auth.validate(Capability('forged'), 'gmail.read'))
            self.assertNotIn(cap.token, json.dumps(db.read('SELECT * FROM capabilities'), default=str))
            self.assertNotIn(cap.token, repr(cap))
            with self.assertRaises(Rejected): auth.issue(claim, cid, frozenset({'gmail.send'}))
            bob = connection(db, 'bob')
            with self.assertRaises(NotFound): auth.issue(claim, bob, frozenset({'gmail.read'}))
            with self.assertRaises(StaleLease): auth.issue(replace(claim, owner_id='bob'), cid, frozenset({'gmail.read'}))

    def test_capability_fails_closed_after_authority_changes(self):
        for mutation in ('lease', 'reset', 'epoch', 'disconnect', 'deadline', 'cancel', 'disabled', 'cap_expiry', 'db_outage'):
            with self.subTest(mutation=mutation), sandbox() as db:
                cid, job, auth = connection(db), accept(db), Authorization(db)
                claim = Leases(db).claim(job.job_id, 'worker')
                cap = auth.issue(claim, cid, frozenset({'gmail.read'}))
                self.assertTrue(auth.validate(cap, 'gmail.read'))
                if mutation == 'lease': expire(db, job.job_id)
                elif mutation == 'reset': Control(db).reset(Actor('alice'), job.conversation_id, 1)
                elif mutation == 'disconnect': Connections(db).disconnect(Actor('alice'), cid)
                elif mutation == 'cancel': Control(db).cancel(Actor('alice'), job.job_id)
                elif mutation == 'db_outage':
                    with patch.object(db, 'transaction', side_effect=ConnectionError('synthetic outage')):
                        self.assertFalse(auth.validate(cap, 'gmail.read'))
                    continue
                else:
                    sql = {'epoch':'UPDATE runtime SET epoch=gen_random_uuid()',
                           'deadline':"UPDATE jobs SET expires_at=clock_timestamp()-interval '1s'",
                           'disabled':"UPDATE owners SET enabled=false WHERE owner_id='alice'",
                           'cap_expiry':"UPDATE capabilities SET expires_at=clock_timestamp()-interval '1s'"}[mutation]
                    with db.transaction() as tx: tx.execute(sql)
                self.assertFalse(auth.validate(cap, 'gmail.read'))

    def test_bound_jobs_cannot_switch_accounts_and_disconnect_cancels(self):
        with sandbox() as db:
            cid, other = connection(db), connection(db, account='second@example.invalid')
            job = accept(db)
            with db.transaction() as tx:
                tx.execute('UPDATE jobs SET connection_id=%s,connection_generation=1 WHERE id=%s', (cid, job.job_id))
            claim = Leases(db).claim(job.job_id, 'worker')
            with self.assertRaises(Rejected): Authorization(db).issue(claim, other, frozenset({'gmail.read'}))
            Connections(db).disconnect(Actor('alice'), cid)
            self.assertFalse(Leases(db).finish(claim, 'stale'))
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'cancelled')

    def test_disconnect_after_general_chat_tool_intent_keeps_hold(self):
        with sandbox() as db:
            cid, job = connection(db), accept(db)
            claim = Leases(db).claim(job.job_id, 'worker')
            cap = Authorization(db).issue(claim, cid, frozenset({'gmail.read'}))
            Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture')
            Connections(db).disconnect(Actor('alice'), cid)
            self.assertFalse(Authorization(db).validate(cap, 'gmail.read'))
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'needs_reconciliation')

    def test_connection_foreign_key_and_paired_null_constraint(self):
        with sandbox() as db:
            cid, job = connection(db, 'bob'), accept(db)
            for sql, params in (
                ('UPDATE jobs SET connection_id=%s WHERE id=%s', (cid, job.job_id)),
                ('UPDATE jobs SET connection_id=%s,connection_generation=1 WHERE id=%s', (cid, job.job_id))):
                with self.assertRaises(psycopg.IntegrityError), db.transaction() as tx: tx.execute(sql, params)

    def test_raw_credentials_are_not_accepted_as_references(self):
        with sandbox() as db:
            for value in ('sk-fixture-not-a-reference', '{"token":"fixture"}', ''):
                with self.assertRaises(Rejected): Connections(db).connect(Actor('alice'), 'fixture@example.invalid', value)
