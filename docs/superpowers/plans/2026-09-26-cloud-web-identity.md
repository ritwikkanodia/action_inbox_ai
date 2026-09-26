# Cloud Web and Identity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task using the preserved native execution method. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver tested, invite-only Google sign-in and PostgreSQL-backed Athena pages without activating live Gmail, Hermes or Azure.

**Architecture:** Extend the separate cloud Flask application, preserving the local SQLite application's behavior. PostgreSQL owns admission, login flows and sessions; a request-database wrapper checks session authority inside durable-service transactions. Cloud-specific pages reuse Athena CSS/images and durable submission primitives without loading laptop-only handlers.

**Tech Stack:** Python 3.12, PostgreSQL 17, psycopg 3.3.6, Flask 3.1.3, google-auth 2.57.1, OAuthLib 3.3.1, requests 2.34.2, cryptography 50.0.1, Gunicorn 26.2.0, unittest, Node and Chrome/Playwright. New package pins reflect inspected local distributions, not a security clearance.

**Spec:** `docs/superpowers/specs/2026-09-26-cloud-web-identity-design.md`, approved 2026-09-26. Read both documents before implementation.

**Status:** Implementation plan awaiting review. No task below has run. Use branch `codex/cloud-web-identity` in `action_inbox_ai-hermes-cloud-image`, based on merged main `6afed64`. Do not switch the live checkout.

## Global Constraints

The following quoted requirements are copied from the approved spec:

- “Preserve the local checkout, SQLite data, credentials, running services and unrelated worktree files.”
- “Start cloud accounts fresh.”
- “Do not call a live mailbox/model, register OAuth clients, activate sources or provision Azure during implementation of this slice.”
- “Use additive migrations after the existing migration set. Never edit already-applied migrations.”
- “At most 50 seats exist per environment.”
- “An invitation is single-use, expires after seven days, and may be revoked before redemption.”
- “Proposed beta defaults are a seven-day absolute session lifetime, a 24-hour idle limit and at most five active sessions per identity.”
- “Cookie: `__Host-athena-session`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no Domain.”
- “Flow state, nonce and browser tokens contain at least 256 random bits.”
- “No `.env` auto-discovery, random secret fallback, SQLite fallback or implicit database migrations on web startup.”
- “The production composition reports chat execution unavailable until the real executor package is integrated, and rejects submissions rather than collecting an unserviceable backlog.”
- “All implementation verification uses disposable, runner-owned PostgreSQL and synthetic identities.”

Additional execution constraints:

- Use the existing interpreter at `/Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python`. The verification runner installs hash-locked additions into its owned temporary target; do not change that interpreter or create a venv.
- Preserve untracked `deploy/`, image scripts and checkpoint documents. Stage exact task files only.
- No automatic push, PR, merge, cloud spend, provider activation or public release follows from passing tests.
- Native execution remains preferred: implement in this chat, one independent whole-branch review at the end, not task-by-task delegation.

## Review Focus

1. Session resolved before logout but used afterward: the mutation must reauthorize inside its transaction; Task 4 tests both race orders.
2. Two subjects claim one email or compete for seat 50: no identity merge or 51st seat; Task 2 tests these races.
3. Recovery changes epoch during Google exchange: an old claimed flow cannot mint a fresh session; Tasks 3 and 7 test this.
4. Old-tab response/draft arrives after another account signs in: discard it and reject stale-context mutations; Task 6 tests this.
5. Callback secrets leak through access logs or Referer: suppress query/referrer logging and protect error responses; Tasks 5 and 7 test this.

## File map

| Files | Responsibility |
| --- | --- |
| `cloud/identity/{__init__,types,config,crypto}.py` | Explicit settings, value objects, hashing, Fernet and CSRF primitives |
| `cloud/migrations/008_identity.sql` | Admission, identities, sessions, flows, rate limits, audit, default-thread bindings |
| `cloud/identity/{admission,sessions,guard}.py` | Transactional invitations/session authority and guarded database access |
| `cloud/identity/{flows,google,transport}.py` | One-use OAuth flow and bounded Google exchange/verification |
| `cloud/identity/{http,security}.py` | Login/logout handlers, CSRF, origin/host checks and headers |
| `cloud/{web,pages,wsgi}.py` | Real composition, scoped read models and explicit WSGI factory |
| `cloud/identity/cli.py`, `cloud/gunicorn.conf.py` | Operator lifecycle and safe access logging |
| `cloud/{http,work,control}.py` | Extend durable handlers without changing synthetic/local contracts |
| `templates/cloud/{login,shell,error}.html`, `static/css/cloud.css` | Cloud-only pages using Athena assets |
| `static/js/{cloud-state,cloud-app}.js` | Account-safe browser state and controller |
| `tests/cloud/identity_support.py`, `tests/cloud/test_identity_*.py` | Synthetic fixtures and real SQL/provider/HTTP tests |
| `scripts/verify/verify_cloud_identity_ui.cjs`, `verify_cloud_identity_browser.py` | Node contracts and Chrome walkthrough |
| `scripts/verify/verify_cloud.py`, `requirements-cloud.{in,lock}` | Extend the existing isolated verification runner |
| `docs/runbooks/cloud-web-identity.md`, `durable-work-local.md` | Delivery boundary, verification and operations |

Do not modify legacy `auth.py`, `db.py`, `app.py`, `templates/index.html` or `static/js/app.js`. Keep the original `cloud.app.create_app(db, principal=None, testing=False)` synthetic factory behavior. Add production composition separately in `cloud/web.py`.

## Test conventions

All commands run from `/Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai-hermes-cloud-image`.

A focused module runs as:

```sh
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --case tests.cloud.test_identity_schema
```

