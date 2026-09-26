# Durable Gmail and Chat Work Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a locally verified PostgreSQL-backed Gmail/chat work engine with Service Bus dispatch, restart recovery and a synthetic executor, without switching the running application or enabling real external effects.

**Architecture:** PostgreSQL owns accepted inputs, conversation order, worker leases, effects and dispatch intent. Service Bus carries disposable wake-up notifications; an outbox and recovery sweep repair delivery gaps. A separate cloud service boundary exposes the engine while the current SQLite app remains the compatibility baseline.

**Tech Stack:** Python using the main checkout interpreter; Flask; psycopg 3.3.6; azure-servicebus 7.14.3; PostgreSQL 17.11 for isolated local tests; stdlib unittest and the existing standalone verification scripts. No ORM, Celery, Redis, new UI framework or shared production credentials.

**Spec:** `docs/superpowers/specs/2026-09-26-durable-work-design.md`, committed as `68a3457`, approved by the user on 2026-09-26. The user subsequently approved this implementation plan and native execution with independent review. Its completion will not constitute production deployment readiness or authorize provisioning.

## Global Constraints

- “Local data stays untouched; cloud starts with fresh accounts/data.”
- “No implementation, provisioning or spend is authorized by writing it.”
- “The application service owns the transaction. Repository helpers do not commit.”
- “No provider/model call runs while a database transaction or row lock is held.”
- “The local SQLite path remains separate; there is no dual-write or automatic import of a user's local database into cloud.”
- “Do not replay an entire agent turn after any recorded mutation intent.”
- “Agents receive neither database credentials nor long-lived Google tokens.”
- “Ordinary restarts: no loss of committed accepted jobs.”
- Approved disaster targets: RPO <=15 minutes; RTO <=4 hours. Actual Azure restore remains a separate release gate.
- Scope is Gmail plus existing in-app chat for 50 invited users; no new device, form, order, casting, Jev or WhatsApp features/source activation.
- Use `/Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai-hermes-cloud-image`; never edit the running checkout's Python or configuration.
- Use `/Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python`; no second worktree venv and no upgrades to the shared venv.
- Keep dotenv disabled for all verification. Install added cloud test dependencies into a test-owned target directory, not the running interpreter's site-packages.
- No Azure resource creation, paid model call, registry push, credential copying or real mailbox access. Broker/provider tests use injected fakes; PostgreSQL tests use a real disposable local server.

## Review Focus

1. A lost HTTP response followed by reset/retry must not reattach an old submission to a new conversation generation (Task 2).
2. A delayed worker must not send a tool request or commit after its lease, connection or recovery epoch changes (Tasks 3, 5, 6, 9).
3. An out-of-order notification must not strand a queued turn, and broker deduplication must not swallow an intentional recovery notification (Task 4).
4. Two interleaved mailbox pages or an invalid pagination token must not advance a checkpoint past unrecorded work (Task 7).
5. A stale browser/service-worker cache must not treat queued work as complete or generate a new submission key on retry (Task 8).

---

## Delivery boundary and execution order

Implement Tasks 1–9 sequentially because they share transaction and ownership
contracts. Each task has its own red/green cycle and commit. Independent review
must examine the finished branch before any merge or switch-over. Do not infer
permission to publish from a passing test.

The delivered cloud API is deny-by-default and testable with an injected synthetic
principal. Production Google identity/onboarding, protected credential storage,
real Hermes/tool isolation and Azure provisioning are separate launch work, not
silently replaced by a development authentication bypass. The final status must
say “local durable backend verified,” not “cloud deployed.”

## File map

| Files | Responsibility |
| --- | --- |
| `requirements-cloud.in`, `requirements-cloud.lock` | Only added cloud libraries and hashed transitive dependencies. |
| `cloud/__init__.py`, `cloud/types.py`, `cloud/config.py` | Public values, states, safe configuration; no import-time clients or dotenv. |
| `cloud/database.py`, `cloud/migrate.py`, `cloud/migrations/001_core.sql` through `007_recovery.sql` | Explicit transactions and versioned forward-only cloud schema. |
| `cloud/work.py` | Acceptance, ordered messages, owner-qualified reads; no execution threads. |
| `cloud/leases.py`, `cloud/recovery.py` | Claims, fencing, completion, retries, expiry and lost-wake-up repair. |
| `cloud/dispatch.py`, `cloud/service_bus.py` | Outbox claiming, publication and broker envelope/settlement adapter. |
| `cloud/effects.py`, `cloud/control.py` | Mutation intent/results, reconciliation, cancellation and reset. |
| `cloud/connections.py`, `cloud/authorization.py` | Connection generations and per-call capability validation; stores references, not raw secrets. |
| `cloud/gmail.py`, `cloud/todos.py` | Recoverable page ingestion, generation outcomes, todo/notice transaction. |
| `cloud/http.py`, `cloud/app.py` | Authenticated API boundary and deny-by-default factory. Does not import `app.py` or use its SQLite helpers. |
| `cloud/worker.py`, `cloud/scheduler.py`, `cloud/synthetic.py`, `cloud/cli.py`, `cloud/telemetry.py` | Process lifecycle, synthetic execution, recovery sweeps, safe diagnostics. |
| `static/js/durable-work.js`; modify `static/js/app.js`, `static/js/sw.js`, `templates/index.html` | Opt-in durable receipt/status behavior; local legacy path unchanged when disabled. |
| `scripts/verify/verify_cloud.py`, `tests/cloud/support.py`, `tests/cloud/test_*.py`, `scripts/verify/verify_durable_work_ui.cjs`, `scripts/verify/verify_durable_work_browser.py` | Test-owned PostgreSQL lifecycle, real transaction/concurrency tests, browser-client contract checks. |
| `docs/runbooks/durable-work-local.md` | Local verification, safe recovery rehearsal, remaining launch gates. |

Leave `db.py`, `main.py`, `auth.py`, `pollers/gmail/auth.py`, `agent/runs.py`,
`agent/hermes_runner.py`, `.env` and all user databases unchanged. Their current
behavior is reference material; new cloud modules must not import their live
entrypoints to reuse it. Reuse pure formatting/validation only after confirming
that import has no provider or local-file side effects.

## Common contracts

Define these values in `cloud/types.py`; use UUID objects internally and JSON
strings at the HTTP/broker boundary. Owner IDs remain text to match existing user
identifiers. Public errors are `Conflict`, `NotFound`, `Rejected`, `StaleLease`,
`Unavailable` subclasses of a new `WorkError`; each carries a safe `code` string.

```python
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

@dataclass(frozen=True)
class Actor:
    owner_id: str

@dataclass(frozen=True)
class Submission:
    conversation_id: UUID
    generation: int
    request_key: UUID
    text: str
    from_suggestion: bool = False

@dataclass(frozen=True)
class Receipt:
    job_id: UUID
    conversation_id: UUID
    generation: int
    state: str
    replayed: bool = False

@dataclass(frozen=True)
class Claim:
    job_id: UUID
    owner_id: str
    conversation_id: UUID | None
    generation: int | None
    attempt: int
    epoch: UUID
    lease_until: datetime

@dataclass(frozen=True)
class Envelope:
    version: int
    dispatch_id: UUID
    job_id: UUID
    epoch: UUID
```

