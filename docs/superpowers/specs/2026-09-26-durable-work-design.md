# Athena: durable Gmail and chat work

**Status:** Written specification approved by the user on 2026-09-26 after review
handoff of commit `68a3457`. The PostgreSQL + Azure Service Bus direction was
approved earlier the same day. The implementation plan is
`docs/superpowers/plans/2026-09-26-durable-work.md`; the user subsequently approved
that plan and native execution with independent review. Approval is not cloud
provisioning or spending authorization.

## What changes for the user

- A request is saved before the app says it has accepted it.
- Messages arriving during another reply wait in conversation order; they are
  not silently replaced by the current run.
- Restarting a service does not erase accepted work or its status.
- Gmail processing can resume after a generation failure without skipping mail.
- If an external action might already have happened, the app says it needs
  checking rather than blindly trying again.
- For a major database disaster, the approved beta targets are at most 15 minutes
  of lost recent data and restoration within 4 hours. These are targets requiring
  a restore drill, not guarantees already demonstrated.

## 1. Scope and decision record

This is the persistence and durable-execution subproject of the Azure rollout,
not the complete cloud launch design. Target: 50 invited users, self-service
Gmail connection and existing in-app chat, separate staging/production, one India
region, and brief planned downtime acceptable. Local data stays untouched; cloud
starts with fresh accounts/data. No feature rewrite or new frontend framework.

Selected: PostgreSQL is the authoritative record; Service Bus dispatches work.
An application transaction stores input, job and dispatch intent together. The
queue contains opaque identifiers, not mailbox bodies or credentials. Database
ownership and idempotency remain mandatory even if broker deduplication is enabled.

Alternative considered: PostgreSQL-only dispatch would reduce infrastructure but
make the application own more queue operations. It is not selected. Keeping the
current in-memory registry is not a durable alternative.

Excluded: WhatsApp activation, personal WhatsApp access, Pocket/Fathom/digest
activation, desktop/mobile companion, browser-history/system ingestion, forms,
orders, OTP handling, casting and Jev. Existing local integrations/data are not
deleted. No new upload capability is introduced.

This design does not select an Azure region/SKU, execution sandbox product, budget,
or retention policy. Those require separate release decisions. It does not approve
remaining Hermes image vulnerabilities or previously exposed credentials.

## 2. Current code and the boundary being changed

Assessment base: application commit `f18c5ac` in the isolated worktree.

- `agent/runs.py` owns runs in a process-local dictionary and daemon threads.
  `app.py:ask_ai` and `_start_chat_turn` acknowledge that memory-only work.
- `db.py:set_chat` and `app.py:_persist_resolution` replace whole conversation
  values. A synthetic in-memory check reproduced an executor completion erasing
  a newer notice written through `db.py:append_chat_bubble`.
- `pollers/gmail/poller.py:poll` commits a cursor before returning events to
  `main.py:_poll_gmail_account` for generation. There is no durable pending-job
  consumer for a generation failure after that point.
- Database helpers commit internally; their transaction ownership must change
  before composing input + job + outbox atomically.
- Agent tools currently open the application SQLite database directly. The cloud
  runner must not receive a PostgreSQL credential in place of that file path.

The web service owns authentication, acceptance and status reads. The scheduler
owns due-mailbox discovery and recovery sweeps. Workers own leased jobs. A
dispatcher publishes committed outbox records. These are separate process roles
with shared transactional contracts, not independent databases or microservices
for every table.

## 3. Authoritative records and transaction rules

Logical records below define behavior, not final migration SQL or filenames.

| Record | Required invariant |
| --- | --- |
| User and source connection | Stable owner/connection IDs; connection generation and active/revoked state fence stale work. Credentials are protected and unavailable to agent sandboxes. |
| Conversation | Owner, kind (chat or todo), generation, next message/order counters, active lease and reconciliation hold. At most one currently authorized attempt per conversation. |
| Message | Append-only identity, owner/conversation/generation, sequence, role, originating job/event and content. Unique origin prevents duplicate completion/notice appends. |
| Job | Immutable input references, owner, conversation order if applicable, state, deadlines, attempt budget and connection generation. Acceptance idempotency key is unique within owner and request scope. |
| Attempt | Job ID, monotonically increasing fencing token, worker, lease/heartbeat, timings and outcome. Old attempts cannot commit current results. |
| Ingested event and mailbox checkpoint | Tenant/connection-scoped source identity, processing identity and cursor/backfill progress. Every event requiring work has a durable job or recorded terminal decision. |
| Outbox | Dispatch ID, job ID, schema version, due time, publish lease and send outcome. Committed atomically with new/retried work. |
| Effect | Server-issued operation identity, authorized target/arguments fingerprint, job/attempt context, intent, provider receipt and confirmed/uncertain outcome. Secrets are excluded. |

