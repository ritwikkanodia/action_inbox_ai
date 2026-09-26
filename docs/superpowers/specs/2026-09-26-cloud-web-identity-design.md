# Cloud web application and identity boundary

Date: 2026-09-26

Status: Proposed written design, awaiting user review. The user approved the separate-cloud-app approach in conversation; this document is not yet implementation or deployment approval.

Repository: `tryathena/athena`. Baseline: merged durable-backend PR #45, main commit `6afed644ba7644d48602f536332ffaf55634d6e2`.

## 1. Outcome and boundaries

Prepare the first productionization slice of Athena: invited people can sign in with Google, receive an isolated cloud account, and use real cloud pages backed by PostgreSQL. Reuse the Athena appearance and durable work contracts without importing the local SQLite application.

The overall beta remains 50 invited, self-service users, Gmail plus in-app chat, separate staging and production, and Azure in one India region. Brief downtime is acceptable. Recovery targets remain at most 15 minutes of data loss and four hours to restore service after a database disaster; this slice does not prove those targets.

This slice delivers identity, invitations, sessions, web pages and authenticated durable API access. It does **not** deliver live Gmail ingestion, model replies, Hermes isolation, cloud infrastructure, deployment automation or a launch-ready beta. A test worker can demonstrate durable chat with synthetic replies; production must not substitute that worker for Hermes.

Preserve the local checkout, SQLite data, credentials, running services and unrelated worktree files. Start cloud accounts fresh. Do not migrate local accounts by email or copy local mailbox credentials. Do not call a live mailbox/model, register OAuth clients, activate sources or provision Azure during implementation of this slice.

## 2. Architecture decision

Extend `cloud/app.py` into a separate production-capable Flask application. Keep its dependencies pointed at `cloud/`, not legacy `app.py`, `auth.py`, `db.py` or local pollers.

Alternatives considered:

- Retrofit the local server with two storage modes: rejected because its login, settings and executor paths mix SQLite and laptop services.
- Rewrite the product in a new web framework: rejected because it adds migration work without improving this slice's identity or durability contract.

The selected approach retains the durable backend and visual assets. Cloud pages get a small dedicated controller rather than loading the entire legacy `static/js/app.js`. That script still calls local todo, settings, push and executor endpoints. Reuse `static/js/durable-work.js` where its contract fits; do not silently port laptop-only controls.

Boundaries:

1. Configuration validates a fixed environment, public origin and Google client configuration.
2. Google identity adapter exchanges a code and verifies identity using maintained Google/OAuth libraries.
3. PostgreSQL identity services manage admission, browser sessions and revocation.
4. Cloud HTTP handlers resolve authenticated owners and enforce browser protections.
5. Cloud page services provide owner-scoped todo and conversation views over the existing durable services.

No new auth SaaS, password system, Redis session store or public administration UI is introduced.

## 3. Google sign-in

Use the server-side authorization-code flow with state, nonce and PKCE S256. Request `openid email profile` only, without offline access or incremental Gmail grants. Production identity clients must be separate from clients previously used for broad local Workspace consent. Login does not mark Gmail connected.

Use a configured HTTPS public origin and one callback path, `/oauth/login/callback`. Never build redirects from untrusted Host or forwarded headers. Staging and production use distinct origins, client credentials, databases and browser secrets. After login, redirect to the fixed `/chat` path; do not accept an arbitrary return URL.

Flow:

1. `GET /login` displays the sign-in page and supplies a ten-minute pre-login browser token. It contains no account or invitation lookup result.
2. `POST /oauth/login` checks same-origin submission and a CSRF token bound to that pre-login browser. It creates a ten-minute, one-use flow record and redirects to Google.
3. The callback requires the matching state and pre-login browser binding. Atomically consume the flow before exchanging the code, without holding a database lock during the network call. A duplicate callback cannot exchange again or create a second session.
4. Exchange and verify through the provider adapter, with bounded network timeouts and response sizes. Verify signature, allowed Google issuer, exact configured audience, authorized party when present, expiry, issued-at sanity and nonce. Reject missing or invalid security-relevant claims; tolerate unrelated additional provider claims. Normalize the two documented Google issuer forms to one internal provider identity.
5. In a transaction, admit or locate the verified identity, then issue a new browser session. If any stage fails, create no account/session and ask the person to restart sign-in. Never automatically replay an ambiguous token exchange.

Use `(provider, sub)` as the immutable login identity. Email is display/admission data, never the account key. Require a verified email to redeem an invitation. Updating an existing identity's email must not merge it with another account or transfer an invitation.