Replace only the module name with the module specified by a task. Never supply a DSN; the runner owns a disposable PostgreSQL instance and random test schemas. Each task records red, then green evidence. Missing-import failures are preliminary: also demonstrate the meaningful behavioral assertion before declaring a fix.

Use `unittest.TestCase`, existing `sandbox()`, real SQL and event-controlled threads with ten-second timeouts. Do not use sleeps to arrange races. Test cryptographic keys/certificates are generated in memory. Test fixtures do not call actual Google.

Each task ends with a commit of its explicit file list, not `git add .`. Record actual results and final counts; this document's future-tense checks are not test evidence.

## Task 1: Identity schema, configuration and cryptographic primitives

**Files:** Create `cloud/identity/{__init__,types,config,crypto}.py`, `cloud/migrations/008_identity.sql`, `tests/cloud/test_identity_schema.py`, `tests/cloud/identity_support.py`; update `requirements-cloud.in` and regenerate its lock.

**Interfaces produced:** Frozen dataclasses; all secret fields have `repr=False`.

```python
DatabaseSettings(dsn: str, schema: str)
WebSettings(database: DatabaseSettings, environment: str, public_origin: str,
            google_client_id: str, google_client_secret: str, flow_key: bytes)
GoogleIdentity(subject: str, email: str, name: str, hosted_domain: str | None)
SessionProof(owner_id: str, token_hash: str, epoch: UUID, context_id: UUID)
IssuedSession(token: str, proof: SessionProof, csrf: str)
FlowStart(state: str, nonce: str, challenge: str, epoch: UUID)
FlowClaim(state_hash: str, browser_hash: str, nonce_hash: str,
          verifier: str, epoch: UUID)
ProviderResponse(status: int, data: bytes, headers: dict[str, str])

DatabaseSettings.from_mapping(values: Mapping[str, str]) -> DatabaseSettings
WebSettings.from_mapping(values: Mapping[str, str]) -> WebSettings
hash_token(raw: str) -> str
new_token() -> str
csrf_token(raw_cookie: str) -> str
seal_verifier(key: bytes, value: str) -> bytes
open_verifier(key: bytes, value: bytes) -> str
```

Also define fixed-code `AuthenticationRequired`, `Forbidden`, `AdmissionDenied` and `RateLimited` subclasses of `WorkError`; `RateLimited.retry_after` is an integer.

- [ ] Write these regression tests and run `tests.cloud.test_identity_schema` red:

```python
def test_repeat_migration_preserves_existing_owners(self):
    with sandbox() as db:
        before = db.read('SELECT owner_id FROM owners ORDER BY owner_id')
        migrate(db)
        self.assertEqual(before, db.read('SELECT owner_id FROM owners ORDER BY owner_id'))
        self.assertEqual(db.read('SELECT seat_limit FROM auth_admission')[0]['seat_limit'], 50)
        self.assertEqual(db.read('SELECT * FROM auth_identities'), [])

def test_insecure_origin_is_rejected(self):
    for origin in ('http://example.invalid', 'https://u:p@example.invalid',
                   'https://example.invalid/path', 'https://example.invalid?x=1'):
        with self.subTest(origin=origin), self.assertRaises(ValueError):
            WebSettings.from_mapping(dict(test_settings(), ATHENA_PUBLIC_ORIGIN=origin))
```

Create `test_settings()` in test support: synthetic client credentials, `https://athena.example.invalid`, staging environment and generated Fernet key. Config-only DSNs use a nonconnecting `.invalid` host with sslmode=verify-full; app fixtures receive only sandbox DB settings. DatabaseSettings.from_mapping reads just database URL/schema and applies the same DSN validation, so operator commands do not need Google secrets.

- [ ] Add these exact inputs to existing requirements, retaining current psycopg/Service Bus pins, and compile hashes without installing into the interpreter:

```text
Flask==3.1.3
google-auth==2.57.1
oauthlib==3.3.1
requests==2.34.2
cryptography==50.0.1
gunicorn==26.2.0
```

```sh
uv pip compile --python /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python --generate-hashes requirements-cloud.in -o requirements-cloud.lock
```

- [ ] Add this migration. All expiry/creation times use the DB clock, not browser time:

```sql
CREATE TABLE auth_admission (
  singleton boolean PRIMARY KEY CHECK(singleton),
  seat_limit integer NOT NULL CHECK(seat_limit=50)
);
INSERT INTO auth_admission VALUES (true,50);
CREATE TABLE auth_identities (
  owner_id text PRIMARY KEY REFERENCES owners,
  provider text NOT NULL CHECK(provider='google'),
  subject text NOT NULL CHECK(length(subject) BETWEEN 1 AND 255),
  email text NOT NULL CHECK(length(email) BETWEEN 3 AND 320),
  display_name text NOT NULL CHECK(length(display_name)<=200),
  disabled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(provider,subject)
);
CREATE TABLE auth_invites (
  id uuid PRIMARY KEY, email text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL, revoked_at timestamptz,
  redeemed_owner_id text UNIQUE REFERENCES owners,
  CHECK(email=lower(btrim(email))), CHECK(expires_at>created_at)
);
CREATE TABLE auth_sessions (
  token_hash text PRIMARY KEY CHECK(length(token_hash)=64),
  owner_id text NOT NULL REFERENCES auth_identities(owner_id),
  epoch uuid NOT NULL, context_id uuid NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL, revoked_at timestamptz,
  CHECK(expires_at>created_at)
);
CREATE INDEX auth_sessions_owner ON auth_sessions(owner_id);
CREATE INDEX auth_sessions_expiry ON auth_sessions(expires_at);
CREATE TABLE auth_flows (
  state_hash text PRIMARY KEY CHECK(length(state_hash)=64),
  browser_hash text NOT NULL UNIQUE CHECK(length(browser_hash)=64),
  nonce_hash text NOT NULL CHECK(length(nonce_hash)=64),
  verifier_cipher bytea, epoch uuid NOT NULL,
  status text NOT NULL CHECK(status IN ('pending','claimed','finished','failed')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  CHECK((status='pending')=(verifier_cipher IS NOT NULL))
);
CREATE INDEX auth_flows_expiry ON auth_flows(expires_at);
CREATE INDEX auth_invites_expiry ON auth_invites(expires_at);
CREATE TABLE auth_limits (
  key text NOT NULL, bucket_start timestamptz NOT NULL,
  attempts integer NOT NULL CHECK(attempts>0),
  PRIMARY KEY(key,bucket_start)
);
CREATE INDEX auth_limits_bucket ON auth_limits(bucket_start);
CREATE TABLE auth_audit (
  id uuid PRIMARY KEY, action text NOT NULL, owner_id text,
  actor text NOT NULL, reason text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE cloud_conversation_bindings (
  owner_id text NOT NULL REFERENCES owners, slot text NOT NULL,
  conversation_id uuid NOT NULL,
  PRIMARY KEY(owner_id,slot), UNIQUE(owner_id,conversation_id),
  FOREIGN KEY(owner_id,conversation_id) REFERENCES conversations(owner_id,id)
);
```

