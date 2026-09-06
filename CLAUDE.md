# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
source venv/bin/activate

# Poller — Gmail + Fathom + morning digest, loops forever
python main.py

# Web UI — separate terminal
flask --app app run --debug --port 5001
```

Config lives entirely in `.env` (loaded with `load_dotenv(override=True)` at the top of both
entrypoints — before any other import, since module-level code reads env vars). See
`.env.example` for the annotated list. `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`OPENAI_API_KEY`, and `FLASK_SECRET_KEY` are required; everything else has a default.

There is no test suite, linter config, or CI in this repo. Verify changes by running the
two processes and exercising the UI. The exception is `scripts/verify/`, a handful of
standalone assertion scripts for the `db.py` schema/helpers and the web surface — run
them with plain `python` (no pytest):

```bash
python scripts/verify/verify_migration.py    # optionally: <path-to-db-copy>
python scripts/verify/verify_connections.py
python scripts/verify/verify_links.py
python scripts/verify/verify_cursors.py
python scripts/verify/verify_web.py
```

## Architecture

Two runtimes share one SQLite database (`DB_PATH`, default `gmail_events.db`):

**Poller (`main.py`)** — a `while True` loop, 30s per cycle. For each user with at least one
connected source (`list_active_users`), it runs each enabled source in its own `try/except` so
one failing source never kills the cycle. Sources are gated by `ENABLED_SOURCES`; the default
set is `gmail,fathom,morning_digest`. `browser_history` and `system` are macOS-only and opt-in.
The morning digest is the exception to the per-source loop: it iterates `list_all_users`, not
just connected ones, so unconnected users get a "connect Gmail" nudge instead of nothing.

**Web UI (`app.py`)** — Flask, multi-user, every route behind `@login_required` except
`/login`, the OAuth callbacks, `/digest/preview`, `/stats`, and the PWA routes
(`/manifest.webmanifest`, `/sw.js`, `/offline`) — Chrome fetches those before a session
exists, and a redirect to `/login` would make the app non-installable. Key routes:

- `GET /` — todos for the current user, ordered closed-last, then importance, then recency
- `POST /todos` — user-entered todo (`source='user'`)
- `PATCH /todos/<id>` — `due_date`, `importance`, `status`, `decision`
- `POST /todos/<id>/ask-ai` — one agent turn; posting with no message returns the existing
  thread without an LLM call
- `GET /todos/<id>/context` — source context (e.g. the Gmail thread) for the detail pane
- `POST /todos/<id>/reset-thread` — clears `ai_thread`
- `GET /settings`, `POST /settings/sources/<source>`, `/settings/sources/gmail/auth` — source connections
- `GET /digest/preview?user_id=…[&format=json]` — renders a user's digest without sending it

## Auth and per-user credentials

Sign-in-with-Google lives in `auth.py`; Gmail *data* access is a separate OAuth grant in
`pollers/gmail/auth.py`. Both use the same `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` Google
Cloud project. There is no `credentials.json` or `token.json` — every credential is per user
*and per account*, stored as JSON in the `source_connections` table, keyed by
`(user_id, source, account_id)` (`auth_type` is `oauth2` or `api_key`).
`account_id` is the lowercased Gmail address for Gmail and `''` for
single-connection sources. A user can connect several Gmail accounts; each has
its own credentials, its own poll cursor, and its own place in the poll loop,
so one revoked token never disturbs the others. Fathom is an API key the user
pastes into Settings; there is no global fallback key.

Redirect URIs are computed per request from the browser's own hostname to avoid PKCE/state
mismatches between `localhost` and `127.0.0.1`. `ProxyFix` is applied so this works behind a
proxy in deployment.

When Google returns a `RefreshError` (Testing-mode 7-day token expiry, revoked access, password
change), `get_gmail_service` clears the stored connection **for that account** so the UI
flips to "not connected" and prompts re-auth, leaving the user's other accounts polling.
Don't swallow that — the clear-and-reprompt is the intended behavior.

## Data model (`db.py`)

- `users` — one row per Google sign-in
- `user_state` — per-user key/value: Gmail cursors (`gmail:<email>:history_id`,
  `gmail:<email>:backfill_pending`, `gmail:<email>:backfilled` — one set per connected
  account), `fathom_last_polled_at`, digest bookkeeping. (`state` is the legacy
  single-user table, migrated away from.)
- `source_connections` — per-user, per-source credentials
- `events` — raw `GmailEvent` payloads as JSON, append-only
- `todos` — the unified list. `source` ∈ `gmail|fathom|browser_history|system|user`.
  `account_id` records which Gmail account a todo came from; `NULL` means unknown
  (todos predating multi-account support).
- `page_views` — lightweight analytics behind `/stats`

**Dedup** is a unique partial index on `(user_id, source, dedup_key)` where `dedup_key IS NOT NULL`,
and the `save_*_todo` helpers use `INSERT OR IGNORE`. Re-polling the same message or meeting is
always safe. Any new source must set a stable `dedup_key`.

**`importance` is not urgency.** `importance` (`low|medium|high`) is how much the outcome
matters, independent of timing; urgency is derived from `due_date` at read time. The column was
renamed from `urgency`, so treat old references as stale.

