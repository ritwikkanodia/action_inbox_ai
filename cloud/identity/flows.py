"""Shared, bounded OAuth login flows; consumed secrets are never re-persisted."""
import math
from oauthlib.oauth2 import WebApplicationClient
from cryptography.fernet import InvalidToken
from cloud.identity.crypto import checked_ascii, new_token, valid_token, hash_token, seal_verifier, open_verifier
from cloud.identity.guard import lock_runtime
from cloud.identity.types import Forbidden, RateLimited, FlowStart, FlowClaim


class Flows:
    def __init__(self, db, key):
        self.db, self.key = db, key

    def _limit(self, browser_hash):
        retry = 0
        with self.db.transaction() as tx:
            lock_runtime(tx)
            now = tx.execute('SELECT clock_timestamp() AS now').fetchone()['now']
            for key, width, maximum in (('global', 60, 100), ('browser:' + browser_hash, 600, 10)):
                row = tx.execute("""INSERT INTO auth_limits(key,bucket_start,attempts)
                    VALUES (%s,to_timestamp(floor(extract(epoch FROM %s::timestamptz)/%s)*%s),1)
                    ON CONFLICT(key,bucket_start) DO UPDATE
                    SET attempts=least(auth_limits.attempts+1,%s) RETURNING attempts,bucket_start""",
                    (key, now, width, width, maximum + 1)).fetchone()
                if row['attempts'] > maximum:
                    retry = max(retry, math.ceil(width - (now - row['bucket_start']).total_seconds()))
        # Counter changes survive a rejected attempt or later failed insertion.
        if retry:
            raise RateLimited(retry)

    def begin(self, browser_token):
        try:
            checked_ascii(browser_token)
        except ValueError:
            raise Forbidden() from None
        browser_hash = hash_token(browser_token)
        self._limit(browser_hash)
        state, nonce, verifier = (new_token() for _ in range(3))
        challenge = WebApplicationClient('pkce').create_code_challenge(verifier, 'S256')
        with self.db.transaction() as tx:
            runtime = lock_runtime(tx)
            old = tx.execute("""SELECT *,expires_at>clock_timestamp() AS live
                FROM auth_flows WHERE browser_hash=%s FOR UPDATE""", (browser_hash,)).fetchone()
            if old and old['live'] and old['status'] != 'pending':
                raise Forbidden()
            if old:
                tx.execute('DELETE FROM auth_flows WHERE browser_hash=%s', (browser_hash,))
            tx.execute("""INSERT INTO auth_flows(state_hash,browser_hash,nonce_hash,verifier_cipher,
                epoch,status,expires_at) VALUES (%s,%s,%s,%s,%s,'pending',clock_timestamp()+interval '10 minutes')""",
                (hash_token(state), browser_hash, hash_token(nonce), seal_verifier(self.key, verifier), runtime['epoch']))
        return FlowStart(state, nonce, challenge, runtime['epoch'])

    def consume(self, state, browser_token):
        if not valid_token(state):
            raise Forbidden()
        try:
            checked_ascii(browser_token)
        except ValueError:
            raise Forbidden() from None
        with self.db.transaction() as tx:
            runtime = lock_runtime(tx)
            row = tx.execute("""SELECT *,expires_at>clock_timestamp() AS live FROM auth_flows
                WHERE state_hash=%s AND browser_hash=%s FOR UPDATE""",
                (hash_token(state), hash_token(browser_token))).fetchone()
            if not row or not row['live'] or row['status'] != 'pending' or row['epoch'] != runtime['epoch']:
                raise Forbidden()
            try:
                verifier = open_verifier(self.key, bytes(row['verifier_cipher']))
            except (InvalidToken, ValueError, TypeError):
                raise Forbidden() from None
            tx.execute("UPDATE auth_flows SET status='claimed',verifier_cipher=NULL WHERE state_hash=%s",
                       (row['state_hash'],))
        return FlowClaim(row['state_hash'], row['browser_hash'], row['nonce_hash'], verifier, row['epoch'])

    def fail(self, claim):
        with self.db.transaction() as tx:
            tx.execute("""UPDATE auth_flows SET status='failed',verifier_cipher=NULL
                WHERE state_hash=%s AND browser_hash=%s AND epoch=%s AND status='claimed'""",
                (claim.state_hash, claim.browser_hash, claim.epoch))