States: `queued`, `running`, `retry_pending`, `succeeded`, `failed`, `cancelled`,
`expired`, `needs_reconciliation`. The last state blocks later conversation work.
No device-wait implementation. `succeeded` maps to legacy display status `done`
only at the compatibility UI boundary, not in database logic.

Planning defaults (must appear in configuration and tests): leases 60s, heartbeat
15s, recovery sweep 30s, missing-notification redispatch after 60s, maximum three
execution attempts, retry delays 5s/30s with injected bounded jitter. Chat expires
24h after acceptance, Gmail generation after 72h; a running synthetic attempt has
a 10-minute wall-time limit. Expiry creates a visible outcome; possible effects
override expiry to reconciliation. Limit 20 unfinished jobs per conversation and
100 per owner; check duplicate requests before capacity rejection. HTTP text limit
32 KiB UTF-8, request body 64 KiB. These are testable beta defaults, not load targets.

All connection/lease/deadline comparisons use PostgreSQL time. A unit-test clock
may control pure retry calculations; integration tests expire rows directly in
their isolated schema rather than changing the host clock or sleeping a minute.

### Verification commands used throughout

Run from the isolated worktree. The new runner in Task 1 owns its temporary
PostgreSQL container and refuses a supplied database URL. It forwards `--case`
only to the named unittest module under `tests.cloud`; `--all` runs those modules
plus existing app checks. These are planned commands, not tests already run:

```sh
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --case tests.cloud.test_database
PYTHON_DOTENV_DISABLED=1 /Users/Madhav/Documents/ChatGPT/inbox/action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --all
```

Pin the legacy script allowlist to `verify_actions.py`, `verify_chat.py`,
`verify_clarify.py`, `verify_connections.py`, `verify_cursors.py`, `verify_dedup.py`,
`verify_executor.py`, `verify_google_mcp.py`, `verify_google_scopes.py`,
`verify_hermes_activity.py`, `verify_links.py`, `verify_migration.py`,
`verify_noise.py`, `verify_pocket.py`, `verify_push.py`, `verify_todo_tools.py`,
`verify_web.py`, `verify_whatsapp.py`. Do not glob every verifier: that would
recurse into the new runner or silently execute image/browser checks with different
prerequisites. Browser/image checks are invoked explicitly in their owning steps.

## Task 1: Transaction foundation and disposable verification environment

**Files:** Create `requirements-cloud.in`, `requirements-cloud.lock`,
`cloud/{__init__,types,config,database,migrate}.py`, `cloud/migrations/001_core.sql`,
`scripts/verify/verify_cloud.py`, `tests/__init__.py`, `tests/cloud/{__init__,support,test_database}.py`.

**Interfaces:** `Database(dsn: str, schema: str)` exposes
`transaction() -> ContextManager[psycopg.Connection]` and
`read(sql: str, params: tuple = ()) -> list[dict]`;
`migrate(db: Database) -> None` applies checksum-tracked numbered SQL files.
`tests.cloud.support.sandbox() -> ContextManager[Database]` creates a unique schema,
applies migrations and seeds owners `alice`/`bob`; no real identity records.

- [ ] Write the following test, plus migration repeat/checksum mismatch and
  unsafe-target rejection cases. These tests execute real SQL:

```python
import unittest
from tests.cloud.support import sandbox

class DatabaseTests(unittest.TestCase):
    def test_transaction_rollback(self):
        with sandbox() as db:
            with self.assertRaisesRegex(RuntimeError, 'fixture crash'):
                with db.transaction() as tx:
                    tx.execute("INSERT INTO owners(owner_id) VALUES (%s)", ('rolled-back',))
                    raise RuntimeError('fixture crash')
            self.assertEqual(db.read("SELECT owner_id FROM owners WHERE owner_id=%s",
                                     ('rolled-back',)), [])
```

- [ ] Run `verify_cloud.py --case tests.cloud.test_database`; capture the missing
  runner/module failure before implementing. Build the harness next and rerun to
  expose the real transaction/schema failures; a missing dependency alone is not
  sufficient red evidence for rollback behavior.
- [ ] Create the isolated harness and dependency lock. Input file contents:

```text
psycopg[binary]==3.3.6
azure-servicebus==7.14.3
```

  Resolve with the available `uv pip compile --generate-hashes requirements-cloud.in
  -o requirements-cloud.lock`, recording resolver/Python versions. Install the lock
  with the main interpreter's pip using `--require-hashes --target` into a
  `tempfile.mkdtemp(prefix='athena-cloud-deps-')` directory. Pass that directory in
  `PYTHONPATH` only to test subprocesses. Do not upgrade shared site-packages.
  Reuse Flask from the main interpreter. Resolve/review dependency conflicts before
  marking this step complete; the latest version lookup is not compatibility proof.

  PostgreSQL image, verified from official registry metadata on 2026-09-26:
  `postgres:17-bookworm@sha256:639ab7ceb90e13123085b741fb31ef493fba25463002f6da665352e7b534b652`.
  Pull once, then run with `--pull=never`, unique `athena-cloud-test-<uuid>` name,
  `--label ai.athena.verify=cloud`, no host mounts, tmpfs data, 512 MiB memory,
  2 CPU, 128 PIDs, and an ephemeral port bound only to `127.0.0.1`. Use generated
  synthetic credentials, not trust authentication. Determine the mapped port via
  Docker inspect; bound readiness to 30s. Forward only the generated test DSN to
  children. In `finally`, inspect the exact container's name/label before removing
  that test-owned container. Never remove volumes or accept a user database URL.

  `sandbox()` validates loopback DSN/database `athena_verify` plus a runner-generated
  marker, creates `verify_<uuidhex>` schema, and drops only that schema afterward.
  Use `psycopg.sql.Identifier` for schema identifiers, never interpolated user SQL.
  Deny non-loopback socket connections in verification processes; broker/provider
  clients remain injected fakes. Explicitly remove inherited provider keys in child
  environments without printing them.

  Transaction kernel (`Database.transaction`):

```python
from contextlib import contextmanager
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

@contextmanager
def transaction(self):
    with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
            conn.execute("SET LOCAL lock_timeout = '2s'")
            conn.execute("SET LOCAL statement_timeout = '10s'")
            yield conn
```

  `read` uses this same boundary and returns fetched dictionaries. Migration runner
  takes a database advisory lock, creates `schema_migrations(version, sha256,
  applied_at)`, verifies existing checksums, and executes each new SQL file plus its
  version insert in one transaction. No migration runs on normal requests.
  Initial schema includes `owners(owner_id text primary key, enabled boolean)` and
  `runtime(singleton boolean primary key check(singleton), epoch uuid, enabled boolean)`;
  initialize the runtime disabled. Test helpers enable only their synthetic runtime.
  Initialize its epoch with a fresh UUID. Each test schema gets independent runtime
  state, owners and migration versions; tests do not share committed fixture jobs.
