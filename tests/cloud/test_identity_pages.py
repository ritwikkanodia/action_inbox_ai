"""Owner-scoped pages, pagination and one durable default thread."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import unittest
from uuid import uuid4
from cloud.identity.guard import RequestDatabase
from cloud.types import Actor, NotFound, Rejected
from cloud.work import Work
from tests.cloud.identity_support import admit_fixture, web_fixture, login_fixture
from tests.cloud.support import sandbox


class PageTests(unittest.TestCase):
    def services(self,db,email='alice@gmail.com',subject='alice-sub'):
        from cloud.pages import Pages
        issued=admit_fixture(db,email,subject)
        return Pages(RequestDatabase(db,lambda:issued.proof),b'x'*32),Actor(issued.proof.owner_id),issued.proof

    def seed(self,db,owner,count=1):
        ids=sorted([uuid4() for _ in range(count)])
        with db.transaction() as tx:
            for id in ids:
                tx.execute("INSERT INTO todos(id,owner_id,title,source,created_at,source_meta) VALUES (%s,%s,%s,'gmail',%s,%s)",
                           (id,owner,'<img src=x onerror=alert(1)>',datetime(2026,1,1,tzinfo=timezone.utc),'{"private":"hidden-provider-data"}'))
        return ids

    def test_two_tabs_share_primary_and_todo_threads_during_pause(self):
        with sandbox() as db:
            pages,actor,proof=self.services(db)
            todo=self.seed(db,actor.owner_id)[0]
            with db.transaction() as tx: tx.execute('UPDATE runtime SET enabled=false')
            for kind,id in (('chat',None),('todo',todo)):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(pages.ensure,actor,kind,id) for _ in range(2)]
                    values=[f.result(timeout=10) for f in futures]
                self.assertEqual(values[0],values[1])
            self.assertEqual(len(db.read('SELECT * FROM cloud_conversation_bindings')),2)
            self.assertEqual(db.read('SELECT * FROM jobs'),[])

    def test_gets_do_not_create_threads_and_adopt_existing_lowest_id(self):
        with sandbox() as db:
            pages,actor,proof=self.services(db)
            for _ in range(2):
                data=pages.bootstrap(actor,proof)
                self.assertIsNone(data['conversation_id'])
                self.assertEqual(data['capabilities'],{'chat_execute':False,'gmail_connect':False})
            self.assertEqual(db.read('SELECT * FROM conversations'),[])
            ids=[Work(db).create_conversation(actor,'chat') for _ in range(2)]
            self.assertEqual(pages.ensure(actor,'chat'),min(ids))
            self.assertEqual(pages.bootstrap(actor,proof)['conversation_id'],str(min(ids)))

    def test_pagination_ties_are_exact_and_foreign_cursor_denied(self):
        with sandbox() as db:
            pages,actor,proof=self.services(db)
            ids=self.seed(db,actor.owner_id,53)
            bob,bob_actor,bob_proof=self.services(db,'bob@gmail.com','bob-sub')
            self.seed(db,bob_actor.owner_id,2)
            first=pages.bootstrap(actor,proof)
            self.assertEqual(len(first['todos']),50)
            second=pages.bootstrap(actor,proof,first['next_cursor'])
            self.assertIsNone(second['next_cursor'])
            self.assertEqual([t['id'] for t in first['todos']+second['todos']],[str(i) for i in ids])
            for row in first['todos']:
                self.assertEqual(set(row),{'id','title','status','source','importance','due_date','suggested_action'})
            self.assertNotIn('hidden-provider-data',str(first))
            with self.assertRaises(Rejected): bob.bootstrap(bob_actor,bob_proof,first['next_cursor'])
            for cursor in ('bad','a'*1025,first['next_cursor']+'x'):
                with self.assertRaises(Rejected): pages.bootstrap(actor,proof,cursor)
            self.assertEqual(len(bob.bootstrap(bob_actor,bob_proof)['todos']),2)

    def test_foreign_todo_details_ensure_and_bad_kind(self):
        with sandbox() as db:
            pages,actor,proof=self.services(db)
            bob,bob_actor,_=self.services(db,'bob@gmail.com','bob-sub')
            todo=self.seed(db,bob_actor.owner_id)[0]
            with self.assertRaises(NotFound): pages.detail(actor,todo)
            with self.assertRaises(NotFound): pages.ensure(actor,'todo',todo)
            for kind,id in (('wrong',None),('todo',None),('chat',todo)):
                with self.assertRaises(Rejected): pages.ensure(actor,kind,id)
            self.assertEqual(bob.detail(bob_actor,todo)['id'],str(todo))

    def test_http_pages_are_private_safe_and_no_legacy_controls(self):
        with sandbox() as db:
            app=web_fixture(db); client=app.test_client()
            for url in ('/','/chat','/settings'):
                self.assertEqual(client.get(url).status_code,303)
            proof=login_fixture(client,db)
            bootstrap=client.get('/api/cloud/bootstrap')
            self.assertEqual(bootstrap.status_code,200)
            self.assertEqual(bootstrap.json['context_id'],proof['context_id'])
            self.assertEqual(bootstrap.json['csrf'],proof['csrf'])
            self.assertEqual(set(bootstrap.json),{'profile','csrf','context_id','capabilities','todos','next_cursor','conversation_id'})
            for url in ('/','/chat','/settings'):
                response=client.get(url); html=response.get_data(as_text=True)
                self.assertEqual(response.status_code,200)
                for forbidden in ('static/js/app.js','pwa.js','marked','fonts.googleapis','onclick=','data:image','executor-select','type="file"'):
                    self.assertNotIn(forbidden,html)
                self.assertIn('/static/js/cloud-app.js',html)
                self.assertIn('Gmail is not available yet',html)
                self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(db.read('SELECT * FROM conversations'),[])
            headers={'Origin':'https://localhost','X-CSRF-Token':proof['csrf'],'X-Athena-Context':proof['context_id']}
            response=client.post('/api/cloud/conversations/ensure',json={'kind':'chat'},headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/api/cloud/bootstrap').json['conversation_id'],response.json['id'])
            self.assertEqual(client.post('/api/cloud/conversations/ensure',json={'kind':'chat','owner_id':'bob'},headers=headers).status_code,400)