**Migrations** run inside `init_db` as idempotent `ALTER TABLE` steps guarded by `PRAGMA
table_info` checks. Fresh databases get the full schema with `CHECK` constraints; migrated ones
can't gain constraints without a table rebuild, so the enums are *also* enforced in Python in
the `save_*` helpers. Keep both in sync when adding an enum value.

## LLM usage

Every OpenAI call in the repo uses **`gpt-5.4-mini`** — `pollers/gmail/todo_generator.py`,
`pollers/browser/generator.py`, `pollers/system/generator.py`, and `agent/resolver.py`. The
three generators use Chat Completions with `response_format={"type": "json_object"}`.

**Todo generation** (`pollers/gmail/todo_generator.py`): thread context + sender → JSON with a
`should_generate_todo` boolean and a `reasoning` sentence. Returning `false` is a first-class
outcome, not an error — newsletters, receipts, OTPs, and sign-in requests should be skipped
there, and the poller logs the reasoning.

**The per-todo agent** (`agent/`) uses the **OpenAI Agents SDK** (`openai-agents`), not the raw
Responses API:

- `resolver.py` builds an `Agent` with `WebSearchTool` plus Gmail tools
  (`search_email_threads`, `fetch_email_thread`) and runs it via `Runner.run_sync`.
  `local_file_tools` (`agent/tools/local_files.py`, Spotlight-backed `search_local_files` /
  `read_local_file`, with GPT-vision OCR fallback for scanned PDFs) is always wired in but
  self-gates on `LOCAL_SEARCH_ROOT` — it returns no tools at all when that env var is unset.
  A `use_browser` tool (`agent/tools/browser/`, Playwright-driven, persistent Chromium profile
  at `~/.action_inbox_ai/chrome-profile`) is wired in only when `ENABLE_BROWSER_AGENT` is set —
  off by default since a real Chromium window isn't safe to assume in the deployed container.
  Run `python -m agent.tools.browser.login` once locally to log into any sites the agent needs.
- The whole thread is persisted as SDK input-list JSON in `todos.ai_thread` and round-tripped
  through `result.to_input_list()`. The SDK drops unknown top-level keys on that round trip,
  which is why the synthetic bootstrap turn is marked with a `HIDDEN_CONTEXT_SENTINEL` prefix
  *inside* the user content — `app._thread_for_client` filters it out before rendering. Don't
  move that marker to a sibling key.
- `prompt.py` instructs the agent to produce the finished artifact and to exhaust its tools
  before asking the user anything. Clarifying questions are treated as a failure mode.

## Gmail polling specifics

The poller iterates every connected Gmail account for a user, each in its own
`try/except`. On first connect of an account it does a **3-day backfill**
(`gmail:<email>:backfill_pending` user-state key), capturing that account's current
`historyId` *before* fetching so anything arriving mid-backfill is still picked up on
the next cycle — dedup absorbs the overlap. After that it's incremental: History
API with `startHistoryId`, paged to exhaustion, writing back the max `historyId` seen. A
`last_id` of `None` on a non-backfill path just bootstraps the baseline and returns nothing.

`SENT` and `DRAFT` messages are skipped (there's a TODO about handling them for follow-up
tracking). A 404 fetching a full message is swallowed — the message was deleted between the
history record and the fetch.

`spam_filter.py` drops events with no sender, with labels `SPAM`/`CATEGORY_PROMOTIONS`/
`CATEGORY_FORUMS`, from a hardcoded `NOREPLY_SENDERS` set, or from the digest's own
`RESEND_FROM` address — otherwise the app generates todos from its own digest emails.

## Morning digest (`pollers/digest/poller.py`)

Sends via Resend, gated on `SEND_HOUR_LOCAL = 9` in the user's timezone (`DIGEST_TIMEZONE`,
default `Asia/Kolkata`), deduped by a `digest_last_sent_date` user-state key so the 30s loop
sends at most once a day. Items with a deadline inside `URGENT_WINDOW_HOURS` (72) lead;
suggestions age out after `AGEOUT_DAYS` (7) and are capped at `SUGGESTION_LIMIT` (15). Users
with no connected source get a connect prompt instead, at most `CONNECT_PROMPT_MAX` (3) times.

Use `/digest/preview` to iterate on the template — it renders without sending.

## Adding a source

1. New package under `pollers/`, exposing `poll(conn, user_id) -> int` (count saved).
2. Add the name to `KNOWN_SOURCES` in `main.py`, and to `DEFAULT_ENABLED_SOURCES` only if it's
   cross-platform and safe on by default.
3. Add a `save_<source>_todo` helper in `db.py` with a stable `dedup_key`, and extend the
   `source` CHECK constraint *and* the Python-side enum validation.
4. Wire it into the per-user block in `main()` inside its own `try/except`.

## Deployment

`railway.json` runs both runtimes in one container:
`python main.py & exec gunicorn --bind 0.0.0.0:${PORT:-8000} app:app`. Set `BASE_URL` in
production — it's used for OAuth redirect URIs and digest links, and `OAUTHLIB_INSECURE_TRANSPORT`
is only enabled when it points at localhost.