- [ ] Rerun the module and verify rollback, migration idempotence, changed-migration
  rejection, cleanup on test failure, refusal of non-loopback/custom DSNs and no
  provider egress. Record image ID/Python/dependency versions without secrets.
- [ ] Commit only this task's named files: `test: add isolated PostgreSQL transaction harness`.

## Task 2: Atomic acceptance and append-only conversations

**Files:** Create `cloud/work.py`, `cloud/migrations/002_work.sql`,
`tests/cloud/test_acceptance.py`; extend `cloud/types.py` with the contracts above.

**Interfaces:** `Work(db)` exposes `create_conversation(actor: Actor, kind: str,
todo_id: UUID | None = None) -> UUID`, `accept(actor: Actor, request: Submission)
-> Receipt`, `snapshot(actor: Actor, conversation_id: UUID) -> dict`,
`notice(actor: Actor, conversation_id: UUID, origin: str, text: str) -> None`.
Snapshot keys: `generation`, `messages` (id, sequence, role, content, job_id),
`jobs` (job_id, state, reason, order), `reconciliation_hold`.

- [ ] Write actual acceptance tests, including this duplicate/conflict case:

```python
import unittest
from uuid import uuid4
from cloud.types import Actor, Submission, Conflict
from cloud.work import Work
from tests.cloud.support import sandbox

class AcceptanceTests(unittest.TestCase):
    def test_same_request_has_one_receipt(self):
        with sandbox() as db:
            work, actor = Work(db), Actor('alice')
            cid = work.create_conversation(actor, 'chat')
            key = uuid4()
            request = Submission(cid, 1, key, 'fixture request')
            one, two = work.accept(actor, request), work.accept(actor, request)
            self.assertEqual(one.job_id, two.job_id)
            self.assertEqual(len(work.snapshot(actor, cid)['messages']), 1)
            with self.assertRaises(Conflict):
                work.accept(actor, Submission(cid, 1, key, 'changed input'))
```

  Add two independent connections submitting the same key behind a
  `threading.Barrier(2)`; assert one message/job/outbox record. Add Bob's access to
  Alice's conversation, over-capacity duplicate versus new request, oversized UTF-8,
  empty text, and transaction abort after job insert. The latter uses a test-only
  PostgreSQL trigger raising on outbox insert; assert no message/job remains.
- [ ] Run `verify_cloud.py --case tests.cloud.test_acceptance`; expect missing API,
  then failing atomicity/uniqueness assertions until implementation is complete.
- [ ] Implement schema and acceptance in one short transaction. Columns:
  `conversations(id, owner_id, kind, todo_id, generation, next_message_seq,
  next_job_order, reconciliation_hold)`,
  `messages(id, owner_id, conversation_id, generation, sequence, role, content,
  origin, job_id, created_at)`,
  `jobs(id, owner_id, conversation_id, generation, job_order, kind, request_key,
  input_hash, input jsonb, state, reason, attempts, fence, epoch, due_at, expires_at,
  lease_until, worker_id, cancel_requested, created_at, finished_at)`,
  `outbox(id, job_id, epoch, version, due_at, published_at, lease_until, fence,
  quarantined, created_at)`.
  Also create the minimal owner-scoped `todos(id, owner_id, title, source, status,
  dedup_key, created_at, updated_at)` relation for todo conversations; Task 7 adds
  generation metadata. Give it unique `(owner_id,id)` and scoped dedup constraints.
  Todo conversations require an existing same-owner todo; chat has no todo ID.
  Include owner-qualified conversation/message/job foreign keys, unique
  `(owner_id, conversation_id, generation, request_key)`, unique message origin and
  sequence per conversation/generation, and unique job order. Constrain states/kinds,
  nonnegative counters and allowed roles in SQL. Request hash covers normalized
  text, generation, kind and suggestion provenance; persist immutable input.
  Add composite unique `(owner_id,id)` keys to referenced entities so composite
  foreign keys are enforceable. Verify direct SQL cross-owner relations fail,
  not merely HTTP authorization. Job kinds are `chat`, `todo`, `gmail_generation`;
  a null conversation is allowed only for generation jobs.

```sql
SELECT * FROM conversations WHERE id=%s AND owner_id=%s FOR UPDATE;
-- Check active owner/runtime, generation and duplicate key before queue limits.
-- Insert user message, job and outbox, then increment both sequence counters.
-- All statements execute on the same connection, without helper commits.
```

  For user-wide capacity serialize on the owner row before the conversation row.
  Adopt lock order `runtime -> owner -> connection (if any) -> conversation -> job
  -> outbox/effect`, and use it in subsequent tasks to prevent lock inversion.
  Return a `Receipt` only after leaving the transaction context successfully;
  set `replayed=True` only in the transactional existing-key branch, never through
  a racy preflight lookup, so HTTP can distinguish first acceptance from retry.
  `notice` appends an origin-deduplicated message and never rewrites a thread.
  `snapshot` checks owner and current generation; no in-memory status registry.
- [ ] Rerun all acceptance cases. Simulate response loss by ignoring a committed
  receipt and resubmitting the same key. Stale generation must return Conflict,
  including after reset once Task 5 is present, not create work in a new chat.
- [ ] Commit: `feat: persist accepted conversation work atomically`.

## Task 3: Fenced leases, completion and bounded recovery

**Files:** Create `cloud/leases.py`, `cloud/recovery.py`,
`cloud/migrations/003_leases.sql`, `tests/cloud/test_leases.py`. Never edit an
applied migration checksum.

**Interfaces:** `Leases(db).claim(job_id: UUID, worker_id: str) -> Claim | None`;
`heartbeat(claim: Claim) -> bool`; `finish(claim: Claim, reply: str) -> bool`;
`fail(claim: Claim, retryable: bool, reason: str) -> None`;
`context(claim: Claim) -> list[dict]`; `payload(claim: Claim) -> dict` returns
`kind` and immutable `input`. These reads reject stale claims with `StaleLease`
and close the transaction before invoking any executor.
`Recovery(db).sweep() -> dict[str, int]` returns counts by safe outcome.

- [ ] Write the lease race using two processes/connections. The basic test:

```python
import unittest
from uuid import uuid4
from cloud.types import Actor, Submission
from cloud.work import Work
from cloud.leases import Leases
from cloud.recovery import Recovery
from tests.cloud.support import sandbox

class LeaseTests(unittest.TestCase):
    def test_late_attempt_cannot_commit(self):
        with sandbox() as db:
            actor, work, leases = Actor('alice'), Work(db), Leases(db)
            cid = work.create_conversation(actor, 'chat')
            job = work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            old = leases.claim(job.job_id, 'worker-a')
            self.assertIsNotNone(old)
            with db.transaction() as tx:
                tx.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1s' WHERE id=%s", (job.job_id,))
            Recovery(db).sweep()
            with db.transaction() as tx:
                tx.execute("UPDATE jobs SET due_at=clock_timestamp() WHERE id=%s", (job.job_id,))
            new = leases.claim(job.job_id, 'worker-b')
            self.assertIsNotNone(new)
            self.assertFalse(leases.finish(old, 'stale result'))
            self.assertTrue(leases.finish(new, 'current result'))
            self.assertFalse(leases.finish(new, 'duplicate result'))
```

  Add: second queued turn cannot claim early; notice appended during execution
  survives completion; context excludes later user turns; exhausted attempts become
  failed; expired queued work is visible; disabled owner/runtime cannot claim.
