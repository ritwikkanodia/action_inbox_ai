# Cloud deployment with a per-user Hermes executor

**Date:** 2026-09-25
**Status:** approved design, awaiting implementation plan

## Goal

Run Action Inbox on Railway for a first wave of test users, with as much of the
current feature set working as possible, and with a privacy posture we can
describe to testers in one paragraph and stand behind.

Today everything runs on one Mac and the only executor that works in a container
is the Agents SDK one, which can search the web and read Gmail. This design puts
Hermes itself in the container so cloud users get an agent that can browse the
web, send mail, and work in Calendar, Drive, Docs and Sheets, while making sure
one user's agent can never see another user's data or the app's own secrets.

### In scope (Project A)

- A Docker image that runs the poller, the web app, and Hermes.
- One isolated Hermes environment per user.
- The agent's Google and todo tools reaching the app over HTTP with a per-turn
  token instead of opening the database.
- The runner, executor readiness, prompt and Settings changes that follow.
- A deployment checklist and verify scripts.

### Out of scope (Project B, a later spec)

- Using the owner's Mac as a worker for turns that need local logins, local
  files or desktop apps, with a heartbeat, a run queue and a mid-turn handoff.
- Any way for cloud users to give the agent website logins. Cloud agents run
  logged out and say so when a site needs a login.
- Google OAuth verification. The Google Cloud app stays in Testing mode; each
  tester's address is added by hand and their Gmail grant expires every 7 days.
- WhatsApp beyond Meta's free test number (5 recipients).

## What testers can be told

Their data lives in one database on the server's disk. Their agent runs as its
own operating-system user in an environment only it can read, so it cannot open
the database or another user's sessions, cookies or memory. The agent never
holds their Google refresh token; it is given a one-hour access token for the
duration of a turn. The agent is signed in to nothing, so it cannot act on any
website account of theirs. Email content and the agent's reasoning are sent to
OpenAI's API to run the models. The agent acts on the content of their email
without a human approving each step; the prompts forbid irreversible actions on
guessed inputs, and isolation bounds what a misled agent can reach.

## Architecture

```
Railway service (one container)
├── gunicorn app:app  (root)         ── web UI, OAuth, WhatsApp webhook, /internal/*
├── python main.py    (root)         ── pollers, digest, push
└── per turn: hermes chat ... (OS user aib-<n>, HERMES_HOME=/data/hermes/<n>)
    └── spawns: python -m agent.google_mcp  (same OS user)
        └── HTTP to http://127.0.0.1:$PORT/internal/*  with a run token

/data (volume)
├── gmail_events.db        root, 0600
└── hermes/<n>/            aib-<n>, 0700  — config.yaml, state.db, memory, browser profile
```

The two app processes run as root, which is normal inside a single-service
container and is what lets them create OS users and spawn processes as them.
Everything the agent touches runs as an unprivileged per-user account.

## Section 1: image and runtime

`Dockerfile` at the repo root, built by Railway (a Dockerfile takes precedence
over Nixpacks; `railway.json` keeps its start command).

- Base: a Debian-based Python 3.12 image.
- Hermes installed with its official installer, pinned to the version on the
  Mac today (v0.21.4), under a shared, world-readable prefix outside any user's
  home, with Playwright's Chromium and its system dependencies installed to a
  world-readable `PLAYWRIGHT_BROWSERS_PATH`. Per-user processes must be able to
  execute the install tree; nothing in it is secret.
- The repo copied to `/app`, its requirements installed into the system Python.
  `HERMES_BIN` points at the installed launcher.
- `HERMES_CLOUD=1` and `HERMES_HOMES_DIR=/data/hermes` set in the image, so
  the cloud code paths select themselves. `DB_PATH=/data/gmail_events.db`.
- Start command unchanged in shape: `python main.py & exec gunicorn --bind
  0.0.0.0:$PORT --workers 1 --threads 8 app:app`. One worker because the run
  registry is per process; threads so `/run` polls are not queued behind a
  slow request.
- On startup `init_db` also ensures `/data/gmail_events.db` is mode 0600 and
  `/data/hermes` exists, mode 0711 (traversable, not listable).

## Section 2: per-user isolation

New module `agent/cloud_users.py`, used only when `HERMES_CLOUD=1`.

- Table `cloud_users(user_id TEXT PRIMARY KEY, uid INTEGER UNIQUE NOT NULL,
  created_at TEXT)`. `uid` is `20000 + rowid`.