For new admission in this beta, accept Gmail addresses or Workspace identities where Google asserts a hosted domain. Other Google accounts using third-party email addresses are denied rather than assuming an old verified-email assertion proves current mailbox ownership. Supporting their admission later requires a separate mailbox-verification flow. An already admitted, enabled identity continues to be recognized by its stable subject, not re-admitted based on its current email.

Flow state, nonce and browser tokens contain at least 256 random bits. Store their hashes. Store an authenticated-encrypted PKCE verifier in the short-lived flow record; the encryption key is environment-specific and not in the database. The consuming request retains the verifier only in memory for its exchange, and deletes secret-bearing flow data from storage. Expired flows are purged. Never persist Google access, refresh or ID tokens for identity login, and never expose them to the browser. An unexpected refresh token is discarded, not adopted as a Gmail connection.

Use maintained libraries for token verification and cryptography, not handwritten JWT or encryption code. Exact compatible versions and locked dependencies belong in the implementation plan.

## 4. Invitations and the 50-user cap

An operator creates an invitation for an exact email using a database-backed management command. No email delivery integration is needed: the operator shares the ordinary login URL. Google proof of identity, not knowledge of that URL, redeems the invitation.

Normalize by trimming and lowercasing; do not collapse Gmail dots, plus tags, aliases or different domains. An invitation is single-use, expires after seven days, and may be revoked before redemption. Return a generic admission-denied response for uninvited, expired and revoked cases.

A singleton admission lock serializes invitation reservation and redemption. At most 50 seats exist per environment. Unexpired outstanding invitations reserve seats; expired or revoked unredeemed invitations release them. Redeemed identities retain their seat even if disabled. Reusing a redeemed seat or raising the cap requires a separately reviewed operator change; it is not an automatic consequence of deletion or expiry.

Admission commits owner creation, unique provider identity, seat redemption and session creation atomically. Two callbacks for the same identity must return one account; two people racing for the final seat cannot create a 51st account. Existing admitted identities may sign in after the original invitation expires, unless their account is disabled.

Use additive migrations after the existing migration set. Never edit already-applied migrations. Proposed persisted records are an admission guard, invitations, identities, login flows and sessions. Each identity links to the existing `owners` table using an opaque generated owner ID, not an email. Existing synthetic owners are not automatically granted identities.

## 5. Sessions, revocation and request authorization

Use an opaque browser session token with at least 256 random bits, with only its hash stored in PostgreSQL. Do not reuse the legacy signed Flask session or accept owner IDs from cookies, query strings, headers or JSON bodies. Identity tokens are not API bearer tokens.

Cookie: `__Host-athena-session`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, no Domain. Pre-login cookies use the same host-only protections. Production rejects insecure origin configuration; HTTP allowances exist only in an explicitly test-only factory, never via a production environment toggle.

Proposed beta defaults are a seven-day absolute session lifetime, a 24-hour idle limit and at most five active sessions per identity. Issuing a sixth revokes the oldest. All timing is evaluated server-side. Each session records the current runtime recovery epoch. Session creation always replaces any prior cookie for that browser, preventing session fixation.

Every authenticated request checks session expiry/revocation, identity and owner enablement, and epoch equality. Sessions survive normal web replica restarts; a recovery epoch change invalidates them. An unavailable database means a closed gate, not a fallback identity.

All state-changing browser routes require a session-bound CSRF token plus an exact-origin check. A missing Origin may use a same-origin Referer; absent or mismatched evidence is rejected. JSON routes also enforce JSON content type. Logout is POST, not GET. The Google callback is the narrow exception to normal CSRF checks and instead requires its one-use state/browser/nonce checks. Do not enable credentialed cross-origin API access.

Provide current-session logout, logout-all and operator account-disable commands. Disabling an account revokes its sessions and prevents future sign-in and new work. Already accepted jobs remain subject to the existing owner/lease/effect checks; revocation cannot undo an external action that already occurred. Revocation is not account-data deletion.

Authorization must be checked again within the transaction for protected mutations, coordinated with session revocation and owner disablement. Do not rely solely on an `Actor` resolved earlier in a request. Use a consistent runtime/owner/session/resource lock order so a racing mutation either commits before revocation or is denied after it. Keep provider network calls outside these locks. Read requests authorized before revocation may finish; no request beginning after committed revocation may read account data.

The existing `Actor(owner_id)` remains the internal durable-service identity, not public authority. Test-only principal injection remains explicitly gated by `testing=True`; production cannot select a fake principal or import test fixtures.