- [ ] Run `verify_cloud.py --case tests.cloud.test_leases`; expect missing lease
  methods, then observe the stale-write/order checks fail before fencing is added.
- [ ] Claim under ordered locks. Verify due time, expiry, owner, runtime epoch,
  conversation generation/hold, no earlier nonterminal turn, connection generation
  where bound, and no unexpired authorized attempt. Increment fence/attempt counters
  and set lease using database time. Heartbeats and completions compare all claim
  identity fields and unexpired lease, not only worker name.

```sql
UPDATE jobs SET state='running', fence=fence+1, attempts=attempts+1,
 worker_id=%s, lease_until=clock_timestamp()+interval '60 seconds'
WHERE id=%s AND state IN ('queued','retry_pending')
RETURNING id, owner_id, conversation_id, generation, fence, epoch, lease_until;
```

  `Claim.attempt` is the returned fence token; `jobs.attempts` is the bounded count.
  Add `attempts(job_id, fence, owner_id, epoch, worker_id, started_at, lease_until,
  finished_at, outcome, safe_reason)` keyed by `(job_id,fence)`. Insert on claim,
  update heartbeat/outcome alongside the current job and retain previous attempts;
  overwriting only the current job fields loses required diagnostic evidence.
  `finish` appends a unique `completion:<job_id>` message, marks succeeded, releases
  ownership and inserts next-turn wake-up in the same transaction. Context reads
  include prior completed turns/notices but omit later user submissions. `fail`
  only retries no-effect work; Task 5 installs the conservative effect-intent guard
  before any mutation-capable runner exists. Sweep locks candidates in small batches,
  rechecks under the common lock order, marks expired/exhausted work visibly, and
  schedules retries without resetting counters. Never use broker delivery count as
  the execution budget. Deadlines and cancellation are checked before all claims.
- [ ] Rerun lease tests and acceptance tests. Add a real subprocess exit immediately
  after claim and verify another process recovers it. Assert duplicate computation
  is not interpreted as permission for duplicate commits/effects.
- [ ] Commit: `feat: recover fenced work after worker loss`.

## Task 4: Transactional outbox and Service Bus adapter

**Files:** Create `cloud/dispatch.py`, `cloud/service_bus.py`,
`tests/cloud/test_dispatch.py`; extend `cloud/recovery.py` for lost notifications.

**Interfaces:** `Dispatcher(db, send: Callable[[Envelope], None]).once() -> bool`;
`Receiver(db).handle(envelope: Envelope, worker_id: str) -> Claim | None`;
`ServiceBusTransport(client, queue: str).send(envelope: Envelope) -> None` and
`receive_once(handle: Callable[[Envelope], None]) -> None`.
The Azure client is injected; these modules never read connection strings or
start clients at import. Envelope JSON contains only the four defined fields.

- [ ] Write a send-accepted/ack-lost regression:

```python
import unittest
from uuid import uuid4
from cloud.types import Actor, Submission
from cloud.work import Work
from cloud.dispatch import Dispatcher
from tests.cloud.support import sandbox

class DispatchTests(unittest.TestCase):
    def test_send_retry_reuses_dispatch_identity(self):
        with sandbox() as db:
            actor, work, sent = Actor('alice'), Work(db), []
            cid = work.create_conversation(actor, 'chat')
            work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            def uncertain_send(envelope):
                sent.append(envelope)
                if len(sent) == 1:
                    raise TimeoutError('synthetic lost acknowledgement')
            dispatcher = Dispatcher(db, uncertain_send)
            dispatcher.once()
            with db.transaction() as tx:
                tx.execute("UPDATE outbox SET due_at=clock_timestamp(), lease_until=NULL")
            dispatcher.once()
            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0].dispatch_id, sent[1].dispatch_id)
```

  Add new-dispatch-ID recovery after 60s, notification for turn 2 before turn 1,
  duplicate completed job, stale epoch, invalid envelope, database lookup failure,
  and publish fence expiry while a sender is blocked. Assert the sender runs with
  no database transaction open using a second connection's lock probe.
- [ ] Run `verify_cloud.py --case tests.cloud.test_dispatch`; expect absent dispatch
  implementation then the identity/settlement failures.
- [ ] Claim an outbox row briefly, commit its publish lease, send outside the
  transaction, then mark publication only if its fence still matches. On transport
  exception retain the same row/ID with bounded backoff. Recovery creates a new row
  only for eligible jobs with neither valid execution/publish lease nor notification
  in the last 60s; published rows do not suppress recovery forever.

```python
import json
from azure.servicebus import ServiceBusMessage

def send(self, envelope):
    body = json.dumps({'version': envelope.version,
                      'dispatch_id': str(envelope.dispatch_id),
                      'job_id': str(envelope.job_id),
                      'epoch': str(envelope.epoch)})
    with self.client.get_queue_sender(queue_name=self.queue) as sender:
        sender.send_messages(ServiceBusMessage(body, message_id=str(envelope.dispatch_id)))
```

  Receiver uses PeekLock. Complete only after durable claim or confirmed redundancy/
  ineligibility. Lookup errors do not complete. Validate envelope keys/types/version
  and quarantine poison messages with safe reason codes. A completed wake-up can
  be lost safely because the DB lease/recovery sweep owns execution. Configure
  schema-poison dispatches as quarantined in the outbox; do not regenerate them
  forever. No Service Bus sessions/FIFO dependency; order is enforced in PostgreSQL.
  Test injected sender/receiver context managers and settlement calls without Azure.
- [ ] Run dispatch plus lease tests; verify every settled early notification has
  a tested sweep path. Actual Azure delivery remains a staging gate, not claimed
  by this fake-adapter test.
- [ ] Commit: `feat: dispatch durable jobs through a replay-safe outbox`.

## Task 5: Effects, cancellation, reset and reconciliation

**Files:** Create `cloud/effects.py`, `cloud/control.py`,
`cloud/migrations/004_effects.sql`, `tests/cloud/test_effects.py`;
update `cloud/leases.py`, `cloud/recovery.py`.

**Interfaces:** `Effects(db).prepare(claim: Claim, operation_id: UUID,
kind: str, fingerprint: str) -> bool` returns true exactly once for dispatch intent;
`record(claim: Claim, operation_id: UUID, outcome: str, receipt: dict) -> None`;
`Control(db).cancel(actor: Actor, job_id: UUID) -> str`;
`reset(actor: Actor, conversation_id: UUID, generation: int) -> int`;
`reconcile(actor: Actor, job_id: UUID, decision: str, reason: str) -> None`.
Allowed decisions: `confirmed_succeeded` or `closed_without_retry`. Neither replays
an old turn. A later user request has a new idempotency key and is separately authorized.

