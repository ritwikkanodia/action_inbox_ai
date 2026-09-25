# Cloud Hermes (multi-user) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Action Inbox on Railway with a per-user, OS-isolated Hermes executor whose tools reach the app over HTTP with a per-turn token.

**Architecture:** One container runs the poller, gunicorn and, per turn, `hermes chat` as an unprivileged OS user with its own `HERMES_HOME` on the volume. The Google/todo MCP server gains an HTTP mode (`AIB_API_URL` + `AIB_RUN_TOKEN`) that calls new `/internal/*` routes instead of opening SQLite. The runner mints and revokes the token, builds an allowlisted environment, and caps concurrency.

**Tech Stack:** Python 3.12, Flask, SQLite, PyYAML, urllib, Hermes Agent CLI v0.21.4, Playwright Chromium, Railway + Dockerfile.

**Spec:** `docs/superpowers/specs/2026-09-25-cloud-hermes-multi-user-design.md`

## Global Constraints

- No test framework: every task ships or extends a `scripts/verify/verify_*.py` run with plain `python`, no network, no API spend, `PASS`/`FAIL` lines and `SystemExit(1)` on failure.
- Cloud code paths select on `HERMES_CLOUD=1`; with it unset every existing behaviour is unchanged (local Mac and Agents SDK keep SQLite mode).
- The agent's process never receives: `FLASK_SECRET_KEY`, `GOOGLE_CLIENT_SECRET`, `META_WA_*`, `RESEND_API_KEY`, `VAPID_PRIVATE_KEY`, a Google refresh token.
- Per-user home: `/data/hermes/<uid>`, mode 0700, owned by `aib-<uid>`, uid = `20000 + rowid`.
- Cloud toolsets: `browser, web, memory, todo`. Never `terminal`, `file`, `skills`, `cronjob`.
- Run token: 32 urlsafe random bytes, SHA-256 stored, TTL `HERMES_TIMEOUT_SECONDS + 60`.
- `MAX_CLOUD_TURNS` default 3. `HERMES_CLOUD_MODEL` default `gpt-6-astra`.
- Docker is not installed on the dev Mac; the image is verified by the first Railway deploy (Task 9).

---

### Task 1: run tokens and cloud user rows in `db.py`

**Files:**
- Modify: `db.py` (schema in `init_db` after `push_subscriptions`; helpers after `count_push_subscriptions`)
- Create: `scripts/verify/verify_run_tokens.py`

**Interfaces:**
- Produces: `mint_run_token(conn, user_id, todo_id, ttl_seconds) -> str`; `resolve_run_token(conn, token) -> dict | None` (`{user_id, todo_id}`); `revoke_run_token(conn, token) -> None`; `ensure_cloud_user_row(conn, user_id) -> int` (uid).

- [ ] **Step 1: Write the failing verify script** — seeds two users; asserts: token is ≥40 urlsafe chars; plain token is not in `run_tokens`; resolves to `{user_id, todo_id}`; unknown and blank tokens resolve None; a token minted with ttl −1 resolves None; a chat token has `todo_id None`; revoke makes it None; minting sweeps expired rows; uids start at 20001, are distinct, and are stable on repeat.
- [ ] **Step 2: Run** `python scripts/verify/verify_run_tokens.py`, expect ImportError.
- [ ] **Step 3: Schema**

```sql
CREATE TABLE IF NOT EXISTS run_tokens (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, todo_id TEXT,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_users (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL UNIQUE,
    uid INTEGER NOT NULL UNIQUE, created_at TEXT NOT NULL);
```

- [ ] **Step 4: Helpers** — `_hash_token` (sha256 hex), `_iso_in(seconds)` producing the same ISO shape `_now()` uses; `mint_run_token` deletes expired rows, inserts the hash with `secrets.token_urlsafe(32)`; `resolve_run_token` selects where `token_hash = ? AND expires_at >= now`; `revoke_run_token` deletes; `ensure_cloud_user_row` returns the existing uid or inserts with `uid = 20000 + seq`.
- [ ] **Step 5: Run** the script and `verify_migration.py`, expect PASS.
- [ ] **Step 6: Commit** `feat(db): run tokens and cloud user rows`.

---

### Task 2: internal HTTP client + todo tools backend seam

**Files:**
- Create: `agent/internal_client.py`
- Modify: `agent/todo_tools.py` (backends; `build_todo_tools(backend_or_db_path, user_id, current_todo_id)`)
- Modify: `scripts/verify/verify_todo_tools.py` (HTTP-backend section against a stub `http.server`)

**Interfaces:**
- `InternalClient(api_url, token, timeout=10)`: `.get(path, params=None)`, `.post(path, body)`, `.patch(path, body)`; raises `InternalApiError(status, message)` (status 0 when unreachable).
- `SqliteTodoBackend(db_path, user_id)`, `HttpTodoBackend(client)`: `list_todos() -> list[dict]`, `get_todo(id) -> dict | None`, `create_todo(title, importance, due_date, suggested_action) -> dict`, `update_todo(id, updates) -> dict | None` (raises `ValueError` on a bad enum).
- `_safe` maps `InternalApiError` to `Error: internal API <status>: <message>`.