Flow tombstones retain hashes/status until expiry, never the consumed verifier. No raw token/code columns.

- [ ] Implement explicit settings for `ATHENA_DATABASE_URL`, `ATHENA_DATABASE_SCHEMA`, `ATHENA_ENVIRONMENT`, `ATHENA_PUBLIC_ORIGIN`, `ATHENA_GOOGLE_CLIENT_ID`, `ATHENA_GOOGLE_CLIENT_SECRET`, `ATHENA_AUTH_FLOW_KEY`. Require staging/production environment, canonical HTTPS origin without userinfo/path/query/fragment, valid Fernet key, identifier-safe schema and a remote DSN with `sslmode=verify-full`. Reject DSN options overriding search_path. Test-only helpers construct loopback settings directly, never through a production bypass flag.

Implement crypto using standard libraries:

```python
def new_token():
    return secrets.token_urlsafe(32)

def hash_token(raw):
    return hashlib.sha256(raw.encode('ascii')).hexdigest()

def csrf_token(raw_cookie):
    return hmac.new(raw_cookie.encode('ascii'), b'athena:csrf:v1', hashlib.sha256).hexdigest()

def seal_verifier(key, value):
    return Fernet(key).encrypt(value.encode('ascii'))
```

Validate shape/length before these calls; compare secrets with `hmac.compare_digest`; decrypt with Fernet. Config errors/repr must not include supplied DSNs or secrets.

- [ ] Add schema-upgrade test: apply through 007 in a disposable schema, insert one owner/conversation/job, then apply 008 and assert rows/checksums unchanged. Test missing settings, bad key/schema/environment, FK failures and secret-safe repr. Run module green; commit `feat: add cloud identity schema and configuration`.

## Task 2: Atomic admission and restart-safe sessions

**Files:** Create `cloud/identity/{admission,sessions,guard}.py`, `tests/cloud/test_identity_admission.py`, `test_identity_sessions.py`; extend test support.

**Interfaces:**

```python
Admission(db).invite(email: str, actor: str, reason: str) -> UUID
Admission(db).revoke_invite(invite_id: UUID, actor: str, reason: str) -> None
Admission(db).complete(identity: GoogleIdentity, flow: FlowClaim,
                       previous_token: str | None = None) -> IssuedSession
Sessions(db).resolve(raw_token: str) -> SessionProof
Sessions(db).revoke(proof: SessionProof, *, all_sessions: bool = False) -> None
Sessions(db).disable(owner_id: str, actor: str, reason: str) -> None
Sessions(db).revoke_owner(owner_id: str, actor: str, reason: str) -> None
Sessions(db).purge() -> dict[str, int]
lock_runtime(tx) -> dict
lock_session(tx, proof: SessionProof, *, touch: bool = True) -> dict
```

`lock_runtime` obtains runtime FOR SHARE. A current-epoch recovery-audit begin without a resume is a hold, returning Unavailable; ordinary disabled work without a hold permits login/read-only activity. `lock_session` orders runtime → owner → session and rechecks owner/identity enabled, expiry/idle/revocation and epoch.

- [ ] Add `seed_claim(db)`: insert a synthetic unexpired claimed flow with current epoch and null verifier, return its FlowClaim. `admit_fixture(db, email='alice@gmail.com', subject='alice-sub')` creates an invitation, then calls real Admission.complete with that claim and a GoogleIdentity. It returns IssuedSession, not a fake principal.
- [ ] Write and run both modules red:

```python
def test_email_cannot_merge_distinct_subjects(self):
    with sandbox() as db:
        issued = admit_fixture(db)
        with self.assertRaises(AdmissionDenied):
            Admission(db).complete(
                GoogleIdentity('other-sub','alice@gmail.com','Other',None), seed_claim(db))
        self.assertEqual(db.read('SELECT count(*) AS n FROM auth_identities')[0]['n'],1)
        self.assertEqual(Sessions(db).resolve(issued.token).owner_id, issued.proof.owner_id)

def test_new_service_resolves_session_but_logout_revokes_it(self):
    with sandbox() as db:
        issued = admit_fixture(db)
        self.assertEqual(Sessions(db).resolve(issued.token), issued.proof)
        Sessions(db).revoke(issued.proof)
        with self.assertRaises(AuthenticationRequired):
            Sessions(db).resolve(issued.token)
```