- [ ] Write this uncertainty test plus concurrent duplicate prepare and reset tests:

```python
import unittest
from uuid import uuid4
from cloud.types import Actor, Submission
from cloud.work import Work
from cloud.leases import Leases
from cloud.effects import Effects
from cloud.control import Control
from cloud.recovery import Recovery
from tests.cloud.support import sandbox

class EffectTests(unittest.TestCase):
    def test_unknown_effect_is_not_retried(self):
        with sandbox() as db:
            actor, work, leases = Actor('alice'), Work(db), Leases(db)
            cid = work.create_conversation(actor, 'chat')
            job = work.accept(actor, Submission(cid, 1, uuid4(), 'synthetic effect'))
            claim = leases.claim(job.job_id, 'worker')
            self.assertTrue(Effects(db).prepare(claim, uuid4(), 'synthetic_write', 'fixture-hash'))
            with db.transaction() as tx:
                tx.execute("UPDATE jobs SET lease_until=clock_timestamp()-interval '1s' WHERE id=%s", (job.job_id,))
            Recovery(db).sweep()
            self.assertIsNone(leases.claim(job.job_id, 'replacement'))
            self.assertTrue(work.snapshot(actor, cid)['reconciliation_hold'])
            self.assertEqual(Control(db).cancel(actor, job.job_id), 'needs_reconciliation')
```

  Add confirmation stored but reply lost; cancellation before/after prepare; old
  completion after reset; stale request generation after reset; Bob attempting to
  reconcile Alice's job; duplicate operation ID with changed fingerprint; expiry
  after intent; and denied new claims behind a hold.
- [ ] Run `verify_cloud.py --case tests.cloud.test_effects`; show unsafe requeue
  failure before adding the effect guard.
- [ ] Create `effects(operation_id, job_id, owner_id, attempt_fence, epoch, kind,
  fingerprint, state, receipt jsonb, created_at, recorded_at)` with unique operation
  ID and owner-qualified job relation. `prepare` locks runtime/owner/conversation/job,
  checks valid attempt/deadline/cancel, then inserts intent. The provider call is
  outside this module/transaction. Existing same intent returns false; changed
  arguments conflict. Record receipts even for a now-stale attempt as evidence,
  but never let late receipt recording authorize more work or clear a hold by itself.
  Recorded outcomes are `confirmed_succeeded`, `confirmed_no_effect`, `uncertain`;
  initial intent is `prepared`. Whitelist receipt keys `provider_id` and `status`
  with bounded scalar values; reject arbitrary provider bodies/token fields.
  Even confirmed-no-effect intents require explicit review in this conservative
  first implementation rather than automatically replaying the whole agent turn.

```sql
SELECT EXISTS(SELECT 1 FROM effects WHERE job_id=%s) AS has_intent;
-- Under the job lock, any intent prevents automatic whole-turn retry.
UPDATE jobs SET state='needs_reconciliation', reason='effect_outcome_requires_review',
 lease_until=NULL WHERE id=%s;
UPDATE conversations SET reconciliation_hold=true WHERE id=%s AND owner_id=%s;
```

  Cancel queued work durably; cancel running work revokes authority immediately,
  records a stop request and yields reconciliation if any mutation intent exists.
  Reset increments generation, cancels old pending jobs, preserves audit/effect hold,
  and keeps late results out of the new visible thread. Reconciliation appends an
  audit record with actor/decision/reason/time, clears a hold only when no unresolved
  job remains, and dispatches eligible next work. Never equate cancellation with undo.
- [ ] Run effects, acceptance and lease tests; a confirmed effect with lost reply
  must stay out of automatic retry, not only effects with unknown receipts.
- [ ] Commit: `feat: stop uncertain effects from being replayed`.

## Task 6: Connection generations and fail-closed tool authorization

**Files:** Create `cloud/connections.py`, `cloud/authorization.py`,
`cloud/migrations/005_connections.sql`, `tests/cloud/test_connections.py`;
extend `cloud/types.py` with `Capability` below.

**Interfaces:** `Connections(db).connect(actor: Actor, account: str,
credential_ref: str) -> UUID`; `generation(actor: Actor, connection_id: UUID) -> int`;
`disconnect(actor: Actor, connection_id: UUID) -> None`;
`refresh_reference(actor: Actor, connection_id: UUID, generation: int,
credential_ref: str) -> bool`.
`Authorization(db).issue(claim: Claim, connection_id: UUID, scopes: frozenset[str])
-> Capability`; `validate(capability: Capability, scope: str) -> bool`.

```python
@dataclass(frozen=True)
class Capability:
    token: str
```

- [ ] Write this stale-refresh check and capability fencing cases:

```python
import unittest
from cloud.types import Actor
from cloud.connections import Connections
from tests.cloud.support import sandbox

class ConnectionTests(unittest.TestCase):
    def test_refresh_cannot_recreate_revoked_connection(self):
        with sandbox() as db:
            connections, actor = Connections(db), Actor('alice')
            cid = connections.connect(actor, 'fixture@example.invalid', 'fixture:key-one')
            generation = connections.generation(actor, cid)
            connections.disconnect(actor, cid)
            self.assertFalse(connections.refresh_reference(actor, cid, generation, 'fixture:key-two'))
            self.assertEqual(db.read('SELECT active FROM connections WHERE id=%s', (cid,))[0]['active'], False)
```

  Add validate after lease expiry/reset/epoch rotation, wrong owner/account/scope,
  revoked connection, deadline, cancelled job, disabled owner and database outage.
  Store/log inspection must not contain the issued plaintext token.
- [ ] Run `verify_cloud.py --case tests.cloud.test_connections`; expect missing
  connection/capability behavior then failure of stale refresh without generations.
- [ ] Create `connections(id, owner_id, account, generation, active, credential_ref,
  granted_scopes, updated_at)` with unique owner/account and protected references
  only; no OAuth token JSON in this subproject. Reconnect preserves stable ID and
  increments generation. Conditional refresh must update, never insert:

```sql
UPDATE connections SET credential_ref=%s, updated_at=clock_timestamp()
WHERE id=%s AND owner_id=%s AND active AND generation=%s
RETURNING id;
```

  Store capabilities as a SHA-256 hash of a 32-byte cryptographically random token,
  bound to job/fence/epoch/connection generation/scopes and a 60s maximum expiry.
  Every validation reads current database ownership and fails closed on errors.
  Disconnect revokes capabilities and queued bound work in the same transaction;
  running work without intents cancels, with intents holds for reconciliation.
  Issuance checks scopes are a subset of the stored grant. Tests set synthetic
  grants directly in their schema; no HTTP endpoint can assert grants or owner ID.
  This is the authorization primitive, not a deployed remote MCP/tool server.
  Add nullable `jobs.connection_id` and `connection_generation` together, with
  owner-qualified connection foreign key and paired-null constraint. Create
  `capabilities(token_hash, owner_id, job_id, fence, epoch, connection_id,
  connection_generation, scopes, expires_at, revoked_at)`. Bound jobs can issue
  capabilities only for their bound connection. General chat may select any of
  its owner's active authorized connections, never another owner's account.