- [ ] **Step 1:** Add the stub-server section to `verify_todo_tools.py`: the stub serves the four todo routes on top of the temp SQLite through `SqliteTodoBackend`, checks `Authorization: Bearer tok`, answers 401/404/400 (`{"error"}`) like the real routes; rerun the tool assertions through `HttpTodoBackend`; assert a wrong token yields an `Error:` line containing `401`.
- [ ] **Step 2: Run**, expect ImportError.
- [ ] **Step 3:** Write `internal_client.py` (urllib, JSON, bearer header, error mapping).
- [ ] **Step 4:** Add both backends to `todo_tools.py`; `build_todo_tools` wraps a `str` in `SqliteTodoBackend`; closures call the backend; `todos_update` turns `ValueError` into `ToolError`.
- [ ] **Step 5: Run** `verify_todo_tools.py`, expect PASS for both sections.
- [ ] **Step 6: Commit** `feat(agent): todo tools behind a backend seam, with an HTTP backend`.

---

### Task 3: `/internal/*` routes

**Files:**
- Create: `internal_api.py` (`create_blueprint(get_db) -> Blueprint`)
- Modify: `app.py` (register the blueprint after `get_db` is defined)
- Modify: `pollers/gmail/auth.py` (`get_google_credentials(..., min_valid_seconds=0)`; extract `_refresh_and_store`)
- Create: `scripts/verify/verify_internal_api.py`

**Interfaces:**
- `GET /internal/accounts` → `{"accounts": [addr…]}`
- `GET /internal/credentials?account=` → `{"account","token","expiry","scopes"}`; 409 `{"error"}` on `RuntimeError`
- `GET /internal/todos?status=all|open|ongoing|closed&limit=` → list; `GET /internal/todos/<id>` → dict or 404; `POST /internal/todos` → 201 dict (400 on bad title/importance); `PATCH /internal/todos/<id>` → dict, 400 on bad enum, 404 unknown.
- Every route: 401 `{"error":"unauthorized"}` without a valid bearer; scoped to the token's `user_id`.

- [ ] **Step 1:** Write `verify_internal_api.py` with `app.test_client()`: 401 without/with bogus token; accounts lists only u1's; credentials returns `token`, no `refresh_token`, and refreshes when expiry is within 15 min (monkeypatch `Credentials.refresh` to set `token="fresh"`); todo routes mirror the UI routes and never show u2's rows.
- [ ] **Step 2: Run**, expect ImportError on `internal_api`.
- [ ] **Step 3:** Add `min_valid_seconds` to `get_google_credentials`; refresh and persist when `expiry - utcnow < min_valid_seconds`.
- [ ] **Step 4:** Write `internal_api.py`; register in `app.py`.
- [ ] **Step 5: Run** `verify_internal_api.py`, `verify_web.py`, `verify_google_scopes.py`, expect PASS.
- [ ] **Step 6: Commit** `feat(web): /internal routes for the agent's tools, bearer-token scoped`.

---

### Task 4: MCP server HTTP mode

**Files:**
- Modify: `agent/google_mcp/services.py` (`Binding` gains `api_url`, `run_token`, `.http`; `binding_from_env` reads `AIB_API_URL`, `AIB_RUN_TOKEN`; `http_creds_provider(client)`, `http_account_lister(client)`; `_load` opens no SQLite in HTTP mode)
- Modify: `agent/google_mcp/__main__.py` (todo backend = `HttpTodoBackend` in HTTP mode)
- Modify: `scripts/verify/verify_google_mcp.py` (HTTP section)

- [ ] **Step 1:** Extend the verify script with a stub serving `/internal/accounts` and `/internal/credentials`; assert `.http`, account resolution over HTTP, scope gating from the returned scopes, a 401 becomes an `Error:` result, `make_server` registers google and todo tools without touching `db_path`.
- [ ] **Step 2: Run**, expect failure.
- [ ] **Step 3:** Implement; HTTP creds build `google.oauth2.credentials.Credentials(token=...)` with no refresh handler.
- [ ] **Step 4: Run** `verify_google_mcp.py`, `verify_todo_tools.py`, expect PASS.
- [ ] **Step 5: Commit** `feat(google_mcp): HTTP mode — tools reach the app with a run token`.

---

### Task 5: `agent/cloud_users.py`

**Files:**
- Create: `agent/cloud_users.py`
- Create: `scripts/verify/verify_cloud_users.py`

**Interfaces:**
- `is_cloud()`, `HOMES_DIR`, `MAX_TURNS`, `CLOUD_MODEL`, `ENV_ALLOWLIST = ("PATH","HOME","HERMES_HOME","LANG","PLAYWRIGHT_BROWSERS_PATH","OPENAI_API_KEY")`
- `CloudUser(uid, username, home)`; `ensure(conn, user_id, run=subprocess.run, homes_dir=None) -> CloudUser`; `render_config(user, python, repo) -> dict`; `write_config(user, python, repo) -> str`; `subprocess_env(user, binding) -> dict`.