## 6. Cloud pages and API contract

Reuse Athena typography, layout and styles without branding changes. Use cloud-specific templates/controller where needed to avoid legacy handlers. Render user names, todo fields and replies as escaped text; rich HTML/Markdown rendering is not part of this slice. Do not fetch profile images from arbitrary provider claims.

Routes:

| Surface | Behavior |
| --- | --- |
| `/login` and OAuth routes | Identity flow above; no Gmail authorization |
| `/`, `/chat`, `/settings` | Authenticated cloud pages; login redirect for unauthenticated HTML |
| `POST /logout` | Revoke current session and clear cookies |
| `POST /api/auth/logout-all` | Revoke all sessions for the current identity |
| `GET /api/cloud/bootstrap` | Owner profile, capabilities, bounded todo page and existing primary conversation ID |
| `POST /api/cloud/conversations/ensure` | Idempotently obtain/create primary chat or a conversation for an owned todo |
| Existing `/api/work/...` | Durable receipt/status/cancel/reset contracts, now with real identity and CSRF protection |
| `/live`, `/ready` | Minimal process and web-dependency health, respectively |

GET requests never create conversations or accept jobs. The ensure operation uses an owner-scoped binding with uniqueness for the primary chat and each todo, so concurrent tabs do not create different default threads. Existing conversation IDs and generation/reset semantics remain intact. Reuse the durable services rather than duplicating acceptance, cancellation or reconciliation logic.

List todos from PostgreSQL for the authenticated owner only, in pages of 50 with deterministic `(created_at, id)` ordering and an opaque validated continuation cursor. A todo detail/conversation lookup always verifies ownership. This slice supports viewing stored todos and their durable conversations; manual todo creation/editing, action suggestions and local context fetches remain unavailable until their cloud contracts are implemented.

Capabilities are server-computed, not browser feature flags that grant access. Gmail shows “not available yet”; no connect/toggle button is active. Other sources, executor selection, push, uploads and device controls are absent. The production composition reports chat execution unavailable until the real executor package is integrated, and rejects submissions rather than collecting an unserviceable backlog. Existing accepted-job status remains readable during an ordinary processing pause. Synthetic acceptance/replies are available only in test composition.

The controller attaches CSRF tokens to mutations, handles expired sessions without resubmitting automatically, and preserves durable request keys when retrying an acknowledged/ambiguous same-user submission. Key any transient pending-message state by an opaque session/account context, never just the global `chat` label. Clear account content and drafts on logout, session change and a 401. Do not carry another account's draft into a new session. On a back/forward-cache restoration, hide account data until session revalidation succeeds.

Cloud pages do not register a service worker or cache account content. Send `Cache-Control: no-store` on identity, authenticated HTML and API responses. Use a dedicated cloud origin rather than serving production on an origin previously controlled by the local app's service worker. Existing local PWA behavior remains unchanged.

## 7. Configuration, errors and operational boundary

Require explicit cloud database configuration, environment identity, HTTPS public origin, Google client ID/secret and flow-encryption key. No `.env` auto-discovery, random secret fallback, SQLite fallback or implicit database migrations on web startup. Worker credentials and model keys do not belong in this web configuration.

Start production-capable web composition only with validated configuration; readiness checks required schema availability and the identity/session store. `/live` does not query Google or the database. `/ready` is explicitly web readiness, not proof that Gmail/Hermes works. A separate capabilities response exposes execution availability. Runtime recovery holds block login and authenticated data serving; an ordinary work-disabled pause may leave the identity and read-only UI available. Detect a recovery hold from persisted recovery state, not simply `runtime.enabled`.

Use bounded, PostgreSQL-shared login rate limits, not per-process counters: at most ten sign-in starts per pre-login browser per ten minutes and 100 starts globally per minute. Return 429 with Retry-After; expire limiter records. Do not trust forwarded client IPs for admission/security. Request bodies remain bounded by cloud defaults; OAuth parameter lengths and token responses also have explicit bounds in the implementation plan.

Enforce the configured host, HTTPS at trusted ingress, no sniffing, no framing, a same-origin referrer policy and a script CSP without `unsafe-inline` or `unsafe-eval`. OAuth and login responses use the stricter `no-referrer` policy; login POSTs must therefore supply a valid Origin. Cloud bootstrap data uses safe JSON serialization; use external scripts or per-response nonces. Audit required CSS/CDN dependencies and self-host the cloud page assets. Do not relax the local app's configuration globally to achieve cloud compatibility.