- [ ] Rerun connection/effect tests. Confirm secrets are absent from broker envelopes,
  safe error strings and captured logs. A provider call already in flight remains
  explicitly outside any promise of revocation undoing it.
- [ ] Commit: `feat: fence connections and per-attempt tool capabilities`.

## Task 7: Recoverable Gmail ingestion and atomic todo outcomes

**Files:** Create `cloud/gmail.py`, `cloud/todos.py`,
`cloud/migrations/006_gmail.sql`, `tests/cloud/test_gmail.py`.

**Interfaces:** `Gmail(db).claim_poll(actor: Actor, connection_id: UUID) -> PollClaim | None`;
`ingest_page(claim: PollClaim, page: MailPage) -> int` returns number of new jobs;
`resync(claim: PollClaim) -> None` records history invalidation without claiming a
complete historical catch-up. `Todos(db).record_generation(claim: Claim,
decision: str, todo: dict | None) -> UUID | None`; `list(actor: Actor) -> list[dict]`.
Define `PollClaim(connection_id: UUID, owner_id: str, generation: int, fence: int,
epoch: UUID)` and `MailPage(page_key: str, expected_page_key: str, next_page_key:
str | None, final_cursor: str | None, events: tuple[dict, ...])` frozen dataclasses
in `cloud/types.py`. Events carry `message_id`, `event_type`, `occurrence_id`,
`content` and `thread_id`; pages are normalized by an injected provider, not HTTP
request payloads. Initial expected page key is `start`.

- [ ] Write the duplicate-page case, then forced transaction failure before cursor
  advancement, two schedulers and stale pagination/connection-generation tests:

```python
import unittest
from cloud.types import Actor, MailPage
from cloud.connections import Connections
from cloud.gmail import Gmail
from tests.cloud.support import sandbox

class GmailTests(unittest.TestCase):
    def test_duplicate_page_does_not_duplicate_generation(self):
        with sandbox() as db:
            actor, gmail = Actor('alice'), Gmail(db)
            cid = Connections(db).connect(actor, 'fixture@example.invalid', 'fixture:key')
            claim = gmail.claim_poll(actor, cid)
            page = MailPage('page-1', 'start', None, '100', ({
                'message_id': 'm1', 'event_type': 'received',
                'occurrence_id': 'h99', 'thread_id': 't1', 'content': 'fixture'},))
            self.assertEqual(gmail.ingest_page(claim, page), 1)
            self.assertEqual(gmail.ingest_page(claim, page), 0)
            self.assertEqual(db.read('SELECT count(*) AS n FROM jobs')[0]['n'], 1)
```

  Add same provider IDs under different owners/connections; label changes with
  different history occurrence IDs; overlapping backfill/history; exception during
  generation after ingestion; retry after todo committed; and in-app notice
  preserved while a chat reply completes. Invalid page token must request bounded
  resync, never silently advance final history cursor.
- [ ] Run `verify_cloud.py --case tests.cloud.test_gmail`; ensure the page rollback
  assertion fails without atomic event/job/checkpoint processing.
- [ ] Create mailbox checkpoint/lease records, `ingested_events` and page receipts.
  Separate event occurrence identity from received-message generation identity:
  received work deduplicates on `(owner, connection, message_id, 'received')`,
  while label events include occurrence ID. Check page receipt before expected-page
  validation so acknowledged page retry is harmless. On a new page, validate
  expected page key and poll fence, insert normalized events/jobs/outbox, persist
  next-page state and only then a final cursor, all atomically. Durable payloads
  include sufficient generation input; later generation does not depend on a
  vanished in-memory list. Cap provider page size/normalized payload; oversize input
  creates an explicit ingestion error and holds the cursor rather than skipping it.
  Store a canonical hash in each page receipt: same page key with changed content
  is a Conflict, not silent success. Scope receipts to a page-chain UUID and add
  `chain_id: UUID` to `PollClaim`/mailbox checkpoints, so a restarted backfill's
  page names cannot collide with an older chain. Exact completed-page retries may
  return zero without reviving old ownership; all new changes require a valid fence.
  For received-mail jobs use `request_key=uuid5(connection_id, 'received:' +
  message_id)` and a unique owner/connection/kind/request-key index for generation
  jobs. Their conversation ID is null, so the conversation acceptance uniqueness
  constraint alone cannot deduplicate them. Test both changed-page and NULL cases.

```sql
INSERT INTO ingested_events(id, owner_id, connection_id, identity, payload)
VALUES (%s,%s,%s,%s,%s) ON CONFLICT(owner_id,connection_id,identity) DO NOTHING;
-- New received identity: insert generation job and outbox in this transaction.
-- Persist page receipt + next-page state; final cursor only for a complete page chain.
```

  `Todos.record_generation` accepts only `created`, `duplicate`, `skipped` and
  current generation-job claims. Validate source/importance/status fields with SQL
  constraints and preserve existing deterministic noise/near-duplicate behavior.
  Extend `todos` with `connection_id`, `message_id`, `thread_id`, `importance`,
  `suggested_action`, `reasoning`, `due_date`, `source_meta jsonb` and add
  `generation_decisions(job_id primary key, owner_id, decision, todo_id,
  safe_reason, created_at)`. All references use owner-qualified foreign keys.
  Created results require a nonblank title and valid enums; duplicate/skipped
  outcomes create no new todo. Store skipped reasoning as safe codes, not prompts.
  Serialize generated todo decisions per owner to avoid concurrent near-duplicate
  races. Store decision, optional todo, one origin-deduplicated chat notice and job
  terminal state in one transaction. No inline push/WhatsApp/provider send.
  Enforce connection generation before recording results. Resync restarts a bounded
  three-day backfill with a new page chain and visible recovery limitation.
- [ ] Rerun Gmail/lease/acceptance tests. Prove a saved event with failed generation
  stays recoverable even though its source cursor advanced after durable enqueue.
- [ ] Commit: `feat: make Gmail cursor progress recoverable`.

## Task 8: HTTP receipts and opt-in browser status behavior

**Files:** Create `cloud/http.py`, `cloud/app.py`, `tests/cloud/test_http.py`,
`static/js/durable-work.js`, `scripts/verify/verify_durable_work_ui.cjs`,
`scripts/verify/verify_durable_work_browser.py`;
modify `static/js/app.js`, `static/js/sw.js`, `templates/index.html` only behind an
explicit durable-mode flag, absent/false for the existing local app.