- [ ] Implement exact-email normalization (trim/lowercase, no alias folding). Validate ASCII email length 3–320, one @, nonempty local/domain and no whitespace/control characters. Under runtime SHARE → admission FOR UPDATE, count redeemed identities plus live unredeemed reservations; cap at 50. Same-email reservation refresh does not double-count; redeemed email cannot be reissued. Expired/revoked unredeemed reservation may be renewed seven days. Actor/reason limits: 1–128/1–500 characters; audit fixed action names and opaque IDs.
- [ ] Implement completion with runtime → admission → all involved owners in lexical order → identity/session → claimed flow. Include prior-session owner in that lock set before issuing a replacement; re-read all routing hints under locks. Require claimed, unexpired, current-epoch flow. For a new subject require an unredeemed live invite and authoritative Gmail (`gmail.com`) or verified Workspace hosted-domain identity; existing enabled subject can update display metadata without new admission. Never merge by email.

Core subject lookup:

```python
known = tx.execute(
    "SELECT * FROM auth_identities WHERE provider='google' AND subject=%s",
    (identity.subject,)).fetchone()
invite = None if known else tx.execute(
    'SELECT * FROM auth_invites WHERE email=%s FOR UPDATE',
    (identity.email.strip().lower(),)).fetchone()
```

Insert random owner ID/identity only after all checks. Issue random session/hash/context ID; seven-day absolute expiry, 24-hour idle, current epoch. Revoke oldest active sessions ordered by (created_at,token_hash) so issuing one leaves five. Revoke supplied prior session. Mark flow finished and redeem invite in the same commit; no successful cookie on commit failure.

- [ ] Implement resolve/revoke/disable with ordered locks; raw session tokens must be 43 base64url characters. Update last_seen inside the checked transaction. Disable sets owner.enabled=false, identity.disabled_at and session revocations. Purge expired flow/limit rows and expired/revoked sessions in batches of 500; retain identities, invitations and audit. Do not hold locks over provider calls.

Session validity SQL conditions:

```sql
s.revoked_at IS NULL AND i.disabled_at IS NULL AND o.enabled
AND s.epoch=r.epoch AND s.expires_at>clock_timestamp()
AND s.last_seen_at+interval '24 hours'>clock_timestamp()
```

- [ ] Test 49 reservations plus two concurrent new invites yields one winner; duplicate redemption yields one account; two valid flows for same subject never duplicate identity; third-party email admission rejected; enabled subject survives email change; dot/plus aliases stay distinct; expired/revoked invites; sixth-session eviction; idle/absolute boundaries; rollback leaves no seat/session; owner disable stops reads/sign-in. Set DB times directly and use thread events. Run both modules green; commit `feat: enforce beta admission and revocable sessions`.

## Task 3: One-use OAuth and verified Google identity

**Files:** Create `cloud/identity/{flows,google,transport}.py`, `tests/cloud/test_identity_flows.py`, `test_identity_google.py`; extend test support.

**Interfaces:**

```python
Flows(db, key: bytes).begin(browser_token: str) -> FlowStart
Flows(db, key: bytes).consume(state: str, browser_token: str) -> FlowClaim
Flows(db, key: bytes).fail(claim: FlowClaim) -> None
GoogleAdapter(settings: WebSettings, transport: BoundedGoogleTransport)
GoogleAdapter.authorization_url(start: FlowStart) -> str
GoogleAdapter.exchange(code: str, claim: FlowClaim) -> GoogleIdentity
BoundedGoogleTransport.request(method: str, url: str, *, body: bytes | None = None,
                               headers: dict[str,str] | None = None) -> ProviderResponse
BoundedGoogleTransport.certificate_request(url: str, method: str = 'GET',
    body=None, headers=None, timeout=None) -> ProviderResponse
```

- [ ] Write and run `tests.cloud.test_identity_flows` red:

```python
def test_callback_consumption_removes_secret_and_prevents_replay(self):
    with sandbox() as db:
        flows = Flows(db,Fernet.generate_key())
        browser = new_token()
        start = flows.begin(browser)
        claim = flows.consume(start.state,browser)
        self.assertEqual(claim.epoch,start.epoch)
        self.assertIsNone(db.read('SELECT verifier_cipher FROM auth_flows')[0]['verifier_cipher'])
        with self.assertRaises(Forbidden):
            flows.consume(start.state,browser)
```

- [ ] Implement committed rate counters before flow creation: ten starts/browser/ten-minute fixed bucket; 100 globally/minute; DB clock; global lock precedes browser lock. Increment in its own transaction and raise RateLimited after commit, so rejected attempts do not undo the counter. Retry-After is remaining bucket time rounded up. Do not trust forwarded IP.
- [ ] Generate independent state/nonce/verifier; use OAuthLib S256 support. Store hashes, encrypted verifier, current epoch and ten-minute expiry. One flow/tombstone per browser hash; used/claimed browser tokens require a restarted login with a fresh token, which still counts globally. Consume under runtime → flow locks: match hashes/status/expiry, decrypt into memory, set claimed with null ciphertext. Wrong-browser/replay never reaches Google. Failure changes claimed to failed, without restoring secrets.

Claim SQL shape:

```sql
UPDATE auth_flows SET status='claimed',verifier_cipher=NULL
WHERE state_hash=%s AND browser_hash=%s AND status='pending'
AND epoch=%s AND expires_at>clock_timestamp()
```

Read/return the encrypted payload under the same lock before clearing it; this statement is not a separate transaction.

- [ ] Create `SignedGoogleFixture` in tests: in-memory RSA key/self-signed X.509 certificate via cryptography; `valid_claims(subject,email,nonce)`, `token(claims)` using google.auth.jwt.encode/RSASigner, and `adapter_and_claim(claims, *, expected_nonce)` returning a real GoogleAdapter with fake transport and the explicit expected nonce hash. `FakeGoogleTransport` implements both transport methods, returns token/certificate responses and records calls in memory. No mocked success verifier.
- [ ] Write and run `tests.cloud.test_identity_google` red:

```python
def test_real_verifier_rejects_wrong_nonce(self):
    fixture = SignedGoogleFixture()
    claims = fixture.valid_claims('alice-sub','alice@gmail.com','wrong')
    adapter, claim = fixture.adapter_and_claim(claims, expected_nonce='expected')
    with self.assertRaises(Forbidden):
        adapter.exchange('synthetic-code',claim)
```

- [ ] Implement authorization/exchange using maintained library APIs, with a fresh OAuthLib client per request:

```python
client = WebApplicationClient(settings.google_client_id)
url = client.prepare_request_uri(
    'https://accounts.google.com/o/oauth2/v2/auth',
    redirect_uri=settings.public_origin+'/oauth/login/callback',
    scope=['openid','email','profile'], state=start.state, nonce=start.nonce,
    code_challenge=start.challenge, code_challenge_method='S256')
body = client.prepare_request_body(
    code=code, redirect_uri=settings.public_origin+'/oauth/login/callback',
    code_verifier=claim.verifier, client_secret=settings.google_client_secret)
claims = google.oauth2.id_token.verify_oauth2_token(
    token, transport.certificate_request, audience=settings.google_client_id,
    clock_skew_in_seconds=0)
```

Parse the bounded token response through OAuthLib; do not retain access/refresh/ID tokens. Fixed endpoints only: Google token endpoint and Google's X.509 certificate endpoint; no user-supplied discovery/JWT URLs. Require scalar matching aud, matching azp if present, supported Google issuer, verified RS256 signature, integer non-boolean iat/exp with iat <= now < exp, ASCII sub 1–255, matching nonce hash, boolean email_verified=True and bounded email/name/optional hosted domain. Name defaults to email. Tolerate unrelated additional claims.

- [ ] Implement requests transport: TLS verification enabled, trust_env=False, no redirects/retries, connect/read timeouts three seconds each. Stream one-byte chunks; cap decompressed body at 65,536 bytes and check a 15-second monotonic budget, allowing at most one read-timeout overrun. Reject non-success/redirect, bad JSON and oversize Content-Length/body. Code limit 4,096 chars; ID token 16,384 bytes; state exactly 43 base64url chars; callback query limit 8,192 bytes; duplicate security parameters rejected. Cache only fixed-URL certs with TTL min(max-age,3600), protected by a lock; expired cache cannot bypass network/verification errors. Reject unsupported token algorithm before verification; never trust that unverified header for authority.
- [ ] Test all bad claims/signatures, expired/unknown certificates, malicious endpoint hints, harmless extra claims, size/timeout/redirect failures, duplicate callbacks (one token POST), denied consent and consumed-flow exchange failure. Pause exchange, begin restore, then assert complete refuses old epoch and creates no account. Run both modules green; commit `feat: add browser-bound Google identity flow`.

## Task 4: Transactional session authorization for durable APIs

**Files:** Extend guard; modify `cloud/work.py`, `cloud/control.py`, `cloud/http.py`; create `tests/cloud/test_identity_guard.py`.

**Interfaces:**

```python
RequestDatabase(db: Database, proof: Callable[[],SessionProof])
RequestDatabase.transaction() -> ContextManager[psycopg.Connection]
RequestDatabase.read(query: str, params=()) -> list[dict]
create_conversation_in(tx, actor: Actor, kind: str, todo_id: UUID | None = None,
                       *, require_enabled: bool = True) -> UUID
register_routes(app, db, principal, *, submit_gate=None, include_ready=True) -> None
```

Defaults retain existing synthetic tests. Production supplies guarded DB, rejecting submit gate and its own /ready route. Guard does not open nested connections.

- [ ] Write and run `tests.cloud.test_identity_guard` red:

```python
def test_resolved_session_cannot_write_after_logout(self):
    with sandbox() as db:
        issued = admit_fixture(db)
        guarded = RequestDatabase(db,lambda: issued.proof)
        actor = Actor(issued.proof.owner_id)
        cid = Work(guarded).create_conversation(actor,'chat')
        Sessions(db).revoke(issued.proof)
        with self.assertRaises(AuthenticationRequired):
            Work(guarded).accept(actor,Submission(cid,1,uuid4(),'must not commit'))
        self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'],0)
```

- [ ] Implement guard using the base DB transaction:

```python
@contextmanager
def transaction(self):
    proof = self.proof()
    with self.db.transaction() as tx:
        lock_session(tx,proof)
        yield tx
```

Guarded reads also use this context. Production derives Actor from the same request proof, never client input. Existing Work owner locks reenter on the same connection.
- [ ] Extract Work.create_conversation's inner transaction into `create_conversation_in`; preserve kind/ownership validation. Public Work method retains require_enabled=True. Cloud page binding creation can pass False after session authorization, allowing an empty thread while processing is paused without accepting a job.
- [ ] Before Control.cancel calls locked_job, check ownership inside the transaction:

```python
if not tx.execute('SELECT id FROM jobs WHERE id=%s AND owner_id=%s',
                  (job_id,actor.owner_id)).fetchone():
    raise NotFound('job_not_found')
```

This avoids acquiring Bob's owner lock after the guard already locked Alice. Preserve all effect/reconciliation behavior.
- [ ] Add keyword-only route submit gate and include_ready flag. Gate runs after authentication but before accepting work; production always raises Unavailable('executor_not_integrated'). Map auth to 401, forbidden/admission to 403, limiter to 429 plus Retry-After; retain durable status meanings.
- [ ] Pin both mutation/logout race orders with real connections/events. Held guarded mutation must commit before blocked logout; committed logout makes a queued mutation fail without jobs/outbox. Test simultaneous cross-owner cancel guesses yield 404 without deadlock, owner disable on snapshots, restore epoch changes and ordinary disabled-work reads. Run guard plus existing HTTP/acceptance/leases/recovery modules green; commit `fix: fence cloud requests against session revocation`.