Use PostgreSQL constraints and owner-qualified foreign keys to reject cross-user
relationships, not just prompt instructions. Every request/repository operation
takes authenticated ownership context. A connection/account argument cannot change
the caller's owner. Prefer typed timestamps with timezone and validated structured
data over implicit SQLite TEXT behavior.

The application service owns the transaction. Repository helpers do not commit.
No provider/model call runs while a database transaction or row lock is held.
Migrations run as a controlled release step, not inside every request or replica.
The local SQLite path remains separate; there is no dual-write or automatic import
of a user's local database into cloud.

## 4. Acceptance, ordering and status

The client sends a stable idempotency key per user submission and reuses it on
transport retry. In one transaction the server validates ownership/limits,
locks the conversation briefly, appends the user message, assigns job order,
creates the job and creates its outbox record. Only after commit does it return
an accepted receipt with the durable job ID. A repeated key with the same input
returns that receipt; different input under the same key is a conflict.

If the commit response is lost, the client retries the same key. If PostgreSQL is
unavailable, the app must not claim acceptance. A broker outage can delay execution
without losing a database-accepted request. Explicit queue limits reject excess
work before acceptance rather than silently dropping it afterward.

Messages are ordered by database-assigned sequence, not browser timestamps or
broker arrival order. The worker builds context when the turn becomes runnable,
using prior completed turns and relevant notices; it excludes later queued user
messages from the current turn. Completion appends one reply tied to the job;
it never replaces a stale whole-thread snapshot. Display may group a reply with
its originating user message without changing execution order. Reply append, job
completion, conversation lease release and dispatch intent for the next eligible
turn commit together; a completion retry cannot append a second reply.

A failed/cancelled/expired turn permits the next turn to proceed unless an effect
is uncertain. A reconciliation hold blocks later turns in that conversation so
they cannot accidentally repeat the unresolved action. Other conversations/users
continue. The UI distinguishes queued, running, retry pending, completed, failed,
cancelled, expired and needs-reconciliation, with safe reasons and elapsed time.

## 5. Dispatch and crash recovery

