"""One-use browser state, shared rate limiting and recovery fencing."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from cryptography.fernet import Fernet
from cloud.identity.crypto import new_token, hash_token
from cloud.identity.types import Forbidden, RateLimited
from cloud.recovery import Recovery
from cloud.types import Unavailable
from tests.cloud.support import sandbox


class FlowTests(unittest.TestCase):
    def test_consume_scrubs_verifier_and_cannot_replay_or_cross_browser(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            flows = Flows(db,Fernet.generate_key())
            browser = new_token()
            start = flows.begin(browser)
            with self.assertRaises(Forbidden): flows.consume(start.state,new_token())
            claim = flows.consume(start.state,browser)
            self.assertEqual(claim.epoch,start.epoch)
            self.assertEqual(claim.nonce_hash,hash_token(start.nonce))
            row = db.read('SELECT * FROM auth_flows')[0]
            self.assertEqual(row['status'],'claimed')
            self.assertIsNone(row['verifier_cipher'])
            with self.assertRaises(Forbidden): flows.consume(start.state,browser)
            with self.assertRaises(Forbidden): flows.begin(browser)
            flows.fail(claim)
            self.assertEqual(db.read('SELECT status FROM auth_flows')[0]['status'],'failed')

    def test_duplicate_callbacks_have_one_winner(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            key, browser = Fernet.generate_key(), new_token()
            start = Flows(db,key).begin(browser)
            barrier = threading.Barrier(2)
            def consume():
                barrier.wait(timeout=10)
                try:
                    Flows(db,key).consume(start.state,browser)
                    return 'claimed'
                except Forbidden:
                    return 'denied'
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(consume) for _ in range(2)]
                self.assertCountEqual([f.result(timeout=10) for f in futures],['claimed','denied'])

    def test_browser_rate_limit_is_shared_and_rejections_commit_counter(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            key, browser = Fernet.generate_key(), new_token()
            for _ in range(10): Flows(db,key).begin(browser)
            with self.assertRaises(RateLimited) as error: Flows(db,key).begin(browser)
            self.assertGreaterEqual(error.exception.retry_after,1)
            self.assertLessEqual(error.exception.retry_after,600)
            # A separate service cannot reset an over-limit bucket.
            with self.assertRaises(RateLimited): Flows(db,key).begin(browser)
            self.assertEqual(db.read('SELECT count(*) AS n FROM auth_flows')[0]['n'],1)

    def test_global_limit_is_shared_across_fresh_browsers(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            key = Fernet.generate_key()
            # Seed both the present and next bucket to avoid wall-minute races.
            with db.transaction() as tx:
                tx.execute("""INSERT INTO auth_limits VALUES
                    ('global',date_trunc('minute',clock_timestamp()),100),
                    ('global',date_trunc('minute',clock_timestamp())+interval '1 minute',100)""")
            with self.assertRaises(RateLimited): Flows(db,key).begin(new_token())
            self.assertEqual(db.read('SELECT * FROM auth_flows'),[])

    def test_expired_flow_and_recovery_cannot_be_used(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            flows, browser = Flows(db,Fernet.generate_key()), new_token()
            start = flows.begin(browser)
            with db.transaction() as tx: tx.execute("UPDATE auth_flows SET expires_at=clock_timestamp()-interval '1 second'")
            with self.assertRaises(Forbidden): flows.consume(start.state,browser)
            Recovery(db).begin_restore()
            with self.assertRaises(Unavailable): flows.begin(new_token())

    def test_state_and_browser_shape_are_bounded_before_lookup(self):
        from cloud.identity.flows import Flows
        with sandbox() as db:
            flows = Flows(db,Fernet.generate_key())
            for state in ('',None,'é','x'*44):
                with self.assertRaises(Forbidden): flows.consume(state,new_token())
            for browser in ('',None,'é','x'*4097):
                with self.assertRaises(Forbidden): flows.begin(browser)

    def test_duplicate_pipeline_performs_one_token_post(self):
        from cloud.identity.flows import Flows
        from tests.cloud.identity_support import SignedGoogleFixture
        with sandbox() as db:
            flows, browser = Flows(db,Fernet.generate_key()), new_token()
            start = flows.begin(browser)
            fixture = SignedGoogleFixture()
            adapter, _ = fixture.adapter_and_claim(fixture.valid_claims(nonce=start.nonce), expected_nonce=start.nonce)
            barrier = threading.Barrier(2)
            def callback():
                barrier.wait(timeout=10)
                try:
                    claim = flows.consume(start.state,browser)
                    adapter.exchange('synthetic-code',claim)
                    return 'verified'
                except Forbidden:
                    return 'denied'
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(callback) for _ in range(2)]
                self.assertCountEqual([f.result(timeout=10) for f in futures],['verified','denied'])
            self.assertEqual(sum(call[0]=='POST' for call in adapter.transport.calls),1)

    def test_consumed_exchange_failure_is_not_retryable(self):
        from cloud.identity.flows import Flows
        from tests.cloud.identity_support import SignedGoogleFixture
        with sandbox() as db:
            flows, browser = Flows(db,Fernet.generate_key()), new_token()
            start = flows.begin(browser)
            adapter, _ = SignedGoogleFixture().adapter_and_claim({},expected_nonce=start.nonce)
            adapter.transport.failure = Unavailable('identity_provider_unavailable')
            claim = flows.consume(start.state,browser)
            with self.assertRaises(Unavailable): adapter.exchange('synthetic-code',claim)
            flows.fail(claim)
            with self.assertRaises(Forbidden): flows.consume(start.state,browser)
            self.assertEqual(db.read('SELECT * FROM auth_identities'),[])

    def test_restore_during_exchange_prevents_admission(self):
        from cloud.identity.admission import Admission
        from cloud.identity.flows import Flows
        from tests.cloud.identity_support import SignedGoogleFixture
        with sandbox() as db:
            Admission(db).invite('alice@gmail.com','fixture','restore race')
            flows, browser = Flows(db,Fernet.generate_key()), new_token()
            start = flows.begin(browser)
            fixture = SignedGoogleFixture()
            adapter, _ = fixture.adapter_and_claim(fixture.valid_claims(nonce=start.nonce),expected_nonce=start.nonce)
            entered, release = threading.Event(), threading.Event()
            original = adapter.transport.request
            def paused(*args,**kwargs):
                if args[0]=='POST':
                    entered.set()
                    if not release.wait(10): raise AssertionError('exchange not released')
                return original(*args,**kwargs)
            adapter.transport.request = paused
            def callback():
                claim = flows.consume(start.state,browser)
                identity = adapter.exchange('synthetic-code',claim)
                return Admission(db).complete(identity,claim)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(callback)
                self.assertTrue(entered.wait(10))
                try: Recovery(db).begin_restore()
                finally: release.set()
                with self.assertRaises(Unavailable): future.result(timeout=10)
            self.assertEqual(db.read('SELECT * FROM auth_identities'),[])
