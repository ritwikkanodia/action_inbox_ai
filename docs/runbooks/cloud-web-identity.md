# Cloud identity and web operations

## Delivery boundary

This package is the invited-user identity and web boundary, **not a deployed beta**.
It adds Google identity-only sign-in, 50 reserved/admitted seats, PostgreSQL sessions,
isolated cloud pages and authenticated durable APIs. Production chat submission is
rejected with `executor_not_integrated`; Gmail is visibly unavailable. Synthetic
browser replies are test fixtures only.

The original `cloud.app.create_app` remains a compatibility factory with an
explicit testing-only principal. Production-capable composition is
`cloud.web.create_web_app`, started by the explicit WSGI factory. Neither imports
the SQLite app, discovers dotenv, migrates on startup or starts local pollers.
No local accounts, mailbox credentials or personal data are migrated.

## Explicit configuration

Use separate staging/production origins, databases, credentials and encryption
keys. Do not point these commands at the personal local checkout or SQLite data.

| Key | Requirement |
| --- | --- |
| ATHENA_DATABASE_URL | PostgreSQL DSN with host, user, database and sslmode=verify-full; inject securely |
| ATHENA_DATABASE_SCHEMA | Explicit schema, identifier of at most 63 characters |
| ATHENA_ENVIRONMENT | staging or production |
| ATHENA_PUBLIC_ORIGIN | Canonical HTTPS origin, no path or trailing slash |
| ATHENA_GOOGLE_CLIENT_ID | Dedicated identity-only OAuth web client |
| ATHENA_GOOGLE_CLIENT_SECRET | Secret from the dedicated OAuth client |
| ATHENA_AUTH_FLOW_KEY | Environment-specific Fernet key, injected from a secret store |

Operator commands need only the first two keys. Web startup needs all seven.
Model keys and Gmail refresh tokens do not belong in this web environment.
Rotate credentials previously shared in chat before public deployment.

Future configured-cloud commands (not executed against a real environment):

```sh
python -m cloud.identity.cli migrate
gunicorn --config cloud/gunicorn.conf.py 'cloud.wsgi:create_from_env()'
```

Use hash-locked `requirements-cloud.lock` for the cloud dependencies. Migrations
are additive and checksum-checked; an existing checksum mismatch is an error, not
permission to edit history. Provision the selected schema/role explicitly.
Web readiness does not migrate. Back up and rehearse upgrades before production.

## Identity, invitations and sessions

Register exactly `ATHENA_PUBLIC_ORIGIN/oauth/login/callback` in the dedicated
Google client. Scopes are only `openid email profile`, with PKCE S256, state and
nonce; login never connects Gmail. Live registration and provider verification
remain deployment gates.

New admission requires an exact normalized invited Gmail address or a Workspace
identity with Google's hosted-domain claim. Other third-party-email Google
accounts are denied for now. Stable Google subject, not email, identifies an
existing account. Emails are lowercased/trimmed; aliases/dots are not collapsed.

An invitation reserves a seat for seven days. There are at most 50 seats, counting
active outstanding invitations and every admitted identity, including disabled
identities. Revoking or expiring an unused invitation releases its reservation.
Disabling an account does not delete it or reclaim its admitted seat.

Examples use placeholders, not real account data:

```sh
python -m cloud.identity.cli invite --email person@gmail.com --actor operator-id --reason "Approved beta invitation"
python -m cloud.identity.cli revoke-invite --invite-id INVITE_UUID --actor operator-id --reason "Invitation withdrawn"
python -m cloud.identity.cli disable-user --owner-id OWNER_UUID --actor operator-id --reason "Access revoked"
python -m cloud.identity.cli revoke-sessions --owner-id OWNER_UUID --actor operator-id --reason "Sign out all devices"
python -m cloud.identity.cli purge
```

Only `migrate` applies migrations. Operator actions require explicit audit actor
and reason. Keep those values free of credentials/message content. Output contains
fixed result codes, bounded purge counts and opaque invitation IDs. There is no
public admin route, enable-execution command, account re-enable command, deletion
claim or seat-cap bypass.

Sessions have seven-day absolute and 24-hour idle expiry; issuing a sixth active
session revokes the oldest. Browser cookies are host-only, Secure, HttpOnly and
SameSite=Lax. Only hashes are stored. Current-session logout and logout-all require
CSRF, same-origin evidence and the current opaque session context. A stale tab
cannot submit into a new account. Database outage fails closed; a failed sign-out
must not be represented as confirmed revocation.

Authentication failures do not delete shared cookies: a delayed 401 could otherwise
erase a newer login from another window. Explicit successful logout clears them;
expired/revoked cookies confer no authority and are replaced on the next login.
After unconfirmed logout, the current page stays blank until deliberate navigation
or reload, rather than automatically restoring account content.

Login requires JavaScript: Chrome sends Origin:null for a no-referrer navigation
form POST. The cloud sign-in controller instead makes a same-origin CORS-mode
POST with CSRF, receives the fixed Google authorization URL, then navigates.
This preserves both strict Origin checking and no-referrer. No null-origin
exception or unsafe-inline CSP relaxation is used.

The limiter permits ten login starts per signed pre-login browser per ten minutes
and 100 globally per minute, shared in PostgreSQL. Rejections carry Retry-After.
The purge command removes at most 500 expired/revoked session rows, 500 expired
flows and 500 old limiter rows per invocation; it does not delete identities,
invites or audit records. Arrange a cloud-owned scheduled purge before launch.

## Web/ingress requirements