## Task 5: Production composition, HTTP identity and safe logging

**Files:** Create `cloud/identity/{http,security}.py`, `cloud/{web,wsgi}.py`, `cloud/gunicorn.conf.py`, `templates/cloud/{login,error}.html`, `tests/cloud/test_identity_http.py`; extend test support.

**Interfaces:**

```python
create_web_app(settings: WebSettings) -> Flask
create_from_env() -> Flask
register_identity_routes(app, db, settings, flows, admission, sessions, google) -> None
install_security(app, settings) -> None
resolve_request(sessions: Sessions) -> SessionProof
require_csrf(raw_cookie: str, supplied: str, origin: str | None, referer: str | None,
             public_origin: str) -> None
```

Production constructs real adapters itself: no fake provider/principal/executor arguments or environment toggles. Test support provides `web_fixture(db, executable=False)` using identical route/services with explicitly test-only transport, loopback origin/cookie allowances and an optional synthetic submit gate. `login_fixture(client,db,email,subject)` drives invite → login form → POST start → fake signed-token callback without following the final redirect. It reads the issued cookie from the test client, resolves it through Sessions, and returns `{'csrf': csrf_token(cookie), 'context_id': str(proof.context_id)}`. It therefore works before Task 6 creates the pages and does not bypass the login handler.

- [ ] Write and run `tests.cloud.test_identity_http` red:

```python
def test_production_cannot_select_fake_provider(self):
    with self.assertRaises(TypeError):
        create_web_app(WebSettings.from_mapping(test_settings()),google=object())

def test_logout_requires_csrf_and_origin(self):
    with sandbox() as db:
        client = web_fixture(db).test_client()
        login_fixture(client,db,'alice@gmail.com','alice-sub')
        self.assertEqual(client.post('/logout').status_code,403)
        self.assertEqual(client.post('/logout',headers={'Origin':'https://evil.invalid'}).status_code,403)
```

- [ ] Construct explicit Database/settings/real adapter/services and RequestDatabase using request-local proof. Register identity/durable handlers; production gate rejects submission, include_ready=False. WSGI exposes create_from_env factory; do not construct app, migrate or load dotenv on import.
- [ ] GET /login uses itsdangerous.URLSafeTimedSerializer, salt athena-prelogin-v1, flow key and a random 256-bit payload. Validate max_age=600 on server; only reuse cookies whose flow is absent or pending. Claimed/finished/failed flows get a fresh browser token on restart. CSRF derives from the full signed cookie, which has the spec's host-only flags. POST login validates CSRF and exact Origin before flow creation. Callback consumes state/browser binding first; exchange outside DB locks; complete under admission locks; only successful commit sets session cookie, deletes pre-login cookie and redirects to fixed /chat.
- [ ] POST logout and logout-all require session, CSRF and matching context. Invalid sessions clear cookies and return API401 or HTML login redirect; DB failure is 503, never a false successful logout. Clear browser display on an unconfirmed logout but explain the server could not confirm revocation. Provider denial/error renders a clean restart page; never expose raw exceptions.
- [ ] Install host/origin/CSRF enforcement, no CORS, bounded body limits and headers:

```python
headers = {
    'Cache-Control':'no-store',
    'X-Content-Type-Options':'nosniff',
    'X-Frame-Options':'DENY',
    'Referrer-Policy':'same-origin',
    'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
}
```

Login/OAuth use no-referrer. Production HTTPS adds HSTS max-age=31536000 without preload/includeSubDomains. No unsafe-inline/eval, arbitrary profile image loads or unrestricted ProxyFix. Use configured origin for redirects. Actual trusted ingress/TLS policy is a deployment gate; never trust arbitrary forwarded headers. Missing Origin may use exact-origin Referer except login POST, which must supply Origin. Forms include hidden CSRF/context; JSON mutations require JSON and X-CSRF-Token/X-Athena-Context. Context mismatch returns 409 session_context_changed before mutation.

Gunicorn safe format:

```python
access_log_format = '%(m)s %(U)s %(s)s %(L)s'
```

Exclude query, raw request line, Referer, cookies and raw provider errors. Do not enable Flask debug in production.
- [ ] Add /live process-only and /ready bounded DB/schema/identity-store checks. Missing tables or checksum mismatch yields 503; never auto-migrate. Recovery hold yields 503; unavailable executor alone does not fail web readiness. Health routes expose no DSN/provider secrets.
- [ ] Test full signed-token login, commit rollback, cookie issue/deletion attributes, POST-only logout, account disable, bad Host/proxy/Origin/Referer/CSRF, duplicate callback fields, arbitrary return URLs, malformed/oversized input, HTML versus API errors and production submit refusal. Capture application/Gunicorn logs with synthetic secret markers in callback query and verify absence. Run module green; commit `feat: expose secure cloud identity routes`.

## Task 6: Real cloud pages and account-safe browser state

**Files:** Create `cloud/pages.py`, `templates/cloud/shell.html`, `static/css/cloud.css`, `static/js/{cloud-state,cloud-app}.js`, `tests/cloud/test_identity_pages.py`, `scripts/verify/verify_cloud_identity_ui.cjs`; extend web composition/test fixture.

**Interfaces:**