**Interfaces:** `create_app(db: Database, principal: Callable[[], Actor | None]
| None = None, *, testing: bool = False) -> Flask`; passing a synthetic principal
is allowed only when testing is true. Without a production principal integration,
all business routes deny access; readiness reports `authentication_not_integrated`.
No owner-ID header, query parameter or body value can become a principal.

Routes: `POST /api/work/conversations`, `GET /api/work/conversations/<id>`,
`POST /api/work/conversations/<id>/messages`, `POST /api/work/jobs/<id>/cancel`,
`POST /api/work/conversations/<id>/reset`. Message body is text, generation,
request_key, from_suggestion; authenticated ownership is derived server-side.
New acceptance is HTTP 202; duplicate receipt 200; bad body 400; unauthorized 401;
other owner's identifier 404; stale/mismatched key 409; quota 429; DB failure 503.

- [ ] Write the HTTP test with a fixture principal:

```python
import unittest
from uuid import uuid4
from cloud.app import create_app
from cloud.types import Actor
from tests.cloud.support import sandbox

class HttpTests(unittest.TestCase):
    def test_receipt_survives_application_recreation(self):
        with sandbox() as db:
            def principal(): return Actor('alice')
            client = create_app(db, principal, testing=True).test_client()
            cid = client.post('/api/work/conversations', json={'kind':'chat'}).json['id']
            body = {'text':'fixture', 'generation':1, 'request_key':str(uuid4())}
            response = client.post(f'/api/work/conversations/{cid}/messages', json=body)
            self.assertEqual(response.status_code, 202)
            second = create_app(db, principal, testing=True).test_client()
            duplicate = second.post(f'/api/work/conversations/{cid}/messages', json=body)
            self.assertEqual(duplicate.json['job_id'], response.json['job_id'])
            self.assertEqual(duplicate.status_code, 200)
```

  Add malformed JSON/type, huge multibyte text, stale generation, cross-user IDs,
  no principal, database outage and disabled runtime. Assert accepted response
  cannot be produced before commit. Factory must reject synthetic principal with
  testing false. No route may call `agent.runs.start` or model code.
- [ ] Run `verify_cloud.py --case tests.cloud.test_http`; then write/run this Node
  client-contract test before implementing `durable-work.js`:

```javascript
const assert = require('node:assert/strict');
const work = require('../../static/js/durable-work.js');
assert.equal(work.shouldPoll('queued'), true);
assert.equal(work.shouldPoll('retry_pending'), true);
assert.equal(work.shouldPoll('needs_reconciliation'), false);
assert.equal(work.shouldPoll('succeeded'), false);
const pending = work.newSubmission('conversation', 1, 'fixture', 'fixed-key');
assert.equal(work.retrySubmission(pending).request_key, 'fixed-key');
assert.equal(work.retrySubmission(pending).generation, 1);
```

- [ ] Implement thin HTTP handlers calling transactional services, no SQL/threads
  inside route bodies. Status payload includes current generation, ordered messages,
  individual queued/running jobs and hold reason. Return sanitized errors; never
  exceptions containing DSNs/provider payloads. Before production auth integration,
  the factory is deliberately not a deployable authenticated service.

  Implement `durable-work.js` as a dependency-free browser/CommonJS module exporting
  `shouldPoll(state)`, `newSubmission(conversationId, generation, text, requestKey)`
  and `retrySubmission(pending)`. Pending submissions keep the same immutable key
  and generation through uncertain HTTP failures; explicit new send gets a new key.
  Browser generation changes invalidate the old composer state without resubmitting
  old text. Memory-only pending content is acceptable for this tranche; persist the
  receipt/key without plaintext prompt if adding session storage. Do not silently
  retry a lost message with a freshly generated key after page reload.

```javascript
const ACTIVE = new Set(['queued', 'running', 'retry_pending']);
function shouldPoll(state) { return ACTIVE.has(state); }
function newSubmission(conversationId, generation, text, requestKey) {
  return Object.freeze({ conversation_id: conversationId, generation,
                         text, request_key: requestKey });
}
function retrySubmission(pending) { return { ...pending }; }
```

  Under `window.__DURABLE_WORK === true`, keep Send available while other turns run,
  show queued/retry/hold statuses, target Stop to a specific job, and poll conversation
  status while any active job remains. Rendering must preserve notices and associate
  replies with their originating jobs. Legacy mode follows its existing code path.
  Bump `VERSION` in `static/js/sw.js` for changed cached assets, include the new script
  in `templates/index.html`, and update every durable-mode action/clarification
  submission to use the same idempotency contract. Existing service-worker actions
  remain disabled in the cloud test surface rather than bypassing acceptance.
  Add template defaults `durable_work=false` and `durable_conversations={}`;
  export them as `window.__DURABLE_WORK` and `window.__DURABLE_CONVERSATIONS`.
  The latter maps the UI's `chat`/todo identifiers to owned conversation UUIDs.
  The existing app does not set them. A test-only fixture route renders `index.html`
  with empty/synthetic todos, a synthetic user, `initial_view='chat'`, and the
  durable mapping. Register fixture-only `index`, `chat_page`, `settings_page`,
  `logout`, `manifest` endpoints required by that template, never production OAuth.
  No fixture route is registered when `testing=False`.
