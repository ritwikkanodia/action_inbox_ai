"""Real Chrome, real identity routes, fake signed Google HTTP boundary only."""
import io
import os
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from pathlib import Path
from werkzeug.serving import make_server, WSGIRequestHandler
from tests.cloud.support import sandbox
from tests.cloud.identity_support import web_fixture
from cloud.identity.admission import Admission
from cloud.leases import Leases
from cloud.work import Work
from cloud.types import Actor


def verify():
    from playwright.sync_api import sync_playwright, expect
    with sandbox() as db:
        logs=[]
        class SafeHandler(WSGIRequestHandler):
            def log_request(self,code='-',size='-'):
                logs.append(self.command+' '+urlsplit(self.path).path+' '+str(code))
            def log_error(self,*args): logs.append('fixture_server_error')
        server=make_server('127.0.0.1',0,lambda e,s:[],threaded=True,request_handler=SafeHandler)
        origin=f'http://127.0.0.1:{server.server_port}'
        app=web_fixture(db,executable=True,origin=origin); server.app=app
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(channel='chrome',headless=True)
                try:
                    for without_channel in (False,True):
                        context=browser.new_context()
                        if without_channel: context.add_init_script('window.BroadcastChannel=undefined')
                        account={'email':f'alice{int(without_channel)}@gmail.com','subject':f'alice-{without_channel}','name':'Alice'}
                        requested=[]; errors=[]; csp=[]; unexpected=[]; login_origins=[]; send_keys=[]
                        def network(route):
                            parsed=urlsplit(route.request.url)
                            if parsed.netloc==urlsplit(origin).netloc:
                                requested.append(parsed.path)
                                if parsed.path=='/oauth/login': login_origins.append(route.request.headers.get('origin'))
                                if parsed.path.startswith('/api/') and not parsed.path.startswith(('/api/work/','/api/cloud/','/api/auth/')):
                                    unexpected.append(parsed.path)
                                route.continue_()
                            elif parsed.scheme=='https' and parsed.netloc=='accounts.google.com' and parsed.path=='/o/oauth2/v2/auth':
                                params=parse_qs(parsed.query)
                                assert params['scope']==['openid email profile']
                                fixture=app.extensions['fixture_signer']
                                claims=fixture.valid_claims(account['subject'],account['email'],params['nonce'][0])
                                claims['name']=account['name']
                                app.extensions['fixture_transport'].token=fixture.token(claims)
                                route.fulfill(status=302,headers={'Location':origin+'/oauth/login/callback?state='+params['state'][0]+'&code=synthetic-browser-code'})
                            else:
                                unexpected.append(parsed.netloc+parsed.path); route.abort()
                        context.route('**/*',network)
                        def page_setup(page):
                            page.on('pageerror',lambda error:errors.append(str(error)))
                            page.on('console',lambda msg:csp.append(msg.text) if 'Content Security Policy' in msg.text else None)
                            page.on('request',lambda req:send_keys.append(req.post_data_json['request_key']) if req.method=='POST' and urlsplit(req.url).path.endswith('/messages') else None)
                        page=context.new_page(); page_setup(page)
                        def signin(target):
                            Admission(db).invite(account['email'],'fixture','browser invitation') if not db.read('SELECT owner_id FROM auth_identities WHERE subject=%s',(account['subject'],)) else None
                            target.goto(origin+'/login')
                            target.get_by_role('button',name='Continue with Google').click()
                            try: expect(target.locator('#cloud-profile')).to_contain_text(account['name'],timeout=15000)
                            except AssertionError:
                                print('login boundary:',logs[-8:],'origins:',login_origins,'csp:',csp,flush=True)
                                raise
                            expect(target.locator('#cloud-draft')).to_be_enabled()
                        signin(page)
                        second=context.new_page(); page_setup(second); second.goto(origin+'/chat')
                        expect(second.locator('#cloud-profile')).to_contain_text('Alice')
                        expect(second.locator('#cloud-draft')).to_be_enabled()
                        owner=db.read('SELECT owner_id FROM auth_identities WHERE subject=%s',(account['subject'],))[0]['owner_id']
                        assert len(db.read('SELECT * FROM cloud_conversation_bindings WHERE owner_id=%s',(owner,)))==1
                        cid=db.read('SELECT conversation_id FROM cloud_conversation_bindings WHERE owner_id=%s',(owner,))[0]['conversation_id']
                        page.bring_to_front()
                        page.locator('#cloud-draft').fill('alice-private-message')
                        page.locator('#cloud-send').click()
                        expect(page.locator('[data-work-state="queued"]')).to_have_count(1)
                        job=db.read('SELECT id FROM jobs WHERE owner_id=%s ORDER BY created_at DESC',(owner,))[0]['id']
                        claim=Leases(db).claim(job,'fixture-worker')
                        assert Leases(db).finish(claim,'<img src=x onerror=alert(1)> synthetic reply')
                        expect(page.locator('#cloud-thread')).to_contain_text('synthetic reply',timeout=15000)
                        assert page.locator('#cloud-thread img').count()==0
                        def lose_response(route):
                            route.fetch()
                            route.abort()
                        page.route('**/messages',lose_response,times=1)
                        page.locator('#cloud-draft').fill('alice-ambiguous-message')
                        page.locator('#cloud-send').click()
                        expect(page.locator('#cloud-retry')).to_be_visible()
                        before=db.read('SELECT count(*) AS n FROM jobs WHERE owner_id=%s',(owner,))[0]['n']
                        key=send_keys[-1]
                        page.locator('#cloud-retry').click()
                        expect(page.locator('#cloud-retry')).to_be_hidden()
                        expect(page.locator('#cloud-draft')).to_be_enabled()
                        assert send_keys[-1]==key
                        assert db.read('SELECT count(*) AS n FROM jobs WHERE owner_id=%s',(owner,))[0]['n']==before
                        waiting=db.read("SELECT id FROM jobs WHERE owner_id=%s AND state='queued'",(owner,))
                        for item in waiting:
                            assert Leases(db).finish(Leases(db).claim(item['id'],'fixture-worker'),'synthetic retry reply')
                        # Recreate the web composition, keeping only DB and explicit settings.
                        previous=app.extensions['fixture_settings']
                        app=web_fixture(db,executable=True,origin=origin,settings=previous); server.app=app
                        page.reload()
                        expect(page.locator('#cloud-thread')).to_contain_text('alice-private-message')
                        page.locator('#cloud-draft').fill('alice-private-draft')
                        # An in-flight Alice response arrives only after the cookie switches.
                        held=[]
                        def delayed(route):
                            response=route.fetch()
                            held.append((route,response))
                        page.route('**/api/work/conversations/*',delayed,times=1)
                        Work(db).notice(Actor(owner),cid,'delay-'+str(without_channel),'alice-late-content')
                        # Start a reload-status via reset button is a mutation: instead
                        # reload while preserving a held snapshot ticket.
                        page.reload(wait_until='domcontentloaded')
                        expect(page.locator('#cloud-account')).to_be_visible()
                        # Wait with browser event pumping for the intercepted request.
                        for _ in range(50):
                            if held: break
                            page.wait_for_timeout(100)
                        assert held,'snapshot not intercepted'
                        page.evaluate("document.getElementById('cloud-draft').value='alice-private-draft'")
                        second.bring_to_front()
                        account.update(email=f'bob{int(without_channel)}@gmail.com',subject=f'bob-{without_channel}',name='Bob')
                        signin(second)
                        for route,response in held:
                            try: route.fulfill(response=response)
                            except Exception: pass  # Aborted by visibility/session invalidation.
                        page.bring_to_front()
                        expect(page.locator('#cloud-profile')).to_contain_text('Bob',timeout=10000)
                        expect(page.locator('#cloud-thread')).not_to_contain_text('alice-')
                        expect(page.locator('#cloud-draft')).not_to_have_value('alice-private-draft')
                        expect(page.locator('#cloud-retry')).to_be_hidden()
                        page.goto(origin+'/settings'); page.go_back()
                        expect(page.locator('#cloud-profile')).to_contain_text('Bob')
                        expect(page.locator('#cloud-thread')).not_to_contain_text('alice-')
                        assert page.evaluate('localStorage.length+sessionStorage.length')==0
                        assert page.evaluate('async()=> (await navigator.serviceWorker.getRegistrations()).length')==0
                        assert page.evaluate('async()=> (await caches.keys()).length')==0
                        # Stale context cannot mutate even if a tab has not received a signal.
                        result=page.evaluate("""async () => {
                            const b=await (await fetch('/api/cloud/bootstrap')).json();
                            const r=await fetch('/api/cloud/conversations/ensure',{method:'POST',
                            headers:{'Content-Type':'application/json','X-CSRF-Token':b.csrf,'X-Athena-Context':'stale-context'},
                            body:JSON.stringify({kind:'chat'})});return r.status;}""")
                        assert result==409
                        second.goto(origin+'/settings')
                        expect(second.locator('#cloud-profile')).to_contain_text('Bob')
                        second.locator('#cloud-logout-all').click()
                        second.wait_for_url('**/login')
                        page.bring_to_front(); page.reload()
                        page.wait_for_url('**/login')
                        signin(page)
                        with db.transaction() as tx: tx.execute("UPDATE auth_sessions SET created_at=clock_timestamp()-interval '8 days',expires_at=clock_timestamp()-interval '1 second' WHERE owner_id=(SELECT owner_id FROM auth_identities WHERE subject=%s)",(account['subject'],))
                        page.reload(); page.wait_for_url('**/login')
                        signin(page)
                        assert not errors,errors
                        assert not csp,csp
                        assert not unexpected,unexpected
                        assert not any('/ask-ai' in p or '/settings/sources' in p for p in requested)
                        assert not any('synthetic-browser-code' in line or '?' in line for line in logs)
                        context.close()
                        print('PASS identity Chrome lifecycle; BroadcastChannel disabled='+str(without_channel),flush=True)
                finally: browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            assert not thread.is_alive()


if __name__=='__main__':
    if not os.environ.get('ATHENA_VERIFY_DSN'):
        raise SystemExit('run through verify_cloud.py --identity-browser')
    verify()