- Use a new dedicated origin with no legacy service-worker history.
- Terminate HTTPS at a trusted ingress and prevent direct public backend access.
- Preserve the configured Host, including any explicit port. Health probes must
  send that Host; forwarded headers are not trusted to establish authority.
- Do not weaken CSP. Assets are same-origin; all user/provider text is rendered
  as text, without Markdown/HTML interpretation or arbitrary profile images.
- Responses are no-store; identity routes are no-referrer. Production HTTPS adds
  HSTS without preload or includeSubDomains.
- The supplied Gunicorn access format logs method, path, status and duration only.
  Do not override it with raw request-line, query, cookie or Referer logging.
  Apply the same exclusions to ingress/APM/error-reporting configuration.
- `/live` reports process life only. `/ready` checks schema checksums, identity
  storage and recovery hold, not Google, Gmail, Hermes or Azure connectivity.
  An ordinary execution pause does not fail web readiness.
- Production capabilities remain false and message submission remains disabled.
  Stored status/cancel/reset and empty-thread creation retain durable semantics.

Content and drafts stay in page memory; there is no account storage in browser
storage/cache or a cloud service worker. Tabs clear content on visibility/page
lifecycle changes, invalidate outstanding responses, and revalidate sessions.
Without BroadcastChannel, conversation responses and sends additionally recheck
bootstrap. Already displayed content cannot be instantaneously erased remotely.

## Restore and key rotation

Keep ingress/execution off before restoring, and call the existing
`Recovery.begin_restore()` procedure to rotate the runtime epoch. Old sessions
and pending/claimed logins cannot gain new-epoch authority. Reapply independently
retained account/session and invitation revocations **during the hold** using
the audited operator commands. These commands can reduce authority during a hold;
new invitations, login and protected user reads remain blocked.

Review all four persisted recovery checks: deletions/revocations, source
checkpoints, uncertain effects and credential invalidation. Only the existing
explicit reviewed-resume procedure releases the hold. Uncertain old jobs still
need individual reconciliation; do not blindly replay them. A revocation journal
stored only inside the same backup cannot prove post-backup revocations.

Rotating the flow key invalidates signed pre-login cookies, encrypted verifiers
and todo cursors. It does not revoke opaque sessions; use session controls or a
recovery epoch change. Never reuse browser secrets/origin across DB environments.

The local disposable dump/restore test is not an Azure backup or measured
15-minute RPO/four-hour RTO result.

## Verification

Run from the isolated worktree with the existing interpreter, Docker, Chrome,
Playwright and Node. The runner installs hash-locked additions into a disposable
target, creates its own labelled loopback PostgreSQL container and random schemas,
excludes provider credentials and refuses supplied database targets. Browser
tests use a fresh profile and intercept the fixed Google URL with signed synthetic
identity responses; no request reaches Google or a real mailbox/model.

```sh
PYTHON_DOTENV_DISABLED=1 ../action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --all
node scripts/verify/verify_durable_work_ui.cjs
node scripts/verify/verify_cloud_identity_ui.cjs
node scripts/verify/verify_cloud_identity_lifecycle.cjs
node --check static/js/cloud-state.js
node --check static/js/cloud-app.js
node --check static/js/cloud-login.js
PYTHON_DOTENV_DISABLED=1 ../action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --browser
PYTHON_DOTENV_DISABLED=1 ../action_inbox_ai/venv/bin/python scripts/verify/verify_cloud.py --identity-browser
```

Focused service tests include signed JWT verification, callback replay, provider
failure/size/timeouts, admission races, transaction-vs-revocation races, checksum
readiness, same-origin/CSRF/session context, multi-owner pagination/thread binding,
expiry/recovery and operator commands. Production cookie flags are checked over
HTTPS Flask requests; the Chrome fixture uses explicitly test-only loopback
cookies. Production configuration cannot enable that allowance.

## Remaining release gates

Observed local evidence on 2026-09-26: 154 cloud tests and all 18 legacy scripts
passed. Both Node contracts, three real-controller lifecycle regressions and
JavaScript syntax checks passed. Fresh-profile
Chrome checks passed for the legacy durable UI and cloud identity lifecycle,
including lost-response retry, two tabs, restart, account switching with delayed
responses, expiry and logout-all, both with and without BroadcastChannel.
Gunicorn's actual access formatter was checked with synthetic secret markers.
These are local synthetic results, not live-provider or deployed-cloud results.

One independent whole-branch review found three important browser lifecycle
issues: delayed bootstrap display without cross-tab messaging, automatic account
display after failed logout, and a stale 401 deleting a newer session cookie.
Each was reproduced by a failing regression before a single fix pass. The
controller regressions and HTTP suite now pass; fixes are test-verified, not
subject to a second independent review. No minor findings were deferred.

- Independent security/release assessment, threat modelling and load/abuse tests.
- Dedicated OAuth client registration, rotated secrets, consent/privacy/deletion
  policy, real-provider tests and a protected Google credential vault for Gmail.
- Isolated Hermes/tool execution, dependency/image risk review and integration;
  real Service Bus delivery/redelivery and provider capability enforcement.
- Reproducible production builds, reviewed CI/deployment identity/secrets, Azure
  provisioning, India-region availability/quotas, approved credit/cost budget.
- Production backup/restore and rollback rehearsal, independent revocation/deletion
  evidence, measured RPO/RTO, monitoring ownership and retention/deletion operations.
- Separate staging validation and deliberate release approval.

Do not treat this implementation or its synthetic tests as permission to deploy,
push, merge, migrate personal data, activate sources or enable real task execution.
