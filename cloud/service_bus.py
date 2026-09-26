"""Injected Azure adapter. No credentials or clients are constructed here."""
import json
from uuid import UUID
from azure.servicebus import ServiceBusMessage, ServiceBusReceiveMode

from cloud.dispatch import PoisonEnvelope
from cloud.types import Envelope


def encode(envelope):
    return json.dumps({'version': envelope.version, 'dispatch_id': str(envelope.dispatch_id),
                       'job_id': str(envelope.job_id), 'epoch': str(envelope.epoch)})


def decode(body):
    try:
        if len(body.encode()) > 4096: raise ValueError()
        value = json.loads(body)
        if (not isinstance(value, dict) or set(value) != {'version', 'dispatch_id', 'job_id', 'epoch'}
                or type(value['version']) is not int): raise ValueError()
        return Envelope(value['version'], *(UUID(value[k]) for k in ('dispatch_id', 'job_id', 'epoch')))
    except (ValueError, TypeError, AttributeError):
        raise PoisonEnvelope('malformed_envelope') from None


class ServiceBusTransport:
    def __init__(self, client, queue): self.client, self.queue = client, queue

    def send(self, envelope):
        with self.client.get_queue_sender(queue_name=self.queue) as sender:
            sender.send_messages(ServiceBusMessage(encode(envelope), message_id=str(envelope.dispatch_id)))

    def receive_once(self, handle):
        with self.client.get_queue_receiver(queue_name=self.queue, receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
                                            max_wait_time=1) as receiver:
            for message in receiver.receive_messages(max_message_count=1, max_wait_time=1):
                try:
                    envelope = decode(str(message))
                    handle(envelope)
                    if envelope.version != 1: raise PoisonEnvelope('unsupported_envelope')
                except PoisonEnvelope:
                    receiver.dead_letter_message(message, reason='invalid_work_envelope')
                else:
                    # A failed database lookup propagates; it is never acknowledged.
                    receiver.complete_message(message)