Map missing/expired sessions to API 401, invalid CSRF to 403, non-owned resources to 404, capacity/rate limits to 429, and unavailable dependencies to 503. OAuth denial/exchange errors receive a clean restart-sign-in page without raw provider exceptions. Use finite upstream timeouts; do not hold database connections while waiting for Google. DB or provider failure must not consume a beta seat unless admission committed.

Log bounded event names, request IDs, opaque owner IDs where appropriate, outcomes and durations. Never log cookies, authorization codes, ID/access tokens, PKCE/state/CSRF secrets, callback query strings, DSNs, raw provider bodies or mailbox/message content. Configure web-server access logging accordingly, not just application logging. Admission failures must not expose invite email addresses publicly.

A backup restore remains isolated with ingress and execution off until the existing recovery process changes the epoch and checks revocations/deletions. Login-flow records also carry an epoch and cannot mint a new session from a pre-restore flow. Session invalidation alone does not solve resurrected identities; reconciliation of deletions/revocations remains a deployment gate.

## 8. Verification and exit criteria

All implementation verification uses disposable, runner-owned PostgreSQL and synthetic identities. No personal database, mailbox or shared secrets. Do not infer results from previously passing PR #45 tests.

Required evidence:

1. Clean and existing-schema migrations; old durable rows remain intact; repeated migrations are safe.
2. Provider adapter tests with synthetic signed tokens and stubbed network transport: wrong signature/issuer/audience/authorized party/nonce, expiry, unverified email, missing claims, callback replay, browser mismatch, cancellation and ambiguous exchange failure. Do not merely stub the verifier to always return an identity.
3. Admission races: final seat, duplicate identity, expired/revoked invitation, email changes, distinct subjects sharing email, and third-party-email rejection. Show no cross-account merge and no 51st seat.
4. Session restart/replica tests, idle/absolute expiry, five-session cap, fixation prevention, logout-all, disabled owners, mutation-vs-revocation races, recovery epoch change and stale-flow rejection.
5. CSRF, spoofed Host/forwarded headers, unsafe redirects, fake-principal production refusal, malformed/oversized input and DB/provider outage tests. Verify no cookies or code/query values appear in captured logs.
6. Multi-owner API tests for bootstrap, todo pagination, conversation binding, snapshots, submission, cancel and reset. Tampered IDs/cursors never reveal another owner's data.
7. Browser walkthrough with a local synthetic Google adapter: invited sign-in, cloud chat receipt/status via a test worker, simultaneous tabs, restart, session expiry and account switching. Assert no local-only endpoint requests, no fabricated Gmail connection and no cross-account draft/content leakage, including back navigation.
8. Browser CSP and unsafe-content tests; no account response cached by the app; no new cloud service worker. Verify the shared local interface and existing durable/legacy suites separately.
9. Production composition without an executor clearly disables submission. Web readiness cannot be presented as successful live chat/Gmail or as a completed Azure restore drill.

Exit: a reviewed, test-backed cloud identity/web boundary with honest capabilities and documented operator admission/revocation procedures. No public release claim.

## 9. Follow-on work and approval gates

After approval of this written spec, prepare a code-level implementation plan with exact files, migrations, dependencies, lock ordering and tests. Use native implementation in this chat, consistent with the earlier preference, after plan review and execution-method confirmation. Keep an independent security/release review before actual launch.

Next packages remain secure Gmail consent/token storage and ingestion, isolated Hermes execution, then production composition, reproducible builds, CI and Azure deployment. Provider scopes/verification, privacy/deletion policy, rotated credentials, exact callback registrations, approved costs, live-provider tests, restore/rollback rehearsals and staged release are launch gates, not assumed complete here. WhatsApp, companion apps, computer use, forms, ordering and casting remain separate.

## 10. Sources and relationship to existing work

The durable contract is defined in `docs/superpowers/specs/2026-09-26-durable-work-design.md` and its merged implementation. The Azure rollout document in the local checkout supplies overall scope and cost gates; the earlier Railway design is not controlling.

Google's documentation informs the server flow, claim validation, exact callback registration and stable subject identity: [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect), checked 2026-09-26. This spec adds Athena-specific invitation, lifetime and recovery policies.

Flask's security guidance informs cookie settings, CSRF protections, escaped rendering and security headers: [Flask security considerations](https://flask.palletsprojects.com/en/stable/web-security/), checked 2026-09-26. The concrete session store, limit values and cloud/local separation here are design choices, not Flask defaults.
