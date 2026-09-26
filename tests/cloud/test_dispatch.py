"""Real outbox transitions and fake Azure transport boundary; no broker access."""
from dataclasses import replace
import json
import unittest
from uuid import uuid4

from azure.servicebus import ServiceBusReceiveMode
from cloud.dispatch import Dispatcher, Receiver, PoisonEnvelope
from cloud.leases import Leases
from cloud.recovery import Recovery
from cloud.service_bus import ServiceBusTransport
from cloud.types import Envelope
from cloud.types import Actor, Submission
from cloud.work import Work
from tests.cloud.support import sandbox
from tests.cloud.test_leases import accept


def envelope(db, job_id):
    row = db.read('SELECT * FROM outbox WHERE job_id=%s ORDER BY created_at LIMIT 1', (job_id,))[0]
    return Envelope(row['version'], row['id'], job_id, row['epoch'])


def old_notifications(db):
    with db.transaction() as tx:
        tx.execute("UPDATE outbox SET created_at=clock_timestamp()-interval '61 seconds',published_at=clock_timestamp()-interval '61 seconds',lease_until=NULL")


class Message:
    def __init__(self, body): self.body = body
    def __str__(self): return self.body


class Broker:
    def __init__(self, messages=()):
        self.messages, self.sent, self.settled = list(messages), [], []
        self.receive_options = None
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get_queue_sender(self, *, queue_name):
        assert queue_name == 'synthetic'
        return self
    def send_messages(self, message): self.sent.append(message)
    def get_queue_receiver(self, **kwargs):
        self.receive_options = kwargs
        return self
    def receive_messages(self, **kwargs): return self.messages[:1]
    def complete_message(self, message): self.settled.append(('complete', message))
    def dead_letter_message(self, message, **kwargs): self.settled.append(('dead_letter', message, kwargs))