- [ ] **Step 1:** Verify script with a temp homes dir, a recording fake `run`, `pwd.getpwnam` patched: `useradd` called once with the exact argv; home mode 0700; second `ensure` runs nothing; config dict matches the spec's Section 2; `write_config` writes YAML at `<home>/config.yaml`; `subprocess_env` drops every app secret and sets `HOME == HERMES_HOME == home`.
- [ ] **Step 2: Run**, expect ImportError.
- [ ] **Step 3:** Implement (chown/chmod only when `os.geteuid() == 0`, else skipped — the verify runs unprivileged).
- [ ] **Step 4: Run**, PASS. **Commit** `feat(agent): cloud_users — per-user OS user, home, config and env`.

---

### Task 6: runner and activity watcher cloud branch

**Files:**
- Modify: `agent/hermes_activity.py` (`ActivityWatcher(session_name, progress, state_db=None)`)
- Modify: `agent/hermes_runner.py` (`_run(..., user_id)`, cloud branch, semaphore, token lifecycle, `INTERNAL_URL`, `REPO_ROOT`)
- Modify: `scripts/verify/verify_executor.py` (cloud section with a stub `hermes` that dumps env+cwd, `cloud_users.ensure` patched to the current uid)

- [ ] **Step 1:** Extend the verify script: under `HERMES_CLOUD=1` the stub's env has `AIB_RUN_TOKEN`, `AIB_API_URL`, no `FLASK_SECRET_KEY`, no `AGENT_BROWSER_HEADED`; cwd is the home; `config.yaml` exists; the token is revoked after the turn; `chrome_profile.close_for_run` not called.
- [ ] **Step 2: Run**, expect failures.
- [ ] **Step 3:** Implement per spec Section 4.
- [ ] **Step 4: Run** `verify_executor.py`, `verify_hermes_activity.py`, PASS. **Commit** `feat(hermes): cloud turns run as a per-user OS user with a run token`.

---

### Task 7: prompt, executor readiness, Settings copy

**Files:**
- Modify: `agent/hermes_prompt.py` (`_LOGIN_HANDOFF_CLOUD`, `_LOGIN_HANDOFF_FOLLOWUP_CLOUD`, selection on `HERMES_CLOUD`)
- Modify: `agent/executor.py` (readiness checks `HOMES_DIR` writable in cloud mode; cloud-aware Hermes description)
- Create: `scripts/verify/verify_cloud_prompt.py`

- [ ] **Step 1:** Verify: with `HERMES_CLOUD=1`, both prompt templates contain "signed in to nothing" and neither contains "AppleScript"; readiness fails with a message naming `HERMES_HOMES_DIR` when the dir is missing and passes when present; `describe_executors()` shows the cloud description.
- [ ] **Step 2–4:** Implement, run `verify_cloud_prompt.py`, `verify_clarify.py`, `verify_web.py`; **commit** `feat: cloud login-handoff prompt, readiness and Settings copy`.

---

### Task 8: Dockerfile, start script, startup permissions, docs

**Files:**
- Create: `Dockerfile`, `scripts/cloud_start.sh`
- Modify: `railway.json`, `.env.example`, `CLAUDE.md`, `app.py` + `main.py` (`_ensure_db_parent_dir` chmod 0600)

- [ ] **Step 1:** Dockerfile per spec Section 1: `python:3.12-bookworm`; Hermes installed with `HOME=/opt/hermes` via the official installer pinned to `v0.21.4`, tree made `a+rX`; `playwright install --with-deps chromium` into `/opt/pw-browsers`; repo at `/app`; env `HERMES_CLOUD=1 HERMES_HOMES_DIR=/data/hermes DB_PATH=/data/gmail_events.db HERMES_BIN=/opt/hermes/.local/bin/hermes`.
- [ ] **Step 2:** `scripts/cloud_start.sh`: `mkdir -p /data/hermes; chmod 0711 /data/hermes; python main.py & exec gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120 app:app`. `railway.json` start command → `bash scripts/cloud_start.sh`.
- [ ] **Step 3:** chmod the DB 0600 after creation in both entrypoints (best effort).
- [ ] **Step 4:** Document the new env vars in `.env.example`; rewrite `CLAUDE.md`'s Deployment section from spec Section 6 plus the isolation model.
- [ ] **Step 5:** Run every `scripts/verify/*.py`; **commit** `feat(deploy): Dockerfile and start script for the multi-user cloud`.

---

### Task 9: deploy and smoke test (with the user)

- [ ] Push, PR, merge.
- [ ] Railway: volume at `/data`, env per spec Section 6, deploy, read build logs for the Hermes install step.
- [ ] Google Cloud: redirect URIs `<BASE_URL>/oauth/login/callback` and `<BASE_URL>/oauth/gmail/callback`; test users.
- [ ] Smoke: sign in, connect Gmail, todo appears, open, click action, trace shows tool calls, reply lands. Second user cannot see the first's todos; a chat asking the agent to read `/data/gmail_events.db` fails.
