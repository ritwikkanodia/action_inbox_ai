"""Real template + Chrome + isolated PostgreSQL; no live profile or provider traffic."""
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


def verify():
    from playwright.sync_api import sync_playwright, expect
    from werkzeug.serving import make_server
    from cloud.app import create_app
    from cloud.types import Actor
    from cloud.work import Work
    from tests.cloud.support import sandbox, browser_fixture
    with sandbox() as db:
        cid = Work(db).create_conversation(Actor('alice'), 'chat')
        app = create_app(db, lambda: Actor('alice'), testing=True)
        browser_fixture(app, cid)
        server = make_server('127.0.0.1',0,app,threaded=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
                try:
                    context = browser.new_context(service_workers='block')
                    def network(route):
                        url = route.request.url
                        if urlparse(url).hostname == '127.0.0.1': route.continue_()
                        elif 'marked.min.js' in url:
                            route.fulfill(content_type='text/javascript',body="window.marked={setOptions(){},parse(s){const e=document.createElement('span');e.textContent=s;return e.innerHTML;}};")
                        else: route.abort()
                    context.route('**/*', network)
                    page = context.new_page()
                    errors, sends = [], []
                    page.on('pageerror',lambda error: errors.append(str(error)))
                    page.on('request',lambda req: sends.append(req.post_data_json) if req.method=='POST' and req.url.endswith('/messages') else None)
                    page.goto(f'http://127.0.0.1:{server.server_port}/chat')
                    page.wait_for_function('window.__DURABLE_WORK === true',timeout=10000)
                    composer, send = page.locator('#ai-followup'), page.locator('#ai-send-btn')
                    for text in ('first synthetic message','second synthetic message'):
                        expect(composer).to_be_enabled()
                        composer.fill(text)
                        send.click()
                        expect(page.locator('#ai-thread')).to_contain_text(text)
                    expect(page.locator('[data-work-state="queued"]')).to_have_count(2)
                    jobs = db.read('SELECT id FROM jobs ORDER BY job_order')
                    assert len(jobs)==2 and jobs[0]['id'] != jobs[1]['id']
                    # Commit succeeded, but the browser never receives its response.
                    def lose_response(route):
                        route.fetch()
                        route.abort()
                    page.route('**/messages',lose_response,times=1)
                    composer.fill('response lost fixture')
                    send.click()
                    expect(page.locator('[data-retry-submission]')).to_have_count(1)
                    lost_key = sends[-1]['request_key']
                    page.locator('[data-retry-submission]').click()
                    expect(page.locator('[data-retry-submission]')).to_have_count(0)
                    assert sends[-1]['request_key']==lost_key
                    assert db.read('SELECT count(*) AS n FROM jobs')[0]['n']==3
                    page.reload()
                    expect(page.locator('[data-work-state="queued"]')).to_have_count(3)
                    # One failed status read must not permanently stop active polling.
                    page.route('**/api/work/conversations/*',lambda route: route.abort(),times=1)
                    Work(db).notice(Actor('alice'),cid,'fixture:notice','Notice while queued')
                    expect(page.locator('#ai-thread')).to_contain_text('Notice while queued',timeout=10000)
                    page.locator(f'[data-cancel-job="{jobs[0]["id"]}"]').click()
                    expect(page.locator('[data-work-state="cancelled"]')).to_have_count(1)
                    page.locator('#chat-new-btn').click()
                    expect(page.locator('#ai-thread')).not_to_contain_text('first synthetic message')
                    page.reload()
                    expect(page.locator('#ai-thread')).not_to_contain_text('response lost fixture')
                    assert Work(db).snapshot(Actor('alice'),cid)['generation']==2
                    assert not errors, errors
                    assert not page.locator('script[src*="pwa.js"]').count()
                    print('PASS Chrome E2E: queued sends, lost-response retry, reload, notice, targeted cancel, reset')
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


if __name__=='__main__':
    if not os.environ.get('ATHENA_VERIFY_DSN'):
        raise SystemExit(subprocess.run([sys.executable,str(ROOT/'scripts/verify/verify_cloud.py'),'--browser']).returncode)
    verify()
