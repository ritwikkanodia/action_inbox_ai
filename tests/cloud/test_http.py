"""The API must acknowledge only committed work and derive identity server-side."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch
from uuid import uuid4

from cloud.app import create_app
from cloud.types import Actor
from tests.cloud.support import sandbox


class HttpTests(unittest.TestCase):
    def test_receipt_survives_application_recreation(self):
        with sandbox() as db:
            client = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            cid = client.post('/api/work/conversations', json={'kind':'chat'}).json['id']
            body = {'text':'fixture','generation':1,'request_key':str(uuid4())}
            response = client.post(f'/api/work/conversations/{cid}/messages', json=body)
            self.assertEqual(response.status_code, 202)
            second = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            duplicate = second.post(f'/api/work/conversations/{cid}/messages', json=body)
            self.assertEqual(duplicate.json['job_id'], response.json['job_id'])
            self.assertEqual(duplicate.status_code, 200)
            job = response.json['job_id']
            self.assertEqual(second.post(f'/api/work/jobs/{job}/cancel').json['state'], 'cancelled')
            self.assertEqual(second.post(f'/api/work/conversations/{cid}/reset', json={'generation':1}).json['generation'], 2)
            self.assertEqual(second.post(f'/api/work/conversations/{cid}/messages', json=body).status_code, 409)

    def test_authentication_is_not_a_client_header(self):
        with sandbox() as db:
            with self.assertRaises(ValueError): create_app(db, lambda: Actor('alice'))
            client = create_app(db).test_client()
            self.assertEqual(client.post('/api/work/conversations', json={'kind':'chat','owner_id':'alice'}, headers={'X-Owner-ID':'alice'}).status_code, 401)
            self.assertEqual(client.get('/ready').json['reason'], 'authentication_not_integrated')
            self.assertEqual(client.get('/').status_code, 404)
            alice = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            cid = alice.post('/api/work/conversations', json={'kind':'chat'}).json['id']
            bob = create_app(db, lambda: Actor('bob'), testing=True).test_client()
            self.assertEqual(bob.get(f'/api/work/conversations/{cid}').status_code, 404)

    def test_malformed_requests_and_size_limits(self):
        with sandbox() as db:
            client = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            cid = client.post('/api/work/conversations', json={'kind':'chat'}).json['id']
            url = f'/api/work/conversations/{cid}/messages'
            for body in ([], {}, {'text':7}, {'text':'ok','generation':True,'request_key':str(uuid4())},
                         {'text':'é'*16385,'generation':1,'request_key':str(uuid4())},
                         {'text':'\ud800','generation':1,'request_key':str(uuid4())}):
                self.assertEqual(client.post(url,json=body).status_code,400)
            self.assertEqual(client.post(url, data='{broken', content_type='application/json').status_code,400)
            self.assertEqual(client.post(url, data='x'*65537, content_type='application/json').status_code,400)

    def test_commit_failure_cannot_return_accepted(self):
        with sandbox() as db:
            client = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            cid = client.post('/api/work/conversations', json={'kind':'chat'}).json['id']
            original = db.transaction
            @contextmanager
            def broken_commit():
                with original() as tx:
                    yield tx
                    raise ConnectionError('synthetic private DSN must not appear')
            with patch.object(db, 'transaction', broken_commit):
                response = client.post(f'/api/work/conversations/{cid}/messages',json={'text':'fixture','generation':1,'request_key':str(uuid4())})
            self.assertEqual(response.status_code,503)
            self.assertNotIn('DSN',response.get_data(as_text=True))
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'],0)
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            self.assertEqual(client.post(f'/api/work/conversations/{cid}/messages',json={'text':'fixture','generation':1,'request_key':str(uuid4())}).status_code,503)

    def test_quota_and_changed_request_key_have_distinct_status(self):
        with sandbox() as db:
            client = create_app(db, lambda: Actor('alice'), testing=True).test_client()
            cid = client.post('/api/work/conversations',json={'kind':'chat'}).json['id']
            url=f'/api/work/conversations/{cid}/messages'
            body={'text':'first','generation':1,'request_key':str(uuid4())}
            client.post(url,json=body)
            self.assertEqual(client.post(url,json=dict(body,text='changed')).status_code,409)
            for i in range(19): client.post(url,json=dict(body,request_key=str(uuid4())))
            self.assertEqual(client.post(url,json=dict(body,request_key=str(uuid4()))).status_code,429)
            self.assertEqual(client.post(url,json=body).status_code,200)