- `ensure(user_id) -> CloudUser(uid, home)`: on first call for a user, insert
  the row, run `useradd --uid <uid> --no-create-home --home-dir <home>
  --shell /usr/sbin/nologin aib-<uid>`, create `<home>` owned by that uid, mode
  0700. Idempotent: a row that exists and a home that exists are left alone; a
  missing `useradd` entry for an existing row is recreated (a redeploy loses
  `/etc/passwd`, not the volume).
- `write_config(cloud_user, template_vars)`: renders `<home>/config.yaml` from
  a template in the repo on **every** turn, so a config change ships with a
  deploy. Hermes' own defaults fill everything not set. The template sets:
  - `model`: provider `openai-api`, default model from `HERMES_CLOUD_MODEL`
    (default `gpt-6-astra`). The key comes from `OPENAI_API_KEY` in the
    process environment; Hermes reads it itself.
  - `platform_toolsets.cli: [browser, web, memory, todo]`. No `terminal`, no
    `file`, no `skills`, no `cronjob`. Memory is per home, so per user.
  - `browser`: headless, `use_real_profile: false`.
  - `mcp_servers.action_inbox_google`: command `python -m agent.google_mcp`,
    cwd `/app`, env `AIB_USER_ID`, `AIB_ACCOUNT_ID`, `AIB_TODO_ID`,
    `AIB_API_URL`, `AIB_RUN_TOKEN` as `${VAR}` references, exactly as the
    install script writes them locally.
- The Hermes install and the repo are readable by every uid; the home is
  readable only by its owner; the database only by root. A headless browser
  opening `file:///data/hermes/<other>/...` or `file:///data/gmail_events.db`
  gets permission denied from the kernel.

## Section 3: run tokens and the internal API

The MCP server gains an HTTP mode. `binding_from_env` returns a binding with
either `db_path` (today) or `api_url` + `run_token`; `AIB_API_URL` non-empty
selects HTTP mode. The local Mac and the Agents SDK keep the SQLite mode
unchanged.

### Tokens

- Table `run_tokens(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  todo_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)`.
- `db.mint_run_token(conn, user_id, todo_id, ttl_seconds)` returns the plain
  token (32 random bytes, urlsafe) and stores its SHA-256. TTL is
  `HERMES_TIMEOUT_SECONDS + 60`.
- `db.resolve_run_token(conn, token) -> (user_id, todo_id) | None` also
  rejects expired rows. `db.revoke_run_token(conn, token)` is called in the
  runner's `finally`. Minting sweeps expired rows.

### Routes

All under `/internal/`, exempt from `login_required`, authenticated only by
`Authorization: Bearer <token>`; 401 on a missing, unknown or expired token.
Every query is scoped to the token's `user_id`, so a valid token cannot name
another user.

| Route | Backs |
|---|---|
| `GET /internal/accounts` | the `google_accounts` tool: connected Gmail addresses and their granted services |
| `GET /internal/credentials?account=<addr>` | the credentials provider: `{token, expiry, scopes, account}` |
| `GET /internal/todos?status=&limit=` | `todos_list` |
| `GET /internal/todos/<id>` | `todos_get` |
| `POST /internal/todos` | `todos_create` (`source='user'`) |
| `PATCH /internal/todos/<id>` | `todos_update`, same `TODO_EDITABLE_FIELDS` and enum checks as the user-facing PATCH |

The credentials route refreshes server-side when the stored token expires
within 15 minutes, using the existing refresh path (which also clears the
connection on `RefreshError`, as today), and returns only the access token. The
refresh token and the Google client secret never leave the app process. There
is no write-back route because the agent's process never refreshes.

### Backends

- `agent/google_mcp/services.py`: the existing `creds_provider` seam gets an
  HTTP implementation that builds a `google.oauth2.credentials.Credentials`
  from the access token alone (no refresh handler), plus an HTTP account
  lister. Scope gating stays where it is, driven by the `scopes` the route
  returns.
- `agent/todo_tools.py`: `build_todo_tools` takes a backend object with
  `list/get/create/update` instead of a `db_path`. `SqliteBackend` is the
  current code moved behind that interface; `HttpBackend` calls the routes.
  The Agents SDK and the local install script keep passing SQLite.
- Both HTTP clients are `urllib` on the loopback URL, a 10-second timeout,
  and every non-2xx becomes the existing `Error:` line.

## Section 4: runner changes

`agent/hermes_runner.py`, branching on `HERMES_CLOUD`:

- **Per-user process.** `cloud_users.ensure(user_id)`, `write_config`, then
  `Popen(..., user=uid, group=uid, cwd=home)`. The macOS-only steps (Chrome
  profile autoclose, persistent browser, headed flag) are skipped.
