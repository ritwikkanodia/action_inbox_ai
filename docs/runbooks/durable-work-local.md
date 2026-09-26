# Durable work: local verification and recovery

## Delivery boundary

This is a locally verified durable backend, **not a cloud deployment**. It is
separate from the running SQLite application. No user data is imported, no real
mailbox or model is called, and no source is activated. The cloud Flask factory
denies authentication unless an explicit test-only principal is injected.
Production worker startup is refused. Do not make this test factory public.

PostgreSQL owns acceptance, ordering, leases, effect evidence and outbox records.
The Service Bus adapter is covered with injected SDK fakes. CLI dispatch and
delivery are explicitly local simulations, not an Azure connectivity test.
The Gmail adapter consumes normalized synthetic pages; generation is synthetic.

Every initial backfill page, including a single-page listing, requires a baseline
captured **before** the listing. Backfill completion installs that baseline, not
the potentially newer final-page cursor; subsequent history ingestion catches up
arrivals during the listing. Credential refresh results must include the recovery
epoch and connection generation captured before the provider request.

## Run verification

Use the existing main checkout interpreter, Docker and Node. Do not install into
the shared interpreter or create another worktree virtual environment. From this
worktree:

```sh
PYTHON_DOTENV_DISABLED=1 ../action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --all
node scripts/verify/verify_durable_work_ui.cjs
PYTHON_DOTENV_DISABLED=1 ../action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --browser
```

The browser check requires installed Chrome and Playwright in the existing
interpreter. It creates a fresh headless profile, uses synthetic data and only
permits loopback traffic; it does not use a personal browser profile.
It also installs a synthetic old cache-first service worker in a second fresh
profile and verifies that the first durable navigation uses versioned current
scripts. Rejected text remains recoverable while the page is open; it is not
persisted across reload or silently resubmitted after a conversation reset.

For a focused test, replace `--all` with `--case tests.cloud.test_recovery` (or
another `tests.cloud.test_*` module). The runner refuses a supplied database URL.
It installs hash-locked additions into a temporary target, owns one labelled
loopback-only PostgreSQL container with tmpfs storage and creates a random schema
per test. Normal failures clean up schemas, the owned container and dependencies.
Inherited provider credentials are excluded and dotenv is disabled. Python
cloud-test socket calls reject remote addresses; this is not an OS-level network
sandbox or a guarantee about arbitrary legacy/native subprocesses.

If the runner itself is forcibly killed, inspect labelled containers with
`docker ps -a --filter label=ai.athena.verify=cloud` and temporary directories
named `athena-cloud-deps-*`. Verify the exact name, creation time and ownership
before removing any orphan. Never bulk-delete containers, home directories or
other plans' verification artifacts.

## Local process roles

`python -m cloud.cli --help` lists worker, scheduler, dispatcher, metrics and
operator-only restore roles. Every role requires `--synthetic`, a runner-created
`--schema verify_<uuid>` and that runner's temporary local configuration marker.
Use the CLI test rather than copying test credentials into a persistent shell.
`--once` runs one cycle. SIGINT/SIGTERM requests cooperative shutdown.

The scheduler sweeps expired work even without a provider, but creates no mailbox
checkpoint or cursor progress without an explicitly injected provider. The worker
loads authorized context, closes database reads, runs the synthetic component and
renews its lease from the controlling loop. Lost authority prevents commit. A
non-cooperative injected callback may keep computing until process exit; this
thread lifecycle is **not** the future real-agent isolation boundary.

## Recovery rehearsal

`tests.cloud.test_recovery` kills real synthetic child processes at five points:
before publication, after claim, after intent, after fake-provider success before
receipt, and after completion before settlement. It verifies retry only where
safe, uncertainty holds, and no duplicate completed reply.

The dump/restore test uses `pg_dump` and `psql` only between databases inside the
runner's owned container. Runtime is disabled before backup and remains disabled
after restore. No dump file contains personal data or is written to the host.

After any actual restore, keep ingress/dispatch/execution disabled, run the
operator-only `begin_restore`, then review these four checks against independent
evidence before `resume_after_review`:

- `deletions_revocations`: reapply deletions and access revocations since backup.
- `source_checkpoints`: reconcile provider history and incomplete page chains.
- `uncertain_effects`: check outcomes with providers; never blindly replay turns.
- `credential_invalidation`: invalidate stale credentials and capabilities.

Each review records the recovery epoch, reviewer, timestamp and nonempty reason.
Restoration rotates the epoch, revokes capabilities/publication leases and
quarantines old dispatches. **All unfinished jobs are held**, including jobs that
were queued at backup: they could have executed during the lost window. Reviewed
runtime resumption does not clear those holds. Explicit per-job reconciliation
closes old work without replay; any new action needs a fresh request. Old workers,
refresh results and envelopes cannot acquire the new epoch's authority.

Fixture review reasons are not production sign-off. Local timings printed by the
tests do not establish the approved Azure targets of <=15-minute data loss and
<=4-hour service restoration. Independent deletion/revocation evidence cannot
live solely in the same restored database.

## Observability and alert intent

Structured logs allow only event names, opaque IDs, durations, attempts and safe
reason codes. Do not log prompts, mailbox bodies, capabilities or credentials.
Local metrics cover oldest queued/outbox age, expired leases, attempts/retries,
quarantined dispatches, reconciliation holds and checkpoint age.

Before launch, alert on sustained queue/outbox delay, expired leases beyond a
sweep, any new quarantine/uncertainty, and unexpectedly stale active checkpoints.
Set thresholds from load tests and distinguish disconnected/idle sources. Add
provider/model/tool timing only through the approved isolated runner. No Azure
Monitor resources or production alerts are created by this implementation.
Known deferred metric issue: oldest outbox age currently includes unpublished
rows for terminal jobs. Filter actionable work before enabling backlog alerts.

## Remaining launch gates

Local evidence on 2026-09-26: 77 PostgreSQL/API/lifecycle tests, all 18 legacy
verification scripts, the Node contract and fresh-profile Chrome flows passed.
The Chrome flows include definitive rejection recovery, stale-generation drafts
and an installed old service worker. Existing offline image suites passed 3/3
for `athena-hermes:local` and 12/12 for `athena-hermes:patched-amd64`; these do not
clear outstanding vulnerability risk. Independent review found four important
issues; regression tests reproduced each before the fixes passed. No second
review was performed. One minor metric issue is deferred as documented above.

- Production identity/session security, owner isolation review and Gmail consent.
- Protected credential vault and per-call capability enforcement in a separate
  real-agent/tool transport; process/network isolation and dependency/image risk.
- Rotate credentials previously shared in chat before any public deployment.
- Independent deletion/revocation evidence, retention/deletion operations and
  production backup/restore drill with measured RPO/RTO and rollback procedure.
- Real Service Bus delivery/redelivery tests, Azure deployment and load tests.
- Region availability, quotas, credits, operating budget and monitoring ownership.

Do not switch the live app, provision, push, merge or activate real sources merely
because these local tests pass.