```python
Pages(request_db, cursor_key: bytes).bootstrap(actor: Actor, proof: SessionProof,
                                             cursor: str | None = None) -> dict
Pages(request_db, cursor_key: bytes).detail(actor: Actor, todo_id: UUID) -> dict
Pages(request_db, cursor_key: bytes).ensure(actor: Actor, kind: str,
                                          todo_id: UUID | None = None) -> UUID
register_page_routes(app, pages: Pages, sessions: Sessions) -> None
```

Routes: authenticated /, /chat, /settings; GET /api/cloud/bootstrap; GET /api/cloud/todos/<uuid:todo_id>; POST /api/cloud/conversations/ensure. Bootstrap keys: profile(name,email), context_id, csrf, capabilities(chat_execute,gmail_connect), todos,next_cursor,conversation_id. Production capabilities false. No owner/token hash/raw provider data. Todo allowlist: id,title,status,source,importance,due_date,suggested_action.

- [ ] Write and run `tests.cloud.test_identity_pages` red:

```python
def test_two_tabs_share_primary_thread(self):
    with sandbox() as db:
        issued = admit_fixture(db)
        pages = Pages(RequestDatabase(db,lambda: issued.proof),b'x'*32)
        actor = Actor(issued.proof.owner_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(pages.ensure,actor,'chat') for _ in range(2)]
            ids = [f.result(timeout=10) for f in futures]
        self.assertEqual(ids[0],ids[1])
        self.assertEqual(db.read('SELECT count(*) AS n FROM cloud_conversation_bindings')[0]['n'],1)
```

- [ ] Implement ensure within one guarded transaction: verify kind/todo ownership, owner-serialize slot chat or todo:<uuid>, reuse bound conversation; otherwise adopt the lowest-UUID compatible existing conversation or call create_conversation_in with require_enabled=False, then insert binding. Never open a nested Work transaction. GETs never create bindings or jobs.
- [ ] List owned todos ascending (created_at,id), fetch 51 to return 50, and create a signed 24-hour cursor with itsdangerous salt athena-todo-cursor-v1 containing version=1,owner,timestamp,id. Cap cursor at 1,024 chars; check signature, types and owner before SQL. Foreign/invalid cursor → generic400, non-owned detail →404. Render no source_meta or ingested bodies.
- [ ] Build cloud-only shell with existing app.css, Athena mark/favicon, a small cloud.css and existing CSS fallback font stacks. No runtime fonts/marked CDN dependencies, inline handlers or scripts. External durable-work.js,cloud-state.js,cloud-app.js only. Escaped text/textContent for all messages/tasks. Accessible nav, todo view, chat, reset/cancel and logout controls; Gmail explicitly unavailable. No manual task edits, suggestions, source toggles, push, uploads or executor controls.
- [ ] Create side-effect-free browser/CommonJS exports and tests before controller code:

```javascript
// Exported signatures and state:
createState(contextId) // {contextId,generation:0,pending:null,suspended:false}
changeContext(state,contextId) // clear pending; increment generation
beginRequest(state) // immutable {contextId,generation}
acceptResponse(state,ticket) // same context/generation and not suspended
suspend(state) // increment generation, clear pending, suspended=true
resume(state,contextId) // reset context and suspended=false
```

```javascript
const assert = require('node:assert/strict');
const state = require('../../static/js/cloud-state.js');
const s = state.createState('alice-session');
const ticket = state.beginRequest(s);
state.changeContext(s,'bob-session');
assert.equal(state.acceptResponse(s,ticket),false);
assert.equal(s.pending,null);
```

- [ ] Implement controller with same-origin fetch, in-memory drafts only, CSRF and context headers on mutations. On 401/context mismatch clear sensitive DOM/pending/draft and revalidate; never replay automatically. BroadcastChannel carries only session-changed signals; receiving one suspends/clears before fetching authority. Blank sensitive content and invalidate tickets on pagehide/visibility loss; revalidate before display on pageshow/visibility gain. Recheck bootstrap every 30 seconds while visible. Abort old requests and independently discard stale tickets; a successful old response must not render into a new context. If BroadcastChannel is unavailable, revalidate bootstrap before applying an asynchronous conversation response as well as before sending. The server still rejects stale-context mutations independently; do not promise instantaneous erasure of content already displayed in another tab.
- [ ] Use durable request keys/receipt contracts: explicit user retry of ambiguous same-context submission keeps key; definitive rejection permits draft recovery; reset/stale generation never resends automatically. Poll active jobs at 1,2,5,10-second backoff, stop terminal polling. Production composer disabled with explanation and server rejection. LocalStorage/sessionStorage carry no account content; no service worker registered.
- [ ] Test foreign IDs/cursors, pagination ties, ordinary work pause, GET nonmutation, simultaneous ensure, unsafe HTML, all hidden controls, stale-context mutation and failed API responses. Run Node script plus pages/HTTP modules green; commit `feat: add account-isolated cloud pages`.

## Task 7: Browser, recovery and full regression evidence

**Files:** Create `tests/cloud/test_identity_recovery.py`, `scripts/verify/verify_cloud_identity_browser.py`; extend runner/test fixtures and focused tests.

**Interfaces:** Add mutually exclusive runner `--identity-browser`; propagate through child exactly as existing --browser, invoking the named script only. Do not broaden DSN or egress permissions. Browser helper uses the real services/routes with fake signed Google transport, not a bypass principal.

- [ ] Write and run recovery module red:

```python
def test_restore_blocks_old_session_and_claimed_flow(self):
    with sandbox() as db:
        issued = admit_fixture(db)
        claim = seed_claim(db)
        Recovery(db).begin_restore()
        with self.assertRaises((AuthenticationRequired,Unavailable)):
            Sessions(db).resolve(issued.token)
        with self.assertRaises(Unavailable):
            Admission(db).complete(GoogleIdentity('new','new@gmail.com','New',None),claim)
        self.assertEqual(db.read('SELECT count(*) AS n FROM auth_identities')[0]['n'],1)
```