- **Environment allowlist.** Cloud subprocesses get a constructed environment,
  not `dict(os.environ)`: `PATH`, `HOME=<home>`, `HERMES_HOME=<home>`,
  `LANG`, `PLAYWRIGHT_BROWSERS_PATH`, `OPENAI_API_KEY`, and the five `AIB_*`
  variables. `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_SECRET`, `META_WA_*`,
  `RESEND_API_KEY`, `VAPID_PRIVATE_KEY` and everything else stay in the app.
  The one shared secret that does reach every user's process is the OpenAI
  key; it cannot read data, and a per-user key or proxy is a later change.
- **Token lifecycle.** Mint before spawn with the turn's `todo_id`; revoke in
  `finally`, including on cancel and timeout.
- **Concurrency.** A process-wide semaphore of `MAX_CLOUD_TURNS` (default 3)
  around the spawn. A waiting turn emits one progress event, "waiting for a
  free agent slot", so the trace shows why nothing is happening yet. The
  wait counts against the run's timeout.
- **Live trace.** `hermes_activity` takes the state database path as an
  argument; the cloud passes `<home>/state.db`. The local default is
  unchanged.
- **Sessions.** Session naming and `executor_state` are unchanged. Each user's
  sessions live in their own home, so names cannot collide across users.

`agent/hermes_prompt.py` gets a third login-handoff variant, selected when
`HERMES_CLOUD=1`: the agent is signed in to nothing and has no way to sign in;
when a site requires a login it should say which site and what it would have
done, close the turn, and never ask the user for a password.

## Section 5: executor, Settings, everything else

- `executor.readiness("hermes")` in cloud mode also requires `HERMES_HOMES_DIR`
  to exist and be writable by the app. Hermes is the default executor as
  today, so cloud users get it without touching Settings.
- The Hermes description in `EXECUTORS` is cloud-aware: "Browser, web and
  Google tools" instead of naming terminal and desktop tools.
- Pollers, digest, push, WhatsApp, chat, action options and clarifying
  questions need no change. `browser_history` and `system` stay out of
  `ENABLED_SOURCES`.
- The Agents SDK executor stays selectable for a user who wants it; nothing
  routes to it by default.

## Section 6: deployment checklist

1. Railway service from the repo, with a volume mounted at `/data`.
2. Environment: everything in `.env.example` that is required, plus
   `BASE_URL`, `DB_PATH=/data/gmail_events.db`, `TODO_EXECUTOR=hermes`,
   `HERMES_CLOUD=1`, `HERMES_HOMES_DIR=/data/hermes`, `MAX_CLOUD_TURNS`,
   `HERMES_CLOUD_MODEL`, VAPID keys from `scripts/gen_vapid_keys.py`, the
   Resend key and verified sender, the Meta WhatsApp block with a permanent
   System User token and the webhook at `<BASE_URL>/whatsapp/webhook`.
3. Google Cloud: add `<BASE_URL>/auth/callback` and the Gmail callback to the
   authorised redirect URIs; add each tester's address to the OAuth consent
   screen's test users.
4. Smoke test on the deployed URL: sign in, connect Gmail, wait for a todo,
   open it, click a suggested action, watch the trace, confirm the reply.
   Then a second Google account as a second user, confirm it cannot see the
   first user's todos, and that a chat turn asking the agent to read
   `/data/gmail_events.db` fails.

## Verification

Scripts under `scripts/verify/`, plain `python`, no network, no spend, in the
style of the existing ones:

- `verify_run_tokens.py` — mint, resolve, expiry, revoke, sweep.
- `verify_internal_api.py` — Flask test client: 401 without a token, scoping
  to the token's user, each todo route mirrors the user-facing one, the
  credentials route refreshes when near expiry and returns no refresh token.
- `verify_todo_tools.py` — extended: the same assertions against the HTTP
  backend talking to a stub server.
- `verify_google_mcp.py` — extended: HTTP credentials provider and account
  lister against a stub.
- `verify_cloud_users.py` — home layout, config rendering, environment
  allowlist, with `useradd` and the uid switch stubbed.

Plus the manual smoke test in the deployment checklist.

## Risks and open items

- Hermes' installer layout under a shared prefix has not been exercised; the
  implementation plan's first task is a throwaway container build that runs
  `hermes chat -q` as a non-root uid with an MCP server attached.
- Memory: three concurrent turns is roughly 1.5 GB. Pick the Railway plan
  accordingly and lower `MAX_CLOUD_TURNS` if the container is killed.
- Turns interrupted by a deploy are lost, as they are today under the Flask
  reloader. Deploy when the run registry is idle, or accept the loss.
- Testing-mode Gmail grants expire weekly. The existing clear-and-reprompt on
  `RefreshError` handles it; testers will need to reconnect.