class DispatchTests(unittest.TestCase):
    def test_send_retry_reuses_dispatch_identity_and_releases_locks(self):
        with sandbox() as db:
            accept(db)
            sent = []
            def uncertain_send(value):
                # A second transaction can obtain the same locks during transport.
                with db.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout='100ms'")
                    tx.execute("SELECT * FROM owners WHERE owner_id='alice' FOR UPDATE")
                    tx.execute('SELECT * FROM outbox FOR UPDATE')
                sent.append(value)
                if len(sent) == 1: raise TimeoutError('synthetic lost acknowledgement')
            dispatcher = Dispatcher(db, uncertain_send)
            self.assertTrue(dispatcher.once())
            self.assertIsNone(db.read('SELECT published_at FROM outbox')[0]['published_at'])
            with db.transaction() as tx: tx.execute('UPDATE outbox SET due_at=clock_timestamp(),lease_until=NULL')
            self.assertTrue(dispatcher.once())
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0].dispatch_id, sent[1].dispatch_id)
            self.assertIsNotNone(db.read('SELECT published_at FROM outbox')[0]['published_at'])

    def test_out_of_order_notification_is_recovered_with_fresh_identity(self):
        with sandbox() as db:
            first = accept(db)
            second = accept(db, first.conversation_id, 'later')
            early = envelope(db, second.job_id)
            self.assertIsNone(Receiver(db).handle(early, 'early-worker'))
            one = Receiver(db).handle(envelope(db, first.job_id), 'first-worker')
            self.assertTrue(Leases(db).finish(one, 'reply'))
            # Lose every notification including the wake-up created by completion.
            old_notifications(db)
            before = {r['id'] for r in db.read('SELECT id FROM outbox')}
            self.assertEqual(Recovery(db).sweep().get('redispatched'), 1)
            fresh = [r for r in db.read('SELECT * FROM outbox') if r['id'] not in before]
            self.assertEqual(len(fresh), 1)
            self.assertEqual(fresh[0]['job_id'], second.job_id)
            self.assertNotEqual(fresh[0]['id'], early.dispatch_id)
            value = Envelope(1, fresh[0]['id'], second.job_id, fresh[0]['epoch'])
            claim = Receiver(db).handle(value, 'second-worker')
            self.assertIsNotNone(claim)
            self.assertTrue(Leases(db).finish(claim, 'second reply'))
            self.assertIsNone(Receiver(db).handle(value, 'duplicate'))

    def test_stale_epoch_and_unknown_dispatch_cannot_claim(self):
        with sandbox() as db:
            job = accept(db)
            value = envelope(db, job.job_id)
            self.assertIsNone(Receiver(db).handle(replace(value, epoch=uuid4()), 'old'))
            self.assertIsNone(Receiver(db).handle(replace(value, dispatch_id=uuid4()), 'unknown'))
            self.assertEqual(db.read('SELECT attempts FROM jobs')[0]['attempts'], 0)

    def test_poison_is_quarantined_without_endless_redispatch(self):
        with sandbox() as db:
            job = accept(db)
            value = replace(envelope(db, job.job_id), version=999)
            with self.assertRaises(PoisonEnvelope): Receiver(db).handle(value, 'worker')
            self.assertTrue(db.read('SELECT quarantined FROM outbox')[0]['quarantined'])
            self.assertEqual(db.read('SELECT state FROM jobs')[0]['state'], 'failed')
            old_notifications(db)
            self.assertEqual(Recovery(db).sweep().get('redispatched', 0), 0)

    def test_stale_sender_cannot_publish_after_lease_loss(self):
        with sandbox() as db:
            accept(db)
            def delayed_send(_):
                with db.transaction() as tx:
                    tx.execute("UPDATE outbox SET lease_until=clock_timestamp()-interval '1s'")
            Dispatcher(db, delayed_send).once()
            self.assertIsNone(db.read('SELECT published_at FROM outbox')[0]['published_at'])
            sent = []
            Dispatcher(db, sent.append).once()
            self.assertEqual(len(sent), 1)
            self.assertIsNotNone(db.read('SELECT published_at FROM outbox')[0]['published_at'])

    def test_active_publish_lease_and_recent_notification_suppress_sweep(self):
        with sandbox() as db:
            accept(db)
            self.assertEqual(Recovery(db).sweep().get('redispatched', 0), 0)
            old_notifications(db)
            with db.transaction() as tx: tx.execute("UPDATE outbox SET lease_until=clock_timestamp()+interval '30s'")
            self.assertEqual(Recovery(db).sweep().get('redispatched', 0), 0)

    def test_adapter_envelope_and_peeklock_settlement(self):
        value = Envelope(1, uuid4(), uuid4(), uuid4())
        broker = Broker()
        transport = ServiceBusTransport(broker, 'synthetic')
        transport.send(value)
        sent = broker.sent[0]
        self.assertEqual(str(sent.message_id), str(value.dispatch_id))
        body = json.loads(str(sent))
        self.assertEqual(set(body), {'version','dispatch_id','job_id','epoch'})
        self.assertEqual(body['job_id'], str(value.job_id))
        broker.messages = [Message(str(sent))]
        seen = []
        transport.receive_once(seen.append)
        self.assertEqual(seen, [value])
        self.assertEqual(broker.settled[0][0], 'complete')
        self.assertEqual(broker.receive_options['receive_mode'], ServiceBusReceiveMode.PEEK_LOCK)

    def test_database_error_does_not_settle(self):
        value = Envelope(1, uuid4(), uuid4(), uuid4())
        broker = Broker()
        transport = ServiceBusTransport(broker, 'synthetic')
        transport.send(value)
        broker.messages = [Message(str(broker.sent[0]))]
        def unavailable(_): raise ConnectionError('synthetic database unavailable')
        with self.assertRaises(ConnectionError): transport.receive_once(unavailable)
        self.assertEqual(broker.settled, [])

    def test_malformed_wire_envelopes_dead_letter_without_execution(self):
        for body in ('not json', '[]', '{}', '{"version":true}', 'x' * 4097):
            broker, seen = Broker([Message(body)]), []
            ServiceBusTransport(broker, 'synthetic').receive_once(seen.append)
            self.assertEqual(seen, [])
            self.assertEqual(broker.settled[0][0], 'dead_letter')

    def test_disabled_owner_cannot_starve_dispatch(self):
        with sandbox() as db:
            first = accept(db)
            with db.transaction() as tx:
                tx.execute('''INSERT INTO outbox(id,job_id,epoch) SELECT gen_random_uuid(),%s,
                              (SELECT epoch FROM runtime) FROM generate_series(1,100)''', (first.job_id,))
                tx.execute("UPDATE owners SET enabled=false WHERE owner_id='alice'")
            actor = Actor('bob')
            cid = Work(db).create_conversation(actor, 'chat')
            bob = Work(db).accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            sent = []
            self.assertTrue(Dispatcher(db, sent.append).once())
            self.assertEqual([v.job_id for v in sent], [bob.job_id])