- [ ] Run HTTP tests, Node contract tests, and existing `verify_chat.py`,
  `verify_web.py`, `verify_clarify.py`, `verify_actions.py`, `verify_push.py` with
  dotenv disabled. `verify_durable_work_browser.py` starts only that fixture on an
  ephemeral loopback port, uses a fresh Playwright Chrome context without a user
  profile, and blocks non-loopback requests (serve a test-only escaped-text marked
  stub for the template's CDN dependency). Exercise two queued sends, reload/retry,
  notice arrival, cancellation and reset against the real test database. Its
  assertions include two distinct durable receipts, unchanged retry key, queued
  badge/polling, preserved notice, and no pre-reset content resurrection. Confirm
  service-worker asset version differs from the pre-change revision. Shut down
  the exact fixture server/browser in `finally`. If a browser is unavailable,
  report this task unverified rather than replacing it with a claimed Node E2E pass.
- [ ] Commit: `feat: expose durable receipts without changing local execution`.

## Task 9: Synthetic process lifecycle, restore epoch and operational proof

**Files:** Create `cloud/{worker,scheduler,synthetic,cli,telemetry}.py`,
`cloud/migrations/007_recovery.sql`, `tests/cloud/test_recovery.py`,
`docs/runbooks/durable-work-local.md`; extend `cloud/recovery.py`.

**Interfaces:** `SyntheticExecutor.run(claim: Claim, context: list[dict]) -> str`
returns `synthetic:<job_id>` without tools/network;
`SyntheticGenerator.generate(event: dict) -> tuple[str, dict | None]` returns
`('skipped', None)` without network;
`Worker(db, executor, generator).execute(claim: Claim) -> None` renews leases while running;
`Scheduler(db, poll: Callable[[PollClaim], MailPage] | None = None).once() -> dict`
runs recovery sweeps and due-mailbox work only when an explicit provider is injected;
`Recovery(db).begin_restore() -> UUID` disables runtime and rotates epoch;
`review_check(epoch: UUID, check: str, reviewer: str, reason: str) -> None` records
one named recovery check after evidence review;
`resume_after_review(epoch: UUID, reviewer: str, reason: str) -> None` rejects empty
review identity/reason and unresolved recovery checks.

- [ ] Write this restore fence test:

```python
import unittest
from uuid import uuid4
from cloud.types import Actor, Submission
from cloud.work import Work
from cloud.leases import Leases
from cloud.recovery import Recovery
from tests.cloud.support import sandbox

class RecoveryTests(unittest.TestCase):
    def test_restore_epoch_blocks_old_workers(self):
        with sandbox() as db:
            actor, work, leases = Actor('alice'), Work(db), Leases(db)
            cid = work.create_conversation(actor, 'chat')
            job = work.accept(actor, Submission(cid, 1, uuid4(), 'fixture'))
            old = leases.claim(job.job_id, 'old-worker')
            epoch = Recovery(db).begin_restore()
            self.assertNotEqual(epoch, old.epoch)
            self.assertFalse(leases.heartbeat(old))
            self.assertFalse(leases.finish(old, 'must not commit'))
            self.assertIsNone(leases.claim(job.job_id, 'new-worker'))
```

  Add: process killed after claim; before outbox publication; after synthetic effect
  intent; after provider fixture success but before receipt; and after completion
  commit before broker settlement. Add observed timings, no-secret-log assertions
  and test that no unsafe state automatically exits reconciliation.
- [ ] Run `verify_cloud.py --case tests.cloud.test_recovery`; expect absent lifecycle
  behavior then red crash/restore tests before adding process coordination.
- [ ] Implement synthetic worker with heartbeat thread, bounded attempt deadline,
  fail-closed lease loss, cooperative stop and process shutdown. Invoke no real
  Hermes/OpenAI/Google path. Dispatch, recovery and worker loops have independent
  entrypoints through `python -m cloud.cli <role>`, with signal-aware shutdown.
  CLI requires an explicit local-test configuration marker for synthetic operation;
  production worker startup rejects the absence of the approved runner/auth boundary.

```python
class SyntheticExecutor:
    def run(self, claim, context):
        return f'synthetic:{claim.job_id}'

class SyntheticGenerator:
    def generate(self, event):
        return ('skipped', None)
```

  Worker reads `Leases.payload`: chat/todo invokes the executor with
  `Leases.context` then `Leases.finish`; Gmail generation invokes the injected
  generator then `Todos.record_generation`. Each branch closes DB reads before
  execution. Synthetic skip is a test outcome, never a real classification claim.
  `Scheduler.once` obtains a fenced poll claim, invokes its injected provider
  outside the transaction, then commits through `Gmail.ingest_page`. With no
  provider it must not advance any mailbox cursor.

  Restore kernel: in one transaction disable runtime, rotate epoch, revoke all
  capabilities and publication leases, quarantine pre-restore dispatches, and mark
  in-flight/possibly effected work for review. Record recovery checklist entries
  for deletion/revocation evidence, source checkpoints, uncertain effects and
  credential invalidation. Default all entries unresolved; `resume_after_review`
  must refuse until each is explicitly reviewed. Old envelopes remain stale after
  resumption. An independent deletion/revocation store is a launch dependency, not
  replaced by records lost in the same database restoration.
  Check names are `deletions_revocations`, `source_checkpoints`, `uncertain_effects`,
  `credential_invalidation`. Persist reviewer, timestamp and reason for each;
  reject unknown check names or old epochs. These functions are operator-only
  CLI primitives, not unauthenticated web routes. Tests may record synthetic
  evidence, but that must never be reported as a production recovery sign-off.

  Emit JSON logs with identifiers, durations and safe reason codes only. Metrics
  include oldest queued/outbox age, expired leases, attempts, quarantined dispatches,
  reconciliation count and ingestion checkpoint age. Document alert intent; do not
  create Azure Monitor resources.

  Run a local `pg_dump`/restore rehearsal only between runner-created databases in
  its disposable PostgreSQL container. Keep dispatch disabled throughout restoration,
  then demonstrate old epoch rejection and reviewed selective resumption. Record
  local timings separately from Azure RPO/RTO validation. This exercise does not
  prove Azure's four-hour service recovery target.
- [ ] Run `verify_cloud.py --all`, the Node UI contract, and the two original
  image verification scripts only when the matching local images exist. Report
  skips accurately. Run all 18 legacy app verification scripts in fresh subprocesses
  with dotenv disabled and synthetic DBs. Confirm no container, schema, dependency
  target or server process was left behind by successful or failed verification.
  Obtain independent branch review and resolve blocking findings before handoff.
- [ ] Commit: `test: prove durable work recovery with synthetic processes`.

## Spec coverage and completion report

| Spec section | Owning tasks / explicit boundary |
| --- | --- |
| 1–2 scope and current code | All tasks; local SQLite and live configuration preserved. |
| 3 persistence/transactions | 1, 2, 5–7; cloud schema separate from local app. |
| 4 acceptance/order/status | 2, 3, 8. |
| 5 dispatch/crash recovery | 3, 4, 9. |
| 6 effects/control/credentials | 5, 6; no production tool transport or raw token store in this tranche. |
| 7 Gmail durable ingestion | 7, 9 using normalized synthetic provider pages; live Gmail remains gated. |
| 8 recovery/privacy | 6, 9; local restore only, production retention/deletion store and Azure rehearsal remain release gates. |
| 9 evidence | Tests within every task plus combined crash scenarios in 9. |
| 10 handoff | This plan and final review; no provisioning or feature expansion. |

Final report must distinguish implemented local contracts, real PostgreSQL results,
fake broker/provider coverage, browser verification, and untested Azure/real-agent
behavior. List remaining production authentication, Gmail consent, secret storage,
runner isolation, deletion/retention, dependency/image risk, budgets, quotas and
restore/load gates. Do not claim that building the durable core launches the beta.

## Sources checked for planning

- [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html): explicit transaction contexts with autocommit connections avoid accidental long-lived implicit transactions.
- [PostgreSQL locking clauses](https://www.postgresql.org/docs/17/sql-select.html): row-lock behavior for concurrent claiming; application fencing remains a separate contract.
- [Azure Service Bus Python client](https://learn.microsoft.com/en-us/python/api/overview/azure/servicebus-readme?view=azure-python): sender/receiver API surface, to be pinned and adapter-tested before use.
- PyPI metadata checked on 2026-09-26: psycopg 3.3.6 requires Python >=3.10; azure-servicebus 7.14.3 requires Python >=3.9. Package versions are planning inputs, not an installed or tested dependency set.

## Execution choice

Recommended: **native execution** in this task, followed by an independent branch
review. The nine tasks share database/ownership interfaces, so maintaining one
implementation context reduces coordination overhead. Alternative: fresh implementer
and reviewer agents for every task, with additional review cost. User review of
this plan and native execution were subsequently approved before product edits.