After review/resume, assert old epoch sessions/flows still fail but fresh login for a nonrevoked identity works. Extend disposable dump/restore test with identity state; keep ingress disabled, reapply a synthetic independently retained revocation and show disabled identity cannot return after resume. This is not an Azure RPO/RTO test.
- [ ] Implement Chrome walkthrough with new isolated profile, loopback-only routes and runner Python egress denial. Use a local HTTPS fixture or explicit test-only loopback cookie mode; separately test production Secure cookies over Flask HTTPS requests. Never use personal Chrome data.
- [ ] Cover fresh invite/login, two tabs/same conversation, synthetic queued/completed reply, app recreation with same DB/session, expiry/relogin, logout-all, Alice→Bob switch, back navigation and delayed Alice response arriving after Bob login. Capture JS/CSP errors, failed/unexpected endpoint requests; fail on any local-only /ask-ai or /settings/sources call.

Browser assertions:

```python
page.locator('[data-chat-input]').fill('alice-private-draft')
# Drive logout/Bob login through the second page's real routes.
page.bring_to_front()
expect(page.locator('[data-chat-input]')).not_to_have_value('alice-private-draft')
expect(page.locator('[data-account-name]')).to_contain_text('Bob')
assert not any('/ask-ai' in url or '/settings/sources/' in url for url in requested_urls)
```

Repeat without BroadcastChannel and assert stale-context mutation cannot commit. Release a held Alice response after context change; assert no Alice content/draft/retry control appears. Check no account browser storage, no new service worker, no-store/no-referrer on success/errors and secret-safe access logs. A synthetic worker is strictly fixture-only.
- [ ] Run and record all final gates:

```sh
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --all
node scripts/verify/verify_durable_work_ui.cjs
node scripts/verify/verify_cloud_identity_ui.cjs
node --check static/js/cloud-state.js
node --check static/js/cloud-app.js
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --browser
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --identity-browser
```

Fix demonstrated failures in owning task files, rerun focused and final checks, retain exact counts. Commit `test: verify cloud identity and browser lifecycle`.

## Task 8: Operator tools and delivery runbook

**Files:** Create `cloud/identity/cli.py`, `tests/cloud/test_identity_cli.py`, `docs/runbooks/cloud-web-identity.md`; update `docs/runbooks/durable-work-local.md`.

**Interfaces:** `main(argv: list[str], db=None) -> int` permits db injection only from tests, not CLI options. Commands: invite, revoke-invite, disable-user, revoke-sessions, purge, migrate. Operator commands read explicit DB settings only, not Google/model credentials. Account mutations require --actor and --reason.

- [ ] Write and run CLI module red:

```python
def test_invite_requires_audit_reason(self):
    with sandbox() as db, self.assertRaises(SystemExit):
        main(['invite','--email','alice@gmail.com','--actor','operator'],db=db)
```

- [ ] Map CLI to Admission/Sessions and explicit migrate. No command enables execution, increases seat cap, re-enables disabled users or claims account-data deletion. Operator revoke uses Sessions.revoke_owner with runtime→owner→sessions locks and audit. Purge prints bounded counts; other output is fixed result code and opaque IDs, never DSN/token. Test missing actor/reason, malformed email/ID, repeated revocation, disabled user, bounded purge and DB errors.

Explicit migration dispatch:

```python
if args.command == 'migrate':
    migrate(db)
    return 0
```

No other subcommand migrates automatically.
- [ ] Document exact keys, synthetic test commands, operator semantics, approved limits, identity/Gmail separation, trusted ingress/callback registration requirements and credential rotation. Future configured-cloud startup commands are:

```sh
python -m cloud.identity.cli migrate
gunicorn --config cloud/gunicorn.conf.py 'cloud.wsgi:create_from_env()'
```

Do not execute these against personal/local data. Explain rotating the flow key invalidates in-flight login, while browser-session revocation requires session controls/epoch change; changing DB environment must never reuse browser secrets/origin. Describe restore hold, external revocation evidence and explicit resume.
- [ ] Update old runbook's delivery boundary to distinguish original synthetic factory from new web composition. Retain historical test evidence but label it historical. New evidence includes only actually run tests, no claim of live Google verification, Gmail/Hermes execution, Azure deployment, security certification or achieved RPO/RTO. List remaining build/CI/provider/deletion/cost/restore/release gates.
- [ ] Run CLI green, all Task 7 final gates, git diff --check and staged-path/secret review. Commit `docs: document cloud identity operations and boundaries`.
- [ ] Follow executing-plans' one independent final whole-branch review for native execution. Supply approved spec/plan, exact diff and actual test evidence. Fix confirmed findings with regressions; report deferred issues. No task-by-task agents or automatic push/merge.

## Plan self-review and handoff

Coverage: spec 1–2 → constraints/file map; 3 → Tasks 1/3/5; 4 → Tasks 1/2/8; 5 → Tasks 2/4/5/7; 6 → Tasks 4/6/7; 7 → Tasks 1/3/5/7/8; 8 → test cycles/final gates; 9–10 → runbook and launch boundaries. Each Review Focus has owning tests above.

Review this plan before implementation. Preserve native execution in this chat and one independent final reviewer. Settled scope stays unchanged; explain any newly discovered security or scope trade-off before deviating.

Primary API references checked while planning: [Google ID-token verification](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html) and [OAuthLib web application client](https://oauthlib.readthedocs.io/en/latest/oauth2/clients/webapplicationclient.html). Local pinned-library signatures were also inspected; online docs may reflect other versions. No provider calls, dependency installations or implementation tests ran while writing this plan.