Service Bus is an at-least-once wake-up channel, not proof of job ownership or
completion. Use explicit settlement rather than receive-and-delete. Microsoft
documents lock loss, redelivery and even completion-acknowledgement failure; the
application must tolerate these. [Service Bus settlement](https://learn.microsoft.com/en-us/azure/service-bus-messaging/message-transfers-locks-settlement).

The dispatcher leases committed outbox rows, sends, then records publication.
Failure after send but before that record can cause a resend. Reuse the same
dispatch ID for that resend; a deliberately new recovery notification receives
a new dispatch ID for the same job. Broker duplicate detection is optional
defense-in-depth and time-windowed, so it cannot replace persistent job checks.
[Service Bus duplicate detection](https://learn.microsoft.com/en-us/azure/service-bus-messaging/duplicate-detection).

A receiver validates the envelope and looks up the job. Terminal, unknown or
stale-generation notifications cannot start execution. Eligible jobs are claimed
in PostgreSQL with an attempt fencing token and conversation/mailbox ownership.
The receiver settles the wake-up only after this durable claim, or after confirming
it is redundant/not yet eligible. Execution then belongs to the database lease,
not the broker lock. A crash immediately after claim is recovered by lease expiry.
Database unavailability is not evidence that a job is unknown: do not settle a
notification on a failed lookup. Invalid envelope versions are quarantined with
a safe diagnostic, not treated as an instruction or executed speculatively.

A database recovery sweep repairs lost wake-ups, out-of-order deliveries and
expired claims. It creates a new outbox notification for eligible queued work
with no valid claim and no recent dispatch after a bounded delay. A historical
published-outbox row does not prevent redispatch forever: the proposed delay is
60 seconds since the last notification, provided no dispatcher lease is active.
Thus settling an early
notification for a later conversation turn cannot strand it. This sweep also
checks poisoned/dead-lettered dispatches and records actionable failures rather
than endlessly republishing malformed envelopes. Dead-letter replay is explicit.

Proposed beta defaults for review: 60-second database leases, heartbeat every
15 seconds, recovery sweep every 30 seconds, at most three total attempts for
retryable no-effect execution, with 5-second then 30-second jittered delays.
Use database time. Deadlines and attempt counters survive restart. These are
configurable operating defaults, not performance commitments. Transport resend
does not consume an execution attempt; genuine reclaims do. Permanent validation
errors fail without retries. Job expiry is checked before a claim and each tool
dispatch; expiry after a possible mutation requires reconciliation, not silent
discard. The implementation plan must choose explicit queue/deadline limits and
prove those limits cannot discard already-accepted work without a visible outcome.

Lease loss revokes the worker's tool capability and requests sandbox termination.
Every result/heartbeat/state mutation compares the current fencing token.
An old worker cannot overwrite a newer attempt. A delayed process may briefly
continue model computation after lease loss; the design does not promise zero
duplicate computation. Tool access fails closed if current ownership cannot be
verified, and stale results cannot commit. External actions require the
additional effect protocol below; database fencing cannot undo a provider call.

## 6. External effects, cancellation and credentials

Only an authorized tool boundary may perform external mutations. Before a call,
it validates user, job, active attempt, conversation/connection generations,
scope, cancellation and allowed operation. It durably records effect intent, then
calls the provider without a database lock, then records the result/receipt.
Concurrent duplicate tool requests must not both dispatch the same operation.
An operation ID is issued by the server and reused by transport retries; it is
not a model-supplied assertion that two unrelated requests are identical.

Known read-only operations may be retried within budget. Once a write might have
been dispatched, a timeout or worker death is uncertain, not proof of failure.
Do not replay an entire agent turn after any recorded mutation intent. A verified
provider idempotency/lookup contract may later resolve an effect; otherwise mark
needs-reconciliation and require an operator/user resolution with an audit reason.
Even a confirmed completed effect followed by lost model output must not trigger
blind whole-turn replay. There is no universal exactly-once external-action claim.

Cancellation is a durable request. Queued jobs become cancelled; running jobs
lose capabilities and receive a stop request. An already-dispatched provider
operation may finish and must retain its outcome or reconciliation state. Do not
tell the user a message was unsent merely because the process stopped.

Reset increments conversation generation and invalidates old work. Late results
cannot recreate cleared visible history. Necessary minimal effect/job audit state
survives reset under the eventual retention policy. Reset must not clear an
unresolved effect hold; show that outstanding issue separately from the new chat.

Disconnect marks a connection revoked and increments its generation before
cleanup. New claims and tool calls reject the old generation. Token refresh writes
are conditional updates to the still-active generation, never an upsert that can
recreate a disconnected account. A provider call already in flight cannot be
promised cancelled; record/reconcile it. Account deletion disables work first.

Agents receive neither database credentials nor long-lived Google tokens. The
runner integration must provide short-lived job/attempt-scoped tool access and
deny alternate credentialed egress. The durable-work implementation starts with
a synthetic executor; real autonomous external effects remain disabled until this
boundary and its isolation tests are implemented in the runner subproject.

## 7. Gmail ingestion boundary

One fenced poll lease exists per active connection generation. Capture a baseline
for initial three-day backfill, persist page progress, and durably save each page's
normalized inputs plus generation jobs. Advance the final backfill/history cursor
only when everything it covers is committed for recoverable processing. Model
generation happens later and cannot be a prerequisite for holding a poll lock.

Repeated pages use connection-scoped identities. For received-mail generation,
the identity includes the connection and provider message ID; history occurrences
for label changes include their occurrence identity rather than only message/type.
Overlapping backfill/history must not generate duplicate jobs/todos. Do not reuse
the current global `event_id` primary key as the new cross-tenant identity.

Generation records a durable terminal decision: created todo, duplicate or skipped.
Todo creation and any in-app notice intent commit together. Provider notifications
are separate effects, not an inline untracked tail after saving a todo. If history
is no longer available, record a resync requirement and schedule the bounded
backfill without silently claiming full historical recovery. Revoked connections
stop both polling and queued work; reconnect creates a new authorized generation.

## 8. Recovery, privacy and observability

Ordinary restarts: no loss of committed accepted jobs. Major database disaster:
approved target RPO <=15 minutes and RTO <=4 hours, measured through a timed
restore rehearsal. Recovery time measures restoration of safe user-facing service;
individual uncertain actions may remain held rather than being replayed to meet
the clock. Azure's backup capability must be verified for the selected
configuration; documented backup behavior alone is not proof the whole application
meets those targets. [Azure PostgreSQL backup and restore](https://learn.microsoft.com/en-us/azure/postgresql/backup-restore/concepts-backup-restore).

Restore into an isolated environment with ingestion, dispatch and external effects
disabled. Start a new recovery epoch; invalidate all old worker capabilities and
queue envelopes. Review connections, deletion records and effects across the lost
data window before any replay. A restored database cannot know every action that
happened after its restore point; uncertainty must remain visible. An available
independent deletion/revocation record is a release prerequisite, not something a
backup of the same database can guarantee. If it is unavailable, keep affected
accounts/work disabled until reconciled. Rotate/invalidate relevant access where
needed before restoring service.

Logs contain job/attempt/dispatch IDs, timings and safe reason codes by default,
not message bodies, addresses, prompts, tokens or provider response bodies.
Measure acknowledgement, queue age, attempt startup, model/tool duration, retry
count, lease expiry, dead-letter age, reconciliation holds and final latency
separately. Alert on stalled ingestion, outbox backlog and recovery-sweep failure.
Retention periods, deletion backup expiry and operator access are separate release
policy decisions; production launch is blocked until they are approved and tested.

Hermes's private session SQLite is not the app database. This subproject does not
copy a shared Hermes home into workers. A future runner design must establish
scoped conversation continuity and cleanup, or reconstruct sufficient context from
authorized durable messages, before claiming real multi-turn compatibility.

## 9. Required acceptance evidence

Tests use synthetic users/data and real PostgreSQL transactions in an isolated
local test environment. Broker adapter tests simulate failure boundaries; staging
must separately exercise actual Service Bus delivery and settlement. Mocks alone
do not establish database concurrency or Azure behavior.

| Failure / scenario | Required evidence |
| --- | --- |
| Commit fails; response lost after commit | No false acceptance; same request key recovers one durable job. |
| Concurrent submissions and poller notice | Ordered turns, one active attempt, both notice and completion retained. |
| Duplicate/out-of-order wake-ups; broker outage | One authorized attempt and completion; no replay of completed effects; recovery sweep dispatches eligible work. |
| Worker dies immediately after claim | Expired lease recovered; committed input remains available. |
| Old worker returns after replacement | Fenced completion/heartbeat/tool call rejected. |
| Effect succeeds; receipt or completion is lost | Visible reconciliation hold, no automatic external replay. |
| Stop/reset while running | Durable cancellation/generation change; no stale history resurrection or false undo claim. |
| Gmail generation fails after ingestion | Pending generation recoverable; cursor does not skip unrecorded work. |
| Overlap, duplicate pages, revoked connection | Tenant-scoped dedup; stale scheduler/refresh cannot revive access. |
| Cross-user IDs and tool requests | Ownership rejected in service and persistence boundaries. |
| Poisoned envelope / exhausted attempts | Bounded processing; visible failure with audited manual replay. |
| Database restore and queue mismatch | Old epoch cannot execute; uncertain effects/deletions reviewed before enabling work. |

## 10. Handoff and remaining gates

After user review of this written spec, create the code-level implementation plan
for persistence and durable work, mapping exact files, migrations and tests. The
plan must preserve the local SQLite app and use a synthetic runner until scoped
execution is ready. It must be reviewed before selecting implementation execution.

Separate release work remains: region/quotas/credit coverage and cost approval;
Gmail consent/scopes and verification; protected credential storage; sandbox and
tool isolation; image risk remediation; user deletion/retention; real integration
and load evidence; backup restore rehearsal; rollback and operational ownership.
This spec is not approval to provision Azure, push images, activate sources or
send real messages. No claim of deployed or production-ready behavior is made.
