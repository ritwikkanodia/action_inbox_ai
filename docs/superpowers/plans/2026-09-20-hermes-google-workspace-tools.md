# Hermes Google Workspace Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Hermes executor Gmail, Drive, Docs, Sheets, Calendar and Contacts tools backed by the app's own per-user Google OAuth grant, so the agent reaches Google over the API instead of the browser.

**Architecture:** The OAuth grant widens from `gmail.readonly` to a Workspace scope set defined once in `google_scopes.py`. A stdio MCP server in `agent/google_mcp/` reads credentials from `source_connections` in the app's SQLite database; it is registered once in `~/.hermes/config.yaml` and bound per run by three `AIB_*` environment variables the Hermes runner sets on the subprocess. Without a binding the server serves zero tools, so other Hermes use is untouched.

**Tech Stack:** Python 3.14, Flask, `google-auth` / `google-api-python-client`, `mcp` 2.x (`mcp.server.mcpserver.MCPServer`), PyYAML (new), vanilla JS front end with a cache-first service worker.

**Spec:** `docs/superpowers/specs/2026-09-20-hermes-google-workspace-tools-design.md`

## Global Constraints

- No test runner exists. Verification is standalone scripts under `scripts/verify/`, run with plain `python`, printing `PASS`/`FAIL` lines and exiting non-zero on the first failure. Every script sets `DB_PATH` to a temp file before importing app code, and stubs anything that would spend money or reach the network.
- Always `source venv/bin/activate` first. `.env` is loaded with `load_dotenv(override=True)` at the top of entrypoints, before other imports.
- The `mcp` package in the venv is **2.x**: `from mcp.server.mcpserver import MCPServer`. There is no `FastMCP`.
- Refresh must use the **stored** scopes from the token JSON, never the requested list. google-auth raises `RefreshError` when the requested scopes are not a subset of the granted ones.
- The MCP server writes **nothing to stdout** except the protocol. All logging is stderr.
- Every tool returns a `str`. On any failure it returns a one-line string starting `Error:` and never raises across the MCP boundary.
- Any edit to `static/js/app.js` bumps `VERSION` in `static/js/sw.js` (currently `'v11'`), or the browser keeps the old cached frontend.
- Hermes facts relied on (verified against the installed 0.21.3 source): `mcp_servers.<name>.env` values are `${VAR}`-expanded from the launching process's environment and unresolved references stay verbatim; stdio entries accept `cwd`; single-query mode waits up to 15s for servers to come up.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Work on branch `ritwik/feat/hermes-google-workspace-tools`.

## File Structure

| File | Responsibility |
|---|---|
| `google_scopes.py` (new) | The only definition of `POLL_SCOPES`, `AGENT_SCOPES`, `ALL_SCOPES`, plus `has_agent_access(scopes)`. Sets `OAUTHLIB_RELAX_TOKEN_SCOPE`. |
| `pollers/gmail/auth.py` (modify) | `get_google_credentials(conn, user_id, account_id)` — refresh/persist/clear on stored scopes. `get_gmail_service` becomes a wrapper. `SCOPES` imports `ALL_SCOPES`. Flow adds `login_hint`. |
| `auth.py` (modify) | `LOGIN_SCOPES` uses `ALL_SCOPES`; `include_granted_scopes`. |
| `app.py` (modify) | `/settings` adds `agent_access` per account; `gmail_auth` forwards `login_hint`, adds `include_granted_scopes`. |
| `static/js/app.js`, `static/js/sw.js` (modify) | Per-account agent-access line with a Grant link; version bump. |
| `agent/google_mcp/__init__.py` (new) | Package marker. |
| `agent/google_mcp/services.py` (new) | `Binding`, `binding_from_env`, `ToolError`, `Services` (account resolution, scope gating, cached API clients). |
| `agent/google_mcp/tools.py` (new) | `build_tools(svc) -> list[Callable]`: the seventeen tool closures. Plain Python, no MCP types. |
| `agent/google_mcp/__main__.py` (new) | `make_server(binding) -> MCPServer`; entrypoint loads `.env`, builds server, `run("stdio")`. |
| `agent/hermes_runner.py` (modify) | `_google_binding_env(user_id, account_id)`; `_run` takes `binding` and merges it into the subprocess env. |
| `scripts/install_hermes_google_mcp.py` (new) | Idempotent writer/remover of the `action_inbox_google` entry in Hermes' config.yaml. |
| `agent/hermes_prompt.py` (modify) | Tool-preference paragraph in step 2; one sentence in follow-up. |
| `scripts/verify/verify_google_scopes.py` (new) | Scopes module, credential helper, `/settings` payload, `login_hint`. |
| `scripts/verify/verify_google_mcp.py` (new) | Binding, zero-tools, account resolution, scope gating, every tool against fake clients. |
| `scripts/verify/verify_executor.py` (modify) | Stubs accept the new `binding` argument; asserts the env binding. |
| `requirements.txt`, `.env.example`, `CLAUDE.md` (modify) | `pyyaml`; `HERMES_GOOGLE_TOOLS`; docs. |

---

### Task 1: Scope module and general credential helper

**Files:**
- Create: `google_scopes.py`
- Modify: `pollers/gmail/auth.py:13` (SCOPES) and `pollers/gmail/auth.py:49-97` (`get_gmail_service`)
- Modify: `auth.py:22-29` (LOGIN_SCOPES), `auth.py:31-33` (relax env)
- Create: `scripts/verify/verify_google_scopes.py`

**Interfaces:**
- Produces: `google_scopes.POLL_SCOPES: list[str]`, `AGENT_SCOPES: list[str]`, `ALL_SCOPES: list[str]`, `has_agent_access(scopes: Iterable[str]) -> bool`.
- Produces: `pollers.gmail.auth.get_google_credentials(conn, user_id: str, account_id: str | None = None) -> tuple[Credentials, set[str], str]` returning `(creds, granted_scopes, resolved_account_id)`. Raises `RuntimeError` when not connected / revoked / expired (clearing the row in the latter two cases, as today).
- Consumed by Task 3 (`Services`) and Task 2 (`/settings`).

- [ ] **Step 1: Write the failing verify script**

Create `scripts/verify/verify_google_scopes.py`:

```python
"""Verifies the Google scope set and the general credential helper.

Checks that both OAuth flows request the same Workspace-wide scope list, that
`has_agent_access` is the exact test Settings uses, and — the one that matters —
that `get_google_credentials` refreshes with the scopes *stored on the token*,
not the list the app now requests. google-auth raises on refresh when the
requested set exceeds the granted one, so getting this wrong would break polling
for every account connected before the scope change.

No network: `Credentials.refresh` is monkeypatched.

Usage: python scripts/verify/verify_google_scopes.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "scopes.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only-not-a-real-key")

import sqlite3

from google.oauth2.credentials import Credentials

import google_scopes
from db import init_db, set_source_credentials, upsert_user
import pollers.gmail.auth as gauth
import auth as login_auth


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def stored_creds(scopes: list[str], expired: bool) -> dict:
    expiry = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
    return {
        "token": "old-token",
        "refresh_token": "refresh-token",
        "client_id": "verify-client-id",
        "client_secret": "verify-client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes,
        "expiry": expiry.replace(tzinfo=None).isoformat() + "Z",
    }


def check_scopes() -> None:
    print("\n-- scope module --")
    check("POLL_SCOPES is exactly gmail.readonly", google_scopes.POLL_SCOPES == [READONLY])
    for needle in ("gmail.modify", "auth/drive", "auth/documents", "auth/spreadsheets",
                   "calendar.events", "contacts.readonly", "contacts.other.readonly"):
        check(f"AGENT_SCOPES contains {needle}",
              any(needle in s for s in google_scopes.AGENT_SCOPES))
    check("ALL_SCOPES is poll + agent, no duplicates",
          google_scopes.ALL_SCOPES == google_scopes.POLL_SCOPES + google_scopes.AGENT_SCOPES
          and len(set(google_scopes.ALL_SCOPES)) == len(google_scopes.ALL_SCOPES))
    check("has_agent_access true for the full set",
          google_scopes.has_agent_access(google_scopes.ALL_SCOPES))
    check("has_agent_access false for readonly only",
          not google_scopes.has_agent_access([READONLY]))
    check("has_agent_access false when one agent scope is missing",
          not google_scopes.has_agent_access(google_scopes.ALL_SCOPES[:-1]))
    check("Gmail reconnect flow requests ALL_SCOPES", gauth.SCOPES == google_scopes.ALL_SCOPES)
    check("Sign-in flow requests openid + ALL_SCOPES",
          login_auth.LOGIN_SCOPES[:3] == ["openid",
                                          "https://www.googleapis.com/auth/userinfo.email",
                                          "https://www.googleapis.com/auth/userinfo.profile"]
          and login_auth.LOGIN_SCOPES[3:] == google_scopes.ALL_SCOPES)
    check("OAUTHLIB_RELAX_TOKEN_SCOPE set by the scope module",
          os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1")


def check_credentials() -> None:
    print("\n-- get_google_credentials --")
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    init_db(conn)
    user_id, _ = upsert_user(conn, email="verify@example.com", name="V", picture_url=None)

    # An account connected before the scope change: readonly only, token expired.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds([READONLY], expired=True), account_id="old@example.com")
    # A second account with the full grant, token still fresh.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds(google_scopes.ALL_SCOPES, expired=False),
                           account_id="new@example.com")

    seen_scopes: list = []

    def fake_refresh(self, request):
        seen_scopes.append(list(self.scopes or []))
        self.token = "fresh-token"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    real_refresh = Credentials.refresh
    Credentials.refresh = fake_refresh
    try:
        creds, granted, account = gauth.get_google_credentials(conn, user_id, "old@example.com")
        check("refresh ran for the expired account", seen_scopes == [[READONLY]])
        check("refresh used the STORED scopes, not the requested list",
              seen_scopes[0] == [READONLY])
        check("granted set reflects the stored scopes", granted == {READONLY})
        check("resolved account echoed back", account == "old@example.com")
        row = conn.execute(
            "SELECT credentials FROM source_connections WHERE account_id='old@example.com'"
        ).fetchone()
        check("refreshed token persisted", json.loads(row[0])["token"] == "fresh-token")

        creds, granted, account = gauth.get_google_credentials(conn, user_id, "new@example.com")
        check("fresh token not refreshed", len(seen_scopes) == 1)
        check("full grant reported", granted == set(google_scopes.ALL_SCOPES))

        _, _, account = gauth.get_google_credentials(conn, user_id, None)
        check("account_id=None resolves to the first connected account",
              account == "old@example.com")

        try:
            gauth.get_google_credentials(conn, user_id, "nobody@example.com")
            check("unknown account raises RuntimeError", False)
        except RuntimeError as exc:
            check("unknown account raises RuntimeError", "not connected" in str(exc))

        svc = gauth.get_gmail_service(conn, user_id, "new@example.com")
        check("get_gmail_service still builds a client", hasattr(svc, "users"))
    finally:
        Credentials.refresh = real_refresh
    conn.close()


if __name__ == "__main__":
    check_scopes()
    check_credentials()
    print("\nAll checks passed.")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_google_scopes.py`
Expected: `ModuleNotFoundError: No module named 'google_scopes'`

- [ ] **Step 3: Create `google_scopes.py`**

```python
"""The one definition of which Google scopes the app asks for.

Two groups. `POLL_SCOPES` is what the Gmail poller needs and is what every account
connected before the agent tools existed has. `AGENT_SCOPES` is what the Hermes
executor's Google tools need. Both OAuth flows (sign-in in `auth.py`, reconnect in
`pollers/gmail/auth.py`) request the union; a token may still carry fewer scopes
if the user unticks boxes at consent, which is why refresh always uses the scopes
stored on the token and Settings shows agent access per account.
"""

import os
from typing import Iterable

POLL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

AGENT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
    # Addresses the user has merely corresponded with — where most lookups land.
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

ALL_SCOPES = POLL_SCOPES + AGENT_SCOPES

# Google echoes scopes back in a different order or with some unticked; the strict
# default makes oauthlib raise. Both flows rely on this, so it lives with the scopes.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def has_agent_access(scopes: Iterable[str]) -> bool:
    """True when a token carries every scope the agent tools need."""
    return set(AGENT_SCOPES).issubset(set(scopes))
```

- [ ] **Step 4: Refactor `pollers/gmail/auth.py`**

Replace the `SCOPES` line and the whole `get_gmail_service` function with:

```python
from google_scopes import ALL_SCOPES

SCOPES = ALL_SCOPES


def get_google_credentials(
    conn: sqlite3.Connection, user_id: str, account_id: str | None = None
) -> tuple[Credentials, set[str], str]:
    """Valid credentials for one connected Google account.

    Returns (credentials, granted scopes, resolved account id). account_id=None
    resolves to the user's first connected account, which is the fallback for
    todos created before per-account provenance existed.

    Refresh is done against the scopes *stored on the token*, not `SCOPES`:
    google-auth raises RefreshError when the requested set is not a subset of
    what Google returns, and every account connected before the Workspace scopes
    were added carries only gmail.readonly. The granted set is returned so callers
    can gate on it instead of guessing.
    """
    row = get_source_connection(conn, user_id, "gmail", account_id)
    if not row:
        label = account_id or "any account"
        raise RuntimeError(
            f"Gmail not connected ({label}). Visit the settings page to authorize."
        )

    # Resolve the concrete account so revocation clears only this row, never
    # every account the user has connected.
    resolved = row["account_id"]
    info = row["credentials"]
    stored_scopes = list(info.get("scopes") or [])
    creds = Credentials.from_authorized_user_info(info, stored_scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Google has revoked the refresh token (Testing-mode 7-day expiry,
                # user revoked access, password change, etc.). Clear the stored
                # credentials for THIS account so the UI flips to "not connected"
                # and prompts re-auth, leaving the user's other accounts polling.
                clear_source_connection(conn, user_id, "gmail", resolved)
                raise RuntimeError(
                    f"Gmail access revoked by Google for {resolved or 'this account'}. "
                    "Reconnect it in settings."
                ) from e
            refreshed = json.loads(creds.to_json())
            # to_json drops keys it doesn't own; keep ours (connected_email).
            refreshed = {**info, **refreshed}
            set_source_credentials(
                conn, user_id, "gmail", "oauth2", refreshed, account_id=resolved,
            )
        else:
            clear_source_connection(conn, user_id, "gmail", resolved)
            raise RuntimeError(
                f"Gmail credentials expired for {resolved or 'this account'}. "
                "Re-authorize via the settings page."
            )

    return creds, set(stored_scopes), resolved


def get_gmail_service(
    conn: sqlite3.Connection, user_id: str, account_id: str | None = None
):
    """Build a Gmail client for one connected account (see get_google_credentials)."""
    creds, _, _ = get_google_credentials(conn, user_id, account_id)
    return build("gmail", "v1", credentials=creds)
```

- [ ] **Step 5: Point `auth.py` at the scope module**

Replace the `LOGIN_SCOPES` list and the `os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")` line with:

```python
from google_scopes import ALL_SCOPES

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    # The full Workspace grant is requested at sign-in so credentials persist in a
    # single roundtrip. Declining any box leaves a narrower token; Settings shows
    # per account whether the agent tools have what they need.
    *ALL_SCOPES,
]
```

Keep the two-line comment above it about relaxing token scope only if you move it into `google_scopes.py`; the env var is now set there, so delete the `setdefault` line from `auth.py`.

- [ ] **Step 6: Run the verify script**

Run: `python scripts/verify/verify_google_scopes.py`
Expected: every line `PASS`, ending `All checks passed.`

- [ ] **Step 7: Run the existing scripts that touch these files**

Run: `python scripts/verify/verify_connections.py && python scripts/verify/verify_web.py`
Expected: both pass unchanged.

- [ ] **Step 8: Commit**

```bash
git add google_scopes.py pollers/gmail/auth.py auth.py scripts/verify/verify_google_scopes.py
git commit -m "feat(auth): Workspace-wide scope set; refresh on stored scopes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Consent flow and Settings agent-access state

**Files:**
- Modify: `auth.py:78-90` (`start_login`)
- Modify: `app.py:751-780` (`get_settings`), `app.py:782-797` (`gmail_auth`)
- Modify: `static/js/app.js:1232-1263` (`renderGmailAccounts`), `static/js/sw.js:12`
- Modify: `scripts/verify/verify_google_scopes.py`

**Interfaces:**
- Consumes: `google_scopes.has_agent_access`, `db.get_source_connection`.
- Produces: `GET /settings` → `sources.gmail.accounts[i].agent_access: bool` and `sources.gmail.grant_url_template: str` (the auth URL with `login_hint=` appended, front end fills the email).

- [ ] **Step 1: Add the failing checks**

Append to `scripts/verify/verify_google_scopes.py`, before `if __name__ == "__main__":`:

```python
def check_settings() -> None:
    print("\n-- /settings --")
    import app as app_module
    from db import init_db as _init
    client = app_module.app.test_client()
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    _init(conn)
    user_id, _ = upsert_user(conn, email="settings@example.com", name="S", picture_url=None)
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds([READONLY], expired=False), account_id="ro@example.com")
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds(google_scopes.ALL_SCOPES, expired=False),
                           account_id="full@example.com")
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "settings@example.com"
    payload = client.get("/settings").get_json()
    by_email = {a["email"]: a for a in payload["sources"]["gmail"]["accounts"]}
    check("readonly account reports agent_access False", by_email["ro@example.com"]["agent_access"] is False)
    check("full account reports agent_access True", by_email["full@example.com"]["agent_access"] is True)
    check("grant_url_template ends with login_hint=",
          payload["sources"]["gmail"]["grant_url_template"].endswith("login_hint="))

    resp = client.get("/settings/sources/gmail/auth?login_hint=ro%40example.com")
    check("gmail auth redirects to Google", resp.status_code == 302 and "accounts.google.com" in resp.headers["Location"])
    check("login_hint forwarded to Google", "login_hint=ro%40example.com" in resp.headers["Location"])
    check("include_granted_scopes requested", "include_granted_scopes=true" in resp.headers["Location"])
    check("consent still requested", "prompt=select_account" in resp.headers["Location"])
```

And add `check_settings()` to the `__main__` block after `check_credentials()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_google_scopes.py`
Expected: `FAIL  readonly account reports agent_access False` (KeyError on `agent_access` is also acceptable: the field doesn't exist yet).

- [ ] **Step 3: Extend `/settings` and `gmail_auth` in `app.py`**

In `get_settings`, replace the `"gmail": {...}` block with:

```python
            "gmail": {
                "accounts": [
                    {
                        "email": a["account_id"],
                        "connected_at": a["connected_at"],
                        "agent_access": _account_has_agent_access(db, user_id, a["account_id"]),
                    }
                    for a in accounts
                ],
                "auth_url": url_for("gmail_auth"),
                # The front end appends the account's address so Google preselects it.
                "grant_url_template": url_for("gmail_auth") + "?login_hint=",
            },
```

Add this helper above `get_settings`:

```python
def _account_has_agent_access(db, user_id: str, account_id: str) -> bool:
    """Whether the stored token for one account carries every agent scope."""
    from google_scopes import has_agent_access
    row = get_source_connection(db, user_id, "gmail", account_id)
    return bool(row) and has_agent_access(row["credentials"].get("scopes") or [])
```

Make sure `get_source_connection` is in the `from db import (...)` list at the top of `app.py` (it may already be).

In `gmail_auth`, replace the `authorization_url` call with:

```python
    extra = {}
    login_hint = (request.args.get("login_hint") or "").strip()
    if login_hint:
        # "Grant agent access" on one account: preselect it at Google.
        extra["login_hint"] = login_hint
    auth_url, state = flow.authorization_url(
        access_type="offline",
        # `select_account` is required for multi-account: with `consent` alone
        # Google silently reuses the already signed-in account, making it
        # impossible to add a second mailbox from the browser.
        prompt="select_account consent",
        # Extend an existing grant rather than replace it, so re-consenting for
        # the agent scopes keeps whatever the token already had.
        include_granted_scopes="true",
        **extra,
    )
```

- [ ] **Step 4: Add `include_granted_scopes` to sign-in**

In `auth.py` `start_login`, change the `authorization_url` call to:

```python
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
```

- [ ] **Step 5: Render agent access in Settings**

In `static/js/app.js`, `renderGmailAccounts`, replace the `container.innerHTML = ...` template with:

```javascript
  const grantTemplate = (window.__settings && window.__settings.gmailGrantUrlTemplate) || '/settings/sources/gmail/auth?login_hint=';
  container.innerHTML = accounts.map(a => `
    <div class="gmail-account-row" data-email="${escapeHtml(a.email)}">
      <span class="gmail-account-email">${escapeHtml(a.email)}</span>
      <span class="gmail-agent-access ${a.agent_access ? 'on' : 'off'}">
        ${a.agent_access
          ? 'Agent access: granted'
          : `Agent access not granted · <a href="${grantTemplate}${encodeURIComponent(a.email)}">Grant</a>`}
      </span>
      <button class="btn-disconnect gmail-account-disconnect">Disconnect</button>
    </div>`).join('');
```

Find where `renderGmailAccounts(data.sources.gmail.accounts)` is called after fetching `/settings` (search for `sources.gmail.accounts` in `app.js`), and immediately before that call add:

```javascript
      window.__settings = { gmailGrantUrlTemplate: data.sources.gmail.grant_url_template };
```

The disconnect handler returns `data.accounts` from `POST /settings/sources/gmail`; check that route in `app.py` and, if it builds its own account list, add the same `agent_access` field there using `_account_has_agent_access` so a disconnect re-render doesn't lose the line.

In `static/css/app.css` add:

```css
.gmail-agent-access { font-size: 0.85em; margin: 0 0.75rem; }
.gmail-agent-access.on { color: var(--ok, #2e7d32); }
.gmail-agent-access.off { color: var(--muted, #777); }
.gmail-agent-access a { text-decoration: underline; }
```

Bump `static/js/sw.js`: `const VERSION = 'v12';`

- [ ] **Step 6: Run the verify script**

Run: `python scripts/verify/verify_google_scopes.py`
Expected: all `PASS`.

- [ ] **Step 7: Check it in the browser**

Start the web UI (`flask --app app run --debug --port 5001`), open Settings, confirm each Gmail account shows the agent-access line and that Grant opens Google with the account preselected. Hard-reload once so the new service worker activates.

- [ ] **Step 8: Commit**

```bash
git add app.py auth.py static/js/app.js static/js/sw.js static/css/app.css scripts/verify/verify_google_scopes.py
git commit -m "feat(settings): per-account agent access with a Grant link; incremental consent

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: MCP server skeleton — binding, services, zero-tools rule

**Files:**
- Create: `agent/google_mcp/__init__.py`, `agent/google_mcp/services.py`, `agent/google_mcp/__main__.py`
- Create: `scripts/verify/verify_google_mcp.py`

**Interfaces:**
- Consumes: `pollers.gmail.auth.get_google_credentials`, `db.list_gmail_accounts`.
- Produces (services.py):
  - `class Binding(NamedTuple): user_id: str; account_id: str; db_path: str`
  - `binding_from_env(environ: Mapping[str, str]) -> Binding | None` — `None` when `AIB_USER_ID` is missing, blank, or an unexpanded `${...}`.
  - `class ToolError(Exception)` — message is user-facing.
  - `SERVICE_SCOPES: dict[str, tuple[tuple[str, ...], str]]` keyed `gmail_read`, `gmail`, `drive`, `docs`, `sheets`, `calendar`, `contacts` → (accepted scopes, human label).
  - `class Services(binding, creds_provider=None, builder=None)` with `accounts() -> list[str]`, `resolve_account(account: str | None) -> str`, `granted(account: str) -> set[str]`, `require(service: str, account: str | None) -> str` (resolves + gates, raises `ToolError`), and client getters `gmail(account)`, `drive(account)`, `docs(account)`, `sheets(account)`, `calendar(account)`, `people(account)`.
- Produces (`__main__.py`): `make_server(binding: Binding | None) -> MCPServer`; running the module serves stdio.
- Task 4–6 add tools through `tools.build_tools(svc)`; `make_server` calls it when it exists (this task ships a stub `build_tools` returning `[google_accounts]` only).

- [ ] **Step 1: Write the failing verify script**

Create `scripts/verify/verify_google_mcp.py`:

```python
"""Verifies the Google Workspace MCP server without Hermes, Google, or the network.

The server is what Hermes talks to; these checks cover the contract around it:
no binding means no tools; the binding picks the account; a missing scope is an
error string before any API call; and each tool turns API failures into a plain
`Error:` line instead of raising. Google clients are replaced with `Fake`, which
records the fluent call chain and returns a canned response.

Usage: python scripts/verify/verify_google_mcp.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "mcp.db")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

from googleapiclient.errors import HttpError

import google_scopes
from db import init_db, set_source_credentials, upsert_user
from agent.google_mcp import services as S
from agent.google_mcp.__main__ import make_server


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


READONLY = "https://www.googleapis.com/auth/gmail.readonly"
FULL = set(google_scopes.ALL_SCOPES)


class Fake:
    """Stands in for a googleapiclient resource. Any attribute is a method that
    records (name, kwargs) and returns the same Fake; execute() returns the
    canned response for the *last* recorded method name, or raises."""

    def __init__(self, responses=None, raise_exc=None):
        self.responses = responses or {}
        self.raise_exc = raise_exc
        self.calls = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, kwargs))
            return self
        return method

    def execute(self, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        last = self.calls[-1][0] if self.calls else None
        return self.responses.get(last, self.responses.get("*", {}))

    def last(self, name):
        for n, kw in reversed(self.calls):
            if n == name:
                return kw
        return None


class FakeResp:
    def __init__(self, status): self.status = status; self.reason = "boom"


def http_error(status=403):
    return HttpError(FakeResp(status), b'{"error": {"message": "denied"}}')


def make_binding(account="a@example.com"):
    return S.Binding(user_id="u1", account_id=account, db_path=os.environ["DB_PATH"])


def make_services(grants: dict, clients: dict | None = None):
    """grants: account -> set of scopes. clients: (kind, account) -> Fake."""
    clients = clients or {}
    order = list(grants)

    def creds_provider(conn, user_id, account_id):
        acct = account_id or order[0]
        if acct not in grants:
            raise RuntimeError(f"Gmail not connected ({acct}).")
        return object(), set(grants[acct]), acct

    def builder(kind, account, creds):
        return clients.setdefault((kind, account), Fake())

    svc = S.Services(make_binding(order[0]), creds_provider=creds_provider, builder=builder,
                     account_lister=lambda: order)
    return svc, clients


def check_binding() -> None:
    print("\n-- binding --")
    check("no env → None", S.binding_from_env({}) is None)
    check("blank → None", S.binding_from_env({"AIB_USER_ID": " "}) is None)
    check("unexpanded ${AIB_USER_ID} → None",
          S.binding_from_env({"AIB_USER_ID": "${AIB_USER_ID}", "AIB_DB_PATH": "x"}) is None)
    b = S.binding_from_env({"AIB_USER_ID": "u1", "AIB_ACCOUNT_ID": "${AIB_ACCOUNT_ID}",
                            "AIB_DB_PATH": "/tmp/x.db"})
    check("bound with an unexpanded account → empty account", b == S.Binding("u1", "", "/tmp/x.db"))
    b = S.binding_from_env({"AIB_USER_ID": "u1", "AIB_ACCOUNT_ID": "A@Example.com"})
    check("account lowercased, db path defaults to DB_PATH env",
          b.account_id == "a@example.com" and b.db_path == os.environ["DB_PATH"])


def check_server_shape() -> None:
    print("\n-- server --")
    tools = asyncio.run(make_server(None).list_tools())
    check("no binding → zero tools", tools == [])
    server = make_server(make_binding())
    names = {t.name for t in asyncio.run(server.list_tools())}
    check("bound → google_accounts registered", "google_accounts" in names)


def check_accounts_and_gating() -> None:
    print("\n-- account resolution and scope gating --")
    svc, clients = make_services({"a@example.com": {READONLY}, "b@example.com": FULL})
    check("explicit account wins", svc.resolve_account("b@example.com") == "b@example.com")
    check("default is the bound account", svc.resolve_account(None) == "a@example.com")
    check("explicit account is case-insensitive", svc.resolve_account("B@Example.com") == "b@example.com")
    try:
        svc.resolve_account("zzz@example.com")
        check("unknown account is a ToolError", False)
    except S.ToolError as exc:
        check("unknown account is a ToolError", "not one of the connected" in str(exc))

    check("gmail_read accepts readonly", svc.require("gmail_read", "a@example.com") == "a@example.com")
    try:
        svc.require("gmail", "a@example.com")
        check("gmail send on a readonly account is gated", False)
    except S.ToolError as exc:
        check("gmail send on a readonly account is gated",
              "a@example.com has not granted Gmail access" in str(exc)
              and "Grant agent access" in str(exc))
    check("drive granted on full account", svc.require("drive", "b@example.com") == "b@example.com")

    svc_empty, _ = make_services({"a@example.com": {READONLY}})
    svc_empty.binding = S.Binding("u1", "", os.environ["DB_PATH"])
    check("empty bound account falls back to first connected",
          svc_empty.resolve_account(None) == "a@example.com")

    tools = {t.__name__: t for t in __import__("agent.google_mcp.tools", fromlist=["x"]).build_tools(svc)}
    out = json.loads(tools["google_accounts"]())
    check("google_accounts lists both with access flags",
          [a["email"] for a in out] == ["a@example.com", "b@example.com"]
          and out[0]["agent_access"] is False and out[1]["agent_access"] is True
          and "Gmail (read)" in out[0]["services"] and "Drive" in out[1]["services"])
    check("google_accounts marks the default", out[0]["default"] is True and out[1]["default"] is False)


if __name__ == "__main__":
    check_binding()
    check_server_shape()
    check_accounts_and_gating()
    print("\nAll checks passed.")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: `ModuleNotFoundError: No module named 'agent.google_mcp'`

- [ ] **Step 3: Create the package and `services.py`**

`agent/google_mcp/__init__.py`:

```python
"""Google Workspace tools for the Hermes executor, served over MCP (stdio).

Registered once in ~/.hermes/config.yaml by scripts/install_hermes_google_mcp.py
and bound to a user and account per run by the AIB_* environment variables the
runner sets. Credentials come from the app's own database; nothing secret
crosses the environment. See docs/superpowers/specs/2026-09-20-hermes-google-workspace-tools-design.md.
"""
```

`agent/google_mcp/services.py`:

```python
"""Per-run binding, account resolution, scope gating, and Google API clients.

`Binding` is who this server process acts for. `Services` turns an optional
`account` argument into a concrete connected address, checks the stored token
carries the scope a tool needs, and hands back a cached API client. Everything
user-facing goes through `ToolError`, whose message the tool layer returns as an
`Error:` line — nothing here raises across the MCP boundary.
"""

import logging
import os
import sqlite3
from typing import Callable, Mapping, NamedTuple

logger = logging.getLogger(__name__)

_ENV_USER = "AIB_USER_ID"
_ENV_ACCOUNT = "AIB_ACCOUNT_ID"
_ENV_DB = "AIB_DB_PATH"


class Binding(NamedTuple):
    user_id: str
    account_id: str   # '' when the todo predates multi-account
    db_path: str


def _clean(value: str | None) -> str:
    """An env value as the runner meant it: stripped, and empty when Hermes left a
    `${VAR}` reference unexpanded because the variable was never set."""
    value = (value or "").strip()
    if value.startswith("${") and value.endswith("}"):
        return ""
    return value


def binding_from_env(environ: Mapping[str, str]) -> Binding | None:
    """The per-run binding, or None when this is not an Action Inbox turn."""
    user_id = _clean(environ.get(_ENV_USER))
    if not user_id:
        return None
    account = _clean(environ.get(_ENV_ACCOUNT)).lower()
    db_path = _clean(environ.get(_ENV_DB)) or os.environ.get("DB_PATH", "gmail_events.db")
    return Binding(user_id=user_id, account_id=account, db_path=db_path)


class ToolError(Exception):
    """A failure the agent should read as a tool result, worded for the user."""


_GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_RW = "https://www.googleapis.com/auth/gmail.modify"

# service key -> (any of these scopes satisfies it, label used in messages)
SERVICE_SCOPES: dict[str, tuple[tuple[str, ...], str]] = {
    "gmail_read": ((_GMAIL_RO, _GMAIL_RW), "Gmail (read)"),
    "gmail": ((_GMAIL_RW,), "Gmail"),
    "drive": (("https://www.googleapis.com/auth/drive",), "Drive"),
    "docs": (("https://www.googleapis.com/auth/documents",), "Docs"),
    "sheets": (("https://www.googleapis.com/auth/spreadsheets",), "Sheets"),
    "calendar": (("https://www.googleapis.com/auth/calendar.events",), "Calendar"),
    "contacts": (("https://www.googleapis.com/auth/contacts.readonly",), "Contacts"),
}

# service key -> (discovery name, version)
_CLIENTS = {
    "gmail": ("gmail", "v1"),
    "drive": ("drive", "v3"),
    "docs": ("docs", "v1"),
    "sheets": ("sheets", "v4"),
    "calendar": ("calendar", "v3"),
    "people": ("people", "v1"),
}


def _default_creds_provider(conn, user_id, account_id):
    from pollers.gmail.auth import get_google_credentials
    return get_google_credentials(conn, user_id, account_id)


def _default_builder(kind: str, account: str, creds):
    from googleapiclient.discovery import build
    name, version = _CLIENTS[kind]
    return build(name, version, credentials=creds, cache_discovery=False)


class Services:
    def __init__(
        self,
        binding: Binding,
        creds_provider: Callable | None = None,
        builder: Callable | None = None,
        account_lister: Callable[[], list[str]] | None = None,
    ):
        self.binding = binding
        self._creds_provider = creds_provider or _default_creds_provider
        self._builder = builder or _default_builder
        self._account_lister = account_lister or self._list_accounts_from_db
        self._granted: dict[str, set[str]] = {}
        self._creds: dict[str, object] = {}
        self._clients: dict[tuple[str, str], object] = {}

    # -- database -----------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.binding.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _list_accounts_from_db(self) -> list[str]:
        from db import list_gmail_accounts
        conn = self._open()
        try:
            return [a["account_id"] for a in list_gmail_accounts(conn, self.binding.user_id)]
        finally:
            conn.close()

    # -- accounts -----------------------------------------------------------

    def accounts(self) -> list[str]:
        return self._account_lister()

    def resolve_account(self, account: str | None) -> str:
        """Explicit argument, else the bound account, else the first connected."""
        connected = self.accounts()
        if not connected:
            raise ToolError("No Gmail account is connected. The user can connect one in Settings.")
        if account and account.strip():
            wanted = account.strip().lower()
            if wanted not in connected:
                raise ToolError(
                    f"{wanted} is not one of the connected accounts ({', '.join(connected)})."
                )
            return wanted
        if self.binding.account_id and self.binding.account_id in connected:
            return self.binding.account_id
        return connected[0]

    def _load(self, account: str):
        if account in self._granted:
            return
        conn = self._open()
        try:
            creds, granted, resolved = self._creds_provider(conn, self.binding.user_id, account)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            conn.close()
        self._creds[account] = creds
        self._granted[account] = set(granted)

    def granted(self, account: str) -> set[str]:
        self._load(account)
        return self._granted[account]

    def require(self, service: str, account: str | None) -> str:
        """Resolve the account and check it granted `service`. Returns the account."""
        resolved = self.resolve_account(account)
        accepted, label = SERVICE_SCOPES[service]
        if not any(s in self.granted(resolved) for s in accepted):
            raise ToolError(
                f"{resolved} has not granted {label} access. The user can grant it from "
                "Settings → Gmail → Grant agent access."
            )
        return resolved

    # -- clients ------------------------------------------------------------

    def _client(self, kind: str, account: str):
        key = (kind, account)
        if key not in self._clients:
            self._load(account)
            self._clients[key] = self._builder(kind, account, self._creds[account])
        return self._clients[key]

    def gmail(self, account: str): return self._client("gmail", account)
    def drive(self, account: str): return self._client("drive", account)
    def docs(self, account: str): return self._client("docs", account)
    def sheets(self, account: str): return self._client("sheets", account)
    def calendar(self, account: str): return self._client("calendar", account)
    def people(self, account: str): return self._client("people", account)
```

- [ ] **Step 4: Create `tools.py` with the shared error wrapper and `google_accounts`**

`agent/google_mcp/tools.py` (Tasks 4–6 add more closures inside `build_tools`; the wrapper and the accounts tool are final):

```python
"""The tool functions, as plain Python closures over a `Services`.

`build_tools(svc)` returns callables whose names, signatures and docstrings are
what the MCP layer exposes — no MCP types here, so another executor can wrap
the same functions. Every tool returns a string; `_safe` turns any failure into
a one-line `Error: …` result so Hermes always sees an ordinary tool reply.
"""

import functools
import json
import logging
from typing import Callable

from agent.google_mcp.services import SERVICE_SCOPES, Services, ToolError

logger = logging.getLogger(__name__)


def _safe(fn: Callable) -> Callable:
    """Never raise across the MCP boundary: every failure is a tool result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # HttpError, RefreshError, anything from the SDK
            logger.warning("%s failed: %s", fn.__name__, exc)
            return f"Error: {_describe(exc)}"
    return wrapper


def _describe(exc: Exception) -> str:
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", "?")
        try:
            body = json.loads(exc.content.decode("utf-8", errors="replace"))
            message = body.get("error", {}).get("message") or str(exc)
        except Exception:
            message = str(exc)
        return f"Google API returned {status}: {message}"
    return f"{type(exc).__name__}: {exc}"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_tools(svc: Services) -> list[Callable]:
    tools: list[Callable] = []

    def register(fn):
        tools.append(_safe(fn))
        return fn

    @register
    def google_accounts() -> str:
        """List the user's connected Google accounts, which one this todo came from
        (default), and which services each has granted. Pass `account` to any other
        tool to use a non-default account."""
        default = svc.resolve_account(None)
        out = []
        for email in svc.accounts():
            granted = svc.granted(email)
            services = [label for key, (accepted, label) in SERVICE_SCOPES.items()
                        if any(s in granted for s in accepted)]
            out.append({
                "email": email,
                "default": email == default,
                "agent_access": all(
                    any(s in granted for s in accepted)
                    for key, (accepted, _) in SERVICE_SCOPES.items() if key != "gmail_read"
                ),
                "services": services,
            })
        return _dumps(out)

    # Tasks 4-6 add the Gmail, Drive/Docs, Sheets/Calendar/Contacts tools here.

    return tools
```

- [ ] **Step 5: Create `__main__.py`**

```python
"""Entrypoint: `python -m agent.google_mcp`, run by Hermes as a stdio MCP server.

Reads the per-run binding from the environment. With no binding it still serves
— with zero tools — so a Hermes invocation that is not an Action Inbox turn is
unaffected. Logging goes to stderr only; stdout is the protocol channel.
"""

import logging
import os
import sys

# The repo's .env supplies DB_PATH and the Google client id/secret the refresh
# needs. Hermes launches this with cwd set to the repo root by the install
# script, so a relative lookup finds it. Loaded before any app import, as the
# entrypoints do, since module-level code reads env vars.
from dotenv import load_dotenv
load_dotenv(override=False)

from mcp.server.mcpserver import MCPServer

from agent.google_mcp.services import Binding, Services, binding_from_env


INSTRUCTIONS = (
    "The user's own Google account over the API. Use these tools for anything in "
    "Gmail, Drive, Docs, Sheets, Calendar or Contacts instead of a browser. Tools act "
    "on the account the todo came from unless you pass `account`; call "
    "google_accounts to see the options."
)


def make_server(binding: Binding | None) -> MCPServer:
    server = MCPServer("action_inbox_google", instructions=INSTRUCTIONS)
    if binding is None:
        logging.getLogger(__name__).info("no AIB binding; serving zero tools")
        return server
    from agent.google_mcp.tools import build_tools
    for fn in build_tools(Services(binding)):
        server.add_tool(fn)
    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="[google_mcp] %(levelname)s %(message)s")
    make_server(binding_from_env(os.environ)).run("stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the verify script**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: all `PASS`.

- [ ] **Step 7: Smoke the stdio server by hand**

Run (from the repo root, venv active):

```bash
AIB_USER_ID= python -m agent.google_mcp < /dev/null; echo "exit=$?"
```

Expected: exits 0 promptly with a stderr line containing `no AIB binding; serving zero tools`, and nothing on stdout.

- [ ] **Step 8: Commit**

```bash
git add agent/google_mcp scripts/verify/verify_google_mcp.py
git commit -m "feat(google_mcp): stdio MCP server skeleton with per-run binding and scope gating

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Gmail tools

**Files:**
- Modify: `agent/google_mcp/tools.py` (inside `build_tools`)
- Modify: `scripts/verify/verify_google_mcp.py`

**Interfaces:**
- Consumes: `svc.require("gmail_read"|"gmail", account)`, `svc.gmail(account)`, `pollers.gmail.thread_context._header`, `_extract_body`.
- Produces tools: `gmail_search_threads(query, max_results=10, account=None)`, `gmail_read_thread(thread_id, account=None)`, `gmail_create_draft(to, subject, body, cc=None, reply_to_thread_id=None, account=None)`, `gmail_send(...same...)`. All return `str`.

- [ ] **Step 1: Add the failing checks**

Append to `scripts/verify/verify_google_mcp.py` before `__main__`, and call `check_gmail()` from it:

```python
def tools_for(svc):
    from agent.google_mcp.tools import build_tools
    return {t.__name__: t for t in build_tools(svc)}


def check_gmail() -> None:
    print("\n-- gmail --")
    import base64
    from email import message_from_bytes

    thread_full = {"messages": [{
        "id": "m1", "internalDate": "1700000000000", "snippet": "hi",
        "payload": {"mimeType": "text/plain",
                    "headers": [{"name": "From", "value": "Alice <alice@example.com>"},
                                {"name": "To", "value": "b@example.com"},
                                {"name": "Subject", "value": "Booth deposit"},
                                {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
                                {"name": "Message-ID", "value": "<abc@mail>"}],
                    "body": {"data": base64.urlsafe_b64encode(b"Please pay by Friday").decode()}}}]}
    gmail = Fake(responses={
        "list": {"threads": [{"id": "t1", "snippet": "hi"}]},
        "get": thread_full,
        "send": {"id": "sent1", "threadId": "t1"},
        "create": {"id": "d1", "message": {"id": "dm1", "threadId": "t1"}},
    })
    svc, _ = make_services({"b@example.com": FULL}, {("gmail", "b@example.com"): gmail})
    t = tools_for(svc)

    out = json.loads(t["gmail_search_threads"]("from:alice"))
    check("search returns thread id, subject, from, snippet",
          out[0]["thread_id"] == "t1" and out[0]["subject"] == "Booth deposit"
          and out[0]["from"] == "Alice <alice@example.com>" and out[0]["snippet"] == "hi")
    check("search passed the query and cap", gmail.last("list")["q"] == "from:alice"
          and gmail.last("list")["maxResults"] == 10)

    out = t["gmail_read_thread"]("t1")
    check("read_thread renders from/to/date/subject/body",
          "Alice <alice@example.com>" in out and "b@example.com" in out
          and "Booth deposit" in out and "Please pay by Friday" in out)

    out = json.loads(t["gmail_send"]("alice@example.com", "", "On it.", reply_to_thread_id="t1"))
    raw = gmail.last("send")["body"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw["raw"] + "=="))
    check("reply threads onto the Gmail thread", raw["threadId"] == "t1")
    check("reply sets In-Reply-To and References",
          msg["In-Reply-To"] == "<abc@mail>" and msg["References"] == "<abc@mail>")
    check("reply subject defaults to Re: original", msg["Subject"] == "Re: Booth deposit")
    check("send reports ids", out["message_id"] == "sent1" and out["thread_id"] == "t1")

    out = json.loads(t["gmail_create_draft"]("x@example.com", "Hello", "Body", cc="c@example.com"))
    draft = gmail.last("create")["body"]
    msg = message_from_bytes(base64.urlsafe_b64decode(draft["message"]["raw"] + "=="))
    check("draft is a fresh message with cc", "threadId" not in draft["message"]
          and msg["Cc"] == "c@example.com" and msg["Subject"] == "Hello")
    check("draft reports id and url", out["draft_id"] == "d1" and "mail.google.com" in out["url"])

    ro, _ = make_services({"r@example.com": {READONLY}})
    tr = tools_for(ro)
    check("read on readonly account works", not tr["gmail_search_threads"]("x").startswith("Error"))
    check("send on readonly account is gated",
          tr["gmail_send"]("x@example.com", "s", "b").startswith("Error: r@example.com has not granted Gmail access"))

    broken = Fake(raise_exc=http_error(403))
    svc2, _ = make_services({"b@example.com": FULL}, {("gmail", "b@example.com"): broken})
    out = tools_for(svc2)["gmail_search_threads"]("x")
    check("HttpError becomes an Error line", out.startswith("Error: Google API returned 403"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: `KeyError: 'gmail_search_threads'`.

- [ ] **Step 3: Implement the Gmail tools**

In `agent/google_mcp/tools.py`, add these imports at the top:

```python
import base64
from email.message import EmailMessage

from pollers.gmail.thread_context import _extract_body, _header
```

Then, inside `build_tools`, replace the `# Tasks 4-6 add ...` comment with:

```python
    # -- Gmail --------------------------------------------------------------

    def _thread_headers(gmail, thread_id: str) -> tuple[str, str, str]:
        """(subject, last Message-ID, References) from a thread's last message."""
        thread = gmail.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Subject", "Message-ID", "References"],
        ).execute()
        messages = thread.get("messages") or []
        if not messages:
            raise ToolError(f"Thread {thread_id} has no messages.")
        headers = messages[-1].get("payload", {}).get("headers", [])
        subject = _header(headers, "Subject")
        message_id = _header(headers, "Message-ID")
        references = (_header(headers, "References") + " " + message_id).strip()
        return subject, message_id, references

    def _build_message(gmail, to, subject, body, cc, reply_to_thread_id) -> dict:
        """The Gmail API `message` resource for a send or a draft."""
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        payload: dict = {}
        if reply_to_thread_id:
            orig_subject, message_id, references = _thread_headers(gmail, reply_to_thread_id)
            if not subject:
                subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
            if message_id:
                msg["In-Reply-To"] = message_id
                msg["References"] = references
            payload["threadId"] = reply_to_thread_id
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body)
        payload["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return payload

    @register
    def gmail_search_threads(query: str, max_results: int = 10, account: str | None = None) -> str:
        """Search the user's Gmail with Gmail query syntax (e.g. 'from:alice newer_than:7d',
        'subject:invoice', 'has:attachment'). Returns threads with thread_id, subject,
        from, date and snippet. Use gmail_read_thread to read one in full."""
        acct = svc.require("gmail_read", account)
        gmail = svc.gmail(acct)
        resp = gmail.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for t in resp.get("threads", []):
            meta = gmail.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            messages = meta.get("messages") or [{}]
            headers = messages[-1].get("payload", {}).get("headers", [])
            out.append({
                "thread_id": t["id"],
                "subject": _header(headers, "Subject"),
                "from": _header(headers, "From"),
                "date": _header(headers, "Date"),
                "snippet": t.get("snippet", ""),
            })
        return _dumps(out)

    @register
    def gmail_read_thread(thread_id: str, account: str | None = None) -> str:
        """Read every message in a Gmail thread: from, to, date, subject and body text."""
        acct = svc.require("gmail_read", account)
        thread = svc.gmail(acct).users().threads().get(
            userId="me", id=thread_id, format="full").execute()
        parts = []
        for m in thread.get("messages", []):
            payload = m.get("payload", {})
            headers = payload.get("headers", [])
            body = _extract_body(payload) or m.get("snippet", "")
            parts.append(
                f"From: {_header(headers, 'From')}\nTo: {_header(headers, 'To')}\n"
                f"Date: {_header(headers, 'Date')}\nSubject: {_header(headers, 'Subject')}\n\n{body}"
            )
        return "\n\n---\n\n".join(parts) or f"Thread {thread_id} has no messages."

    @register
    def gmail_create_draft(to: str, subject: str, body: str, cc: str | None = None,
                           reply_to_thread_id: str | None = None,
                           account: str | None = None) -> str:
        """Create a Gmail draft (not sent). With reply_to_thread_id it is threaded as a
        reply and the subject defaults to 'Re: <original>'. Use this when the user asked
        for a draft or when any part of the content is inferred rather than confirmed."""
        acct = svc.require("gmail", account)
        gmail = svc.gmail(acct)
        message = _build_message(gmail, to, subject, body, cc, reply_to_thread_id)
        draft = gmail.users().drafts().create(userId="me", body={"message": message}).execute()
        return _dumps({"draft_id": draft["id"],
                       "url": f"https://mail.google.com/mail/u/0/#drafts?compose={draft['id']}"})

    @register
    def gmail_send(to: str, subject: str, body: str, cc: str | None = None,
                   reply_to_thread_id: str | None = None,
                   account: str | None = None) -> str:
        """Send an email from the user's account. Irreversible: only when the user asked
        for this message to this recipient and every input is confirmed. With
        reply_to_thread_id it is sent as a threaded reply."""
        acct = svc.require("gmail", account)
        gmail = svc.gmail(acct)
        message = _build_message(gmail, to, subject, body, cc, reply_to_thread_id)
        sent = gmail.users().messages().send(userId="me", body=message).execute()
        return _dumps({"message_id": sent["id"], "thread_id": sent.get("threadId")})

    # Tasks 5-6 add the Drive/Docs and Sheets/Calendar/Contacts tools here.
```

Note on the `Fake` test double: it returns the canned response for the *last* method name, so `threads().get(...)` and `drafts().create(...)` resolve to the `"get"` and `"create"` entries; the metadata `get` in search returns the full thread, whose headers still carry Subject/From, which is fine for the assertion.

- [ ] **Step 4: Run the verify script**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: all `PASS`.

- [ ] **Step 5: Commit**

```bash
git add agent/google_mcp/tools.py scripts/verify/verify_google_mcp.py
git commit -m "feat(google_mcp): Gmail search, read, draft and send tools

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Drive and Docs tools

**Files:**
- Modify: `agent/google_mcp/tools.py`
- Modify: `scripts/verify/verify_google_mcp.py`

**Interfaces:**
- Consumes: `svc.require("drive"|"docs", account)`, `svc.drive(account)`, `svc.docs(account)`.
- Produces tools: `drive_search(query, max_results=10, account=None)`, `drive_read_file(file_id, account=None)`, `drive_upload_file(name, content, mime_type="text/plain", folder_id=None, account=None)`, `docs_create(title, body_text="", account=None)`, `docs_append(document_id, text, account=None)`, `docs_replace_text(document_id, find, replace, account=None)`.
- Constant: `READ_CAP = 100_000` characters.

- [ ] **Step 1: Add the failing checks**

Append and call `check_drive_docs()`:

```python
def check_drive_docs() -> None:
    print("\n-- drive / docs --")
    drive = Fake(responses={
        "list": {"files": [{"id": "f1", "name": "Plan", "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-09-01T00:00:00Z", "webViewLink": "https://docs/f1"}]},
        "get": {"id": "f1", "name": "Plan", "mimeType": "application/vnd.google-apps.document"},
        "export": b"x" * 150_000,
        "create": {"id": "up1", "webViewLink": "https://drive/up1"},
    })
    docs = Fake(responses={
        "create": {"documentId": "doc1"},
        "get": {"body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}},
        "batchUpdate": {"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]},
    })
    svc, _ = make_services({"b@example.com": FULL},
                           {("drive", "b@example.com"): drive, ("docs", "b@example.com"): docs})
    t = tools_for(svc)

    out = json.loads(t["drive_search"]("plan"))
    q = drive.last("list")["q"]
    check("drive_search uses fullText and name", "fullText contains 'plan'" in q and "name contains 'plan'" in q)
    check("drive_search returns id, name, type, link", out[0]["id"] == "f1" and out[0]["webViewLink"] == "https://docs/f1")

    out = t["drive_read_file"]("f1")
    check("Google Doc exported as text/plain", drive.last("export")["mimeType"] == "text/plain")
    check("read capped at 100k with a note", len(out) < 100_200 and "truncated" in out.lower())

    drive.responses["get"] = {"id": "s1", "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet"}
    drive.responses["export"] = b"a,b\n1,2"
    out = t["drive_read_file"]("s1")
    check("Google Sheet exported as CSV", drive.last("export")["mimeType"] == "text/csv" and out == "a,b\n1,2")

    drive.responses["get"] = {"id": "z1", "name": "img.png", "mimeType": "image/png"}
    out = t["drive_read_file"]("z1")
    check("unsupported type named, not dumped", "image/png" in out and "cannot" in out.lower())

    out = json.loads(t["drive_upload_file"]("notes.txt", "hello", folder_id="fold1"))
    body = drive.last("create")["body"]
    check("upload names the file and parent", body["name"] == "notes.txt" and body["parents"] == ["fold1"])
    check("upload reports link", out["webViewLink"] == "https://drive/up1")

    out = json.loads(t["docs_create"]("Title", "First line"))
    ins = docs.last("batchUpdate")["body"]["requests"][0]["insertText"]
    check("docs_create inserts body at index 1", ins["text"] == "First line" and ins["location"]["index"] == 1)
    check("docs_create reports url", out["document_id"] == "doc1" and out["url"].endswith("/doc1/edit"))

    t["docs_append"]("doc1", "More")
    ins = docs.last("batchUpdate")["body"]["requests"][0]["insertText"]
    check("docs_append inserts before the final newline", ins["location"]["index"] == 41)

    out = t["docs_replace_text"]("doc1", "old", "new")
    rep = docs.last("batchUpdate")["body"]["requests"][0]["replaceAllText"]
    check("replace is case-sensitive and counts", rep["containsText"]["matchCase"] is True and "3" in out)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: `KeyError: 'drive_search'`.

- [ ] **Step 3: Implement**

Add near the top of `tools.py`:

```python
import io

READ_CAP = 100_000

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _cap(text: str) -> str:
    if len(text) <= READ_CAP:
        return text
    return text[:READ_CAP] + f"\n\n[truncated: {len(text) - READ_CAP} more characters not shown]"


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)
```

Inside `build_tools`, replace the `# Tasks 5-6 add ...` comment with:

```python
    # -- Drive --------------------------------------------------------------

    @register
    def drive_search(query: str, max_results: int = 10, account: str | None = None) -> str:
        """Search the user's Google Drive by words in the name or content. Returns id,
        name, mimeType, modifiedTime and webViewLink. Read one with drive_read_file."""
        acct = svc.require("drive", account)
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        resp = svc.drive(acct).files().list(
            q=f"(fullText contains '{safe}' or name contains '{safe}') and trashed = false",
            pageSize=max_results, orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
        return _dumps(resp.get("files", []))

    @register
    def drive_read_file(file_id: str, account: str | None = None) -> str:
        """Read a Drive file as text: Google Docs and Slides as plain text, Sheets as
        CSV, PDFs and text files as their text. Long files are truncated at 100k chars."""
        acct = svc.require("drive", account)
        drive = svc.drive(acct)
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        mime = meta.get("mimeType", "")
        if mime in _EXPORT:
            data = drive.files().export(fileId=file_id, mimeType=_EXPORT[mime]).execute()
            return _cap(data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data))
        if mime == "application/pdf" or mime.startswith("text/"):
            data = drive.files().get_media(fileId=file_id).execute()
            if isinstance(data, str):
                data = data.encode("utf-8")
            text = _pdf_text(data) if mime == "application/pdf" else data.decode("utf-8", errors="replace")
            return _cap(text)
        return (f"Cannot read {meta.get('name')!r}: type {mime} is not exportable as text. "
                "Open it in the browser if its contents are needed.")

    @register
    def drive_upload_file(name: str, content: str, mime_type: str = "text/plain",
                          folder_id: str | None = None, account: str | None = None) -> str:
        """Create a file in the user's Drive from text content. Returns id and webViewLink."""
        acct = svc.require("drive", account)
        from googleapiclient.http import MediaInMemoryUpload
        body: dict = {"name": name}
        if folder_id:
            body["parents"] = [folder_id]
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
        created = svc.drive(acct).files().create(
            body=body, media_body=media, fields="id,webViewLink").execute()
        return _dumps({"id": created["id"], "webViewLink": created.get("webViewLink")})

    # -- Docs ---------------------------------------------------------------

    def _doc_url(document_id: str) -> str:
        return f"https://docs.google.com/document/d/{document_id}/edit"

    @register
    def docs_create(title: str, body_text: str = "", account: str | None = None) -> str:
        """Create a Google Doc with a title and optional body text. Returns id and URL."""
        acct = svc.require("docs", account)
        docs = svc.docs(acct)
        doc = docs.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]
        if body_text:
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"insertText": {"location": {"index": 1}, "text": body_text}}]}).execute()
        return _dumps({"document_id": doc_id, "url": _doc_url(doc_id)})

    @register
    def docs_append(document_id: str, text: str, account: str | None = None) -> str:
        """Append text at the end of an existing Google Doc."""
        acct = svc.require("docs", account)
        docs = svc.docs(acct)
        doc = docs.documents().get(documentId=document_id, fields="body.content.endIndex").execute()
        content = doc.get("body", {}).get("content", [])
        # The body always ends with a newline the API won't let you write after.
        end = max((c.get("endIndex", 1) for c in content), default=1) - 1
        docs.documents().batchUpdate(documentId=document_id, body={"requests": [
            {"insertText": {"location": {"index": max(end, 1)}, "text": text}}]}).execute()
        return _dumps({"document_id": document_id, "url": _doc_url(document_id), "appended": len(text)})

    @register
    def docs_replace_text(document_id: str, find: str, replace: str, account: str | None = None) -> str:
        """Replace every case-sensitive occurrence of `find` in a Google Doc. Returns the count."""
        acct = svc.require("docs", account)
        resp = svc.docs(acct).documents().batchUpdate(documentId=document_id, body={"requests": [
            {"replaceAllText": {"containsText": {"text": find, "matchCase": True},
                                "replaceText": replace}}]}).execute()
        replies = resp.get("replies") or [{}]
        count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        return f"Replaced {count} occurrence(s) in {_doc_url(document_id)}"

    # Task 6 adds the Sheets/Calendar/Contacts tools here.
```

- [ ] **Step 4: Run the verify script**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: all `PASS`.

- [ ] **Step 5: Commit**

```bash
git add agent/google_mcp/tools.py scripts/verify/verify_google_mcp.py
git commit -m "feat(google_mcp): Drive search/read/upload and Docs create/append/replace

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Sheets, Calendar and Contacts tools

**Files:**
- Modify: `agent/google_mcp/tools.py`
- Modify: `scripts/verify/verify_google_mcp.py`

**Interfaces:**
- Consumes: `svc.require("sheets"|"calendar"|"contacts", account)`, `svc.sheets/calendar/people(account)`.
- Produces tools: `sheets_read_range(spreadsheet_id, range, account=None)`, `sheets_write_range(spreadsheet_id, range, values, account=None)`, `sheets_append_rows(spreadsheet_id, range, rows, account=None)`, `calendar_list_events(time_min, time_max, query=None, calendar_id="primary", account=None)`, `calendar_create_event(summary, start, end, attendees=None, description=None, location=None, calendar_id="primary", account=None)`, `contacts_search(query, max_results=10, account=None)`.

- [ ] **Step 1: Add the failing checks**

Append and call `check_sheets_calendar_contacts()`:

```python
def check_sheets_calendar_contacts() -> None:
    print("\n-- sheets / calendar / contacts --")
    sheets = Fake(responses={"get": {"values": [["a", "b"], ["1", "2"]]},
                             "update": {"updatedCells": 4},
                             "append": {"updates": {"updatedRows": 2}}})
    cal = Fake(responses={"list": {"items": [{"id": "e1", "summary": "Standup",
                                              "start": {"dateTime": "2026-09-21T09:00:00+05:30"},
                                              "end": {"dateTime": "2026-09-21T09:15:00+05:30"},
                                              "attendees": [{"email": "x@example.com"}],
                                              "htmlLink": "https://cal/e1"}]},
                          "insert": {"id": "e2", "htmlLink": "https://cal/e2"}})
    people = Fake(responses={
        "searchContacts": {"results": [{"person": {"names": [{"displayName": "Alice A"}],
                                                   "emailAddresses": [{"value": "alice@example.com"}]}}]},
        "search": {"results": [{"person": {"names": [{"displayName": "Bob"}],
                                           "emailAddresses": [{"value": "bob@example.com"}]}}]},
    })
    svc, _ = make_services({"b@example.com": FULL}, {("sheets", "b@example.com"): sheets,
                                                     ("calendar", "b@example.com"): cal,
                                                     ("people", "b@example.com"): people})
    t = tools_for(svc)

    out = json.loads(t["sheets_read_range"]("sid", "Sheet1!A1:B2"))
    check("sheets_read_range returns rows", out == [["a", "b"], ["1", "2"]])
    t["sheets_write_range"]("sid", "Sheet1!A1:B2", [["x", "y"], ["3", "4"]])
    kw = sheets.last("update")
    check("write uses USER_ENTERED", kw["valueInputOption"] == "USER_ENTERED" and kw["body"]["values"][0] == ["x", "y"])
    t["sheets_append_rows"]("sid", "Sheet1!A:B", [["5", "6"]])
    kw = sheets.last("append")
    check("append inserts rows", kw["insertDataOption"] == "INSERT_ROWS" and kw["valueInputOption"] == "USER_ENTERED")

    out = json.loads(t["calendar_list_events"]("2026-09-21T00:00:00Z", "2026-09-22T00:00:00Z", query="stand"))
    kw = cal.last("list")
    check("list_events bounds, query, single expanded events",
          kw["timeMin"].startswith("2026-09-21") and kw["q"] == "stand" and kw["singleEvents"] is True)
    check("list_events shape", out[0]["id"] == "e1" and out[0]["attendees"] == ["x@example.com"])

    out = json.loads(t["calendar_create_event"]("Coffee", "2026-09-22T10:00:00+05:30", "2026-09-22T10:30:00+05:30",
                                                attendees=["x@example.com"], location="Cafe"))
    kw = cal.last("insert")
    check("create_event body and invitations",
          kw["body"]["summary"] == "Coffee" and kw["body"]["attendees"] == [{"email": "x@example.com"}]
          and kw["sendUpdates"] == "all" and out["id"] == "e2")
    t["calendar_create_event"]("Solo", "2026-09-22", "2026-09-23")
    kw = cal.last("insert")
    check("all-day event uses date, no invitations",
          kw["body"]["start"] == {"date": "2026-09-22"} and kw["sendUpdates"] == "none")

    out = json.loads(t["contacts_search"]("ali"))
    check("contacts merges contacts and other contacts",
          {c["email"] for c in out} == {"alice@example.com", "bob@example.com"})
    check("contacts warmed the cache first", people.calls[0][0] == "people" and people.calls[1][0] == "searchContacts"
          and people.calls[1][1]["query"] == "")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: `KeyError: 'sheets_read_range'`.

- [ ] **Step 3: Implement**

Inside `build_tools`, replace the `# Task 6 adds ...` comment with:

```python
    # -- Sheets -------------------------------------------------------------

    @register
    def sheets_read_range(spreadsheet_id: str, range: str, account: str | None = None) -> str:
        """Read a range (A1 notation, e.g. 'Sheet1!A1:D20') from a Google Sheet. Returns rows."""
        acct = svc.require("sheets", account)
        resp = svc.sheets(acct).spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range).execute()
        return _dumps(resp.get("values", []))

    @register
    def sheets_write_range(spreadsheet_id: str, range: str, values: list[list[str]],
                           account: str | None = None) -> str:
        """Overwrite a range of a Google Sheet with rows of values (entered as a user would type them)."""
        acct = svc.require("sheets", account)
        resp = svc.sheets(acct).spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range, valueInputOption="USER_ENTERED",
            body={"values": values}).execute()
        return f"Updated {resp.get('updatedCells', 0)} cell(s) in {range}"

    @register
    def sheets_append_rows(spreadsheet_id: str, range: str, rows: list[list[str]],
                           account: str | None = None) -> str:
        """Append rows after the last row of the table that `range` points at."""
        acct = svc.require("sheets", account)
        resp = svc.sheets(acct).spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range, valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS", body={"values": rows}).execute()
        return f"Appended {resp.get('updates', {}).get('updatedRows', len(rows))} row(s)"

    # -- Calendar -----------------------------------------------------------

    def _when(value: str) -> dict:
        """An all-day date ('2026-09-22') or a dateTime (RFC 3339) as the API wants it."""
        return {"date": value} if len(value) == 10 else {"dateTime": value}

    @register
    def calendar_list_events(time_min: str, time_max: str, query: str | None = None,
                             calendar_id: str = "primary", account: str | None = None) -> str:
        """List the user's calendar events between two RFC 3339 times, optionally matching
        a search string. Returns id, summary, start, end, attendees and htmlLink."""
        acct = svc.require("calendar", account)
        kwargs = dict(calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
                      singleEvents=True, orderBy="startTime", maxResults=50)
        if query:
            kwargs["q"] = query
        resp = svc.calendar(acct).events().list(**kwargs).execute()
        return _dumps([{
            "id": e.get("id"), "summary": e.get("summary"),
            "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date"),
            "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date"),
            "attendees": [a.get("email") for a in e.get("attendees", [])],
            "location": e.get("location"), "htmlLink": e.get("htmlLink"),
        } for e in resp.get("items", [])])

    @register
    def calendar_create_event(summary: str, start: str, end: str,
                              attendees: list[str] | None = None, description: str | None = None,
                              location: str | None = None, calendar_id: str = "primary",
                              account: str | None = None) -> str:
        """Create a calendar event. start/end are RFC 3339 datetimes with offset, or plain
        dates for an all-day event. Attendees are emailed an invitation. Irreversible:
        only with confirmed inputs."""
        acct = svc.require("calendar", account)
        body: dict = {"summary": summary, "start": _when(start), "end": _when(end)}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        created = svc.calendar(acct).events().insert(
            calendarId=calendar_id, body=body,
            sendUpdates="all" if attendees else "none").execute()
        return _dumps({"id": created["id"], "htmlLink": created.get("htmlLink")})

    # -- Contacts -----------------------------------------------------------

    @register
    def contacts_search(query: str, max_results: int = 10, account: str | None = None) -> str:
        """Find a person's email address by name or partial address, across the user's
        contacts and the people they have corresponded with."""
        acct = svc.require("contacts", account)
        people = svc.people(acct)
        mask = "names,emailAddresses"
        # The People API requires a warm-up request before search results are complete.
        people.people().searchContacts(query="", readMask=mask, pageSize=1).execute()
        found: dict[str, str] = {}
        for call in (
            lambda: people.people().searchContacts(query=query, readMask=mask, pageSize=max_results).execute(),
            lambda: people.otherContacts().search(query=query, readMask=mask, pageSize=max_results).execute(),
        ):
            for r in call().get("results", []):
                person = r.get("person", {})
                name = (person.get("names") or [{}])[0].get("displayName", "")
                for e in person.get("emailAddresses") or []:
                    if e.get("value") and e["value"] not in found:
                        found[e["value"]] = name
        return _dumps([{"name": n, "email": e} for e, n in found.items()][:max_results * 2])
```

- [ ] **Step 4: Run the verify script**

Run: `python scripts/verify/verify_google_mcp.py`
Expected: all `PASS`.

- [ ] **Step 5: Confirm the registered tool set**

Run:

```bash
python - <<'PY'
import asyncio, os
os.environ["DB_PATH"] = "/tmp/x.db"
from agent.google_mcp.__main__ import make_server
from agent.google_mcp.services import Binding
print(sorted(t.name for t in asyncio.run(make_server(Binding("u", "", "/tmp/x.db")).list_tools())))
PY
```

Expected: exactly these 17 names — `calendar_create_event, calendar_list_events, contacts_search, docs_append, docs_create, docs_replace_text, drive_read_file, drive_search, drive_upload_file, gmail_create_draft, gmail_read_thread, gmail_search_threads, gmail_send, google_accounts, sheets_append_rows, sheets_read_range, sheets_write_range`.

- [ ] **Step 6: Commit**

```bash
git add agent/google_mcp/tools.py scripts/verify/verify_google_mcp.py
git commit -m "feat(google_mcp): Sheets, Calendar and Contacts tools

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Runner binding and Hermes registration

**Files:**
- Modify: `agent/hermes_runner.py:45-70` (`_run` signature and env), `:190-215` (`resolve`)
- Modify: `scripts/verify/verify_executor.py:46-70` (stub signatures) and its main
- Create: `scripts/install_hermes_google_mcp.py`
- Modify: `requirements.txt`, `.env.example`

**Interfaces:**
- Produces: `hermes_runner._google_binding_env(user_id: str, account_id: str | None) -> dict[str, str]` (empty when `HERMES_GOOGLE_TOOLS` is off); `_run(prompt, session_name, cancel=None, progress=None, binding=None)`.
- Consumes: env variable names `AIB_USER_ID`, `AIB_ACCOUNT_ID`, `AIB_DB_PATH` from Task 3.

- [ ] **Step 1: Add the failing checks to `verify_executor.py`**

Change the three stub signatures so they accept the new argument and record it:

```python
seen_bindings: list = []


def stub_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    seen_bindings.append(binding)
    ...  # existing body unchanged


def failing_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    ...  # existing body unchanged


def blocking_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    ...  # existing body unchanged
```

In `main()`, right after the first successful `turn(...)` with `stub_run` installed, add:

```python
    print("\n-- google tools binding --")
    b = seen_bindings[-1]
    check("resolve passes the binding to _run", isinstance(b, dict))
    check("binding names the user", b.get("AIB_USER_ID") == user_id)
    check("binding names the todo's account or empty", "AIB_ACCOUNT_ID" in b)
    check("binding carries an absolute db path", os.path.isabs(b.get("AIB_DB_PATH", "")))
    os.environ["HERMES_GOOGLE_TOOLS"] = "0"
    check("HERMES_GOOGLE_TOOLS=0 yields no binding", hermes_runner._google_binding_env(user_id, "x") == {})
    os.environ.pop("HERMES_GOOGLE_TOOLS")
    check("default yields the three variables",
          set(hermes_runner._google_binding_env(user_id, None)) == {"AIB_USER_ID", "AIB_ACCOUNT_ID", "AIB_DB_PATH"}
          and hermes_runner._google_binding_env(user_id, None)["AIB_ACCOUNT_ID"] == "")
```

(`user_id` is whatever variable `main()` already uses for the test user; read the file and match it. The kill test calls `_real_run("prompt", "aib-killtest", token)` positionally, which still works.)

- [ ] **Step 2: Run to verify it fails**

Run: `python scripts/verify/verify_executor.py`
Expected: `FAIL  resolve passes the binding to _run` (binding is `None`).

- [ ] **Step 3: Wire the binding in `hermes_runner.py`**

Add after the `HEADED = ...` block:

```python
# Hand the Google Workspace MCP server (agent/google_mcp) the user and account
# this turn is for. The server is registered once in ~/.hermes/config.yaml with
# `${AIB_*}` references in its env block; Hermes expands them from *this*
# process's environment at launch. No token crosses here — the server reads
# credentials from the app's database. Set HERMES_GOOGLE_TOOLS=0 to withhold
# the binding, which makes the server serve zero tools without a config edit.
def _google_tools_enabled() -> bool:
    # Read per call, not at import: the verify script toggles it at runtime.
    return os.environ.get("HERMES_GOOGLE_TOOLS", "1").strip().lower() not in {
        "0", "false", "no",
    }


def _google_binding_env(user_id: str, account_id: str | None) -> dict[str, str]:
    if not _google_tools_enabled():
        return {}
    from agent.db import DB_PATH
    return {
        "AIB_USER_ID": user_id,
        "AIB_ACCOUNT_ID": (account_id or "").strip().lower(),
        "AIB_DB_PATH": os.path.abspath(DB_PATH),
    }
```

Change `_run`'s signature to `def _run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:` and, right after `env = dict(os.environ)`, add:

```python
    if binding:
        env.update(binding)
```

In `resolve`, change the call to:

```python
        reply = _run(prompt, session_name, cancel, progress,
                     binding=_google_binding_env(user_id, todo.get("account_id")))
```

- [ ] **Step 4: Run the executor verify script**

Run: `python scripts/verify/verify_executor.py`
Expected: all `PASS`.

- [ ] **Step 5: Add PyYAML and write the install script**

Append to `requirements.txt`: `pyyaml>=6.0`. Then `pip install pyyaml`.

Create `scripts/install_hermes_google_mcp.py`:

```python
"""Register (or remove) the Google Workspace MCP server in Hermes' config.

Writes one entry, `mcp_servers.action_inbox_google`, into ~/.hermes/config.yaml
(or $HERMES_HOME/config.yaml). The entry runs this repo's venv python on
`agent/google_mcp` with the repo as cwd, and forwards the three AIB_* variables
as `${VAR}` references that Hermes expands from the launching process — the
runner sets them per turn, so nothing per-user lives in the config.

Idempotent: re-running replaces the entry. Hermes' own config writes already
drop YAML comments, so this doing the same costs nothing new.

Usage:
  python scripts/install_hermes_google_mcp.py           # install / update
  python scripts/install_hermes_google_mcp.py --remove
"""
import os
import sys

import yaml

SERVER_NAME = "action_inbox_google"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def config_path() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, "config.yaml")


def entry() -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "agent.google_mcp"],
        "cwd": REPO_ROOT,
        "env": {
            "AIB_USER_ID": "${AIB_USER_ID}",
            "AIB_ACCOUNT_ID": "${AIB_ACCOUNT_ID}",
            "AIB_DB_PATH": "${AIB_DB_PATH}",
        },
    }


def main(argv: list[str]) -> int:
    path = config_path()
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    if not isinstance(config, dict):
        print(f"{path} is not a mapping; refusing to edit it.", file=sys.stderr)
        return 1
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    if "--remove" in argv:
        if servers.pop(SERVER_NAME, None) is None:
            print(f"{SERVER_NAME} was not registered in {path}.")
            return 0
        action = "Removed"
    else:
        servers[SERVER_NAME] = entry()
        action = "Registered"
    config["mcp_servers"] = servers

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)

    print(f"{action} {SERVER_NAME} in {path}")
    if action == "Registered":
        print(yaml.safe_dump({SERVER_NAME: entry()}, sort_keys=False))
        print("Verify with:\n  AIB_USER_ID=<user id> AIB_DB_PATH=$(pwd)/gmail_events.db "
              f"hermes mcp test {SERVER_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 6: Run the install script against a scratch HERMES_HOME, then for real**

```bash
HERMES_HOME=/tmp/hermes-scratch python scripts/install_hermes_google_mcp.py && cat /tmp/hermes-scratch/config.yaml
HERMES_HOME=/tmp/hermes-scratch python scripts/install_hermes_google_mcp.py --remove && cat /tmp/hermes-scratch/config.yaml
```

Expected: first shows the entry with `command` pointing into `venv/bin/python` and `${AIB_*}` refs; second shows `mcp_servers: {}`.

Then, with the real config: `cp ~/.hermes/config.yaml ~/.hermes/config.yaml.bak && python scripts/install_hermes_google_mcp.py`, and check `hermes mcp list` shows `action_inbox_google`. Run `hermes mcp test action_inbox_google` with `AIB_USER_ID` set to a real user id from the database (`sqlite3 gmail_events.db "select user_id, email from users"`) and `AIB_DB_PATH` set to the absolute database path; expected: it connects and lists 17 tools. Without the variables it should connect and list 0 tools.

- [ ] **Step 7: Document the switch in `.env.example`**

After the `HERMES_ACTIVITY_POLL_SECONDS=` entry add:

```
# Give the Hermes agent the Google Workspace tools (agent/google_mcp) for the
# user and account each turn is for. On by default; needs the server registered
# once with scripts/install_hermes_google_mcp.py. Set to 0 to withhold the
# binding — the registered server then serves no tools and the agent falls back
# to the browser for Google.
HERMES_GOOGLE_TOOLS=
```

- [ ] **Step 8: Commit**

```bash
git add agent/hermes_runner.py scripts/verify/verify_executor.py scripts/install_hermes_google_mcp.py requirements.txt .env.example
git commit -m "feat(hermes): bind the Google MCP server per run; install script

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Prompt and documentation

**Files:**
- Modify: `agent/hermes_prompt.py:24-31` (step 2), `:142` (closing line), `:146-150` (follow-up)
- Modify: `CLAUDE.md` (Hermes bullets after the `HERMES_PERSISTENT_BROWSER` bullet; the "Auth and per-user credentials" section; the verify-script list)

**Interfaces:** none new. The prompt names tool prefixes registered in Tasks 4–6.

- [ ] **Step 1: Update step 2 of `INSTRUCTIONS`**

In `agent/hermes_prompt.py`, replace the sentence `Search the user's mail for prior context (tone, commitments, names, prices, dates, the recipient's address). Search their local files for notes, PDFs, and drafts.` with:

```
   The `gmail_*`, `drive_*`, `docs_*`, `sheets_*`, `calendar_*` and `contacts_*` tools are the \
   user's own Google account over the API, on the account this todo came from unless you pass \
   another connected one (`google_accounts` lists them). Use them for anything in Gmail, Drive, \
   Docs, Sheets, Calendar or Contacts — searching, reading, drafting, sending, creating — \
   instead of the browser; the browser is for everything else. Search their mail for prior \
   context (tone, commitments, names, prices, dates, the recipient's address). Search their \
   local files for notes, PDFs, and drafts.
```

Immediately after the existing rule 4 paragraph that begins `Confirmed *inputs* are not the same as a requested *act*.` add a short paragraph:

```
   Sending a message, creating an event, and writing to a document or sheet through the \
   Google tools are irreversible acts under this rule. A reply the user asked for, to the \
   person they named, with content that is confirmed, goes out with `gmail_send`. One whose \
   content or recipient you inferred goes to `gmail_create_draft` and a question — never to \
   `gmail_send`.
```

Replace the closing line `re-fetch it. Use your email tools for *additional* context beyond it.` with `re-fetch it. Use the Gmail tools for *additional* context beyond it.`

- [ ] **Step 2: Update `FOLLOWUP_INSTRUCTIONS`**

After `it is the most recent artifact you produced in this session.` add one sentence on the same paragraph:

```
Google — mail, Drive, Docs, Sheets, Calendar, Contacts — is reached through the `gmail_*`, \
`drive_*`, `docs_*`, `sheets_*`, `calendar_*` and `contacts_*` tools, not the browser.
```

- [ ] **Step 3: Check the prompt still builds and the verify scripts pass**

Run:

```bash
python -c "from agent.hermes_prompt import INSTRUCTIONS, FOLLOWUP_INSTRUCTIONS; assert 'gmail_send' in INSTRUCTIONS and 'gmail_*' in FOLLOWUP_INSTRUCTIONS; print('ok')"
python scripts/verify/verify_executor.py && python scripts/verify/verify_clarify.py
```

Expected: `ok`, then both scripts pass.

- [ ] **Step 4: Update `CLAUDE.md`**

In the "Running the project" verify list add:

```
python scripts/verify/verify_google_scopes.py  # scope set + credential refresh; no network
python scripts/verify/verify_google_mcp.py     # Google MCP server against fake clients; no network
```

In "Auth and per-user credentials", after the sentence ending `there is no global fallback key.` add:

```
The grant requested at sign-in and on reconnect is Workspace-wide (`google_scopes.py`:
Gmail modify, Drive, Docs, Sheets, Calendar events, Contacts) so the Hermes agent's
Google tools can use it. Accounts connected before that carry only `gmail.readonly` and
keep polling — refresh always uses the scopes stored on the token, never the requested
list, because google-auth raises when the requested set exceeds the granted one. Settings
shows per account whether agent access is granted, with a Grant link that re-consents
with `login_hint` and `include_granted_scopes`.
```

In the Hermes bullet list, after the `HERMES_PERSISTENT_BROWSER=1` bullet, add:

```
- **Google is reached over the API, not the browser** (`agent/google_mcp/`). A stdio MCP
  server in this repo serves Gmail/Drive/Docs/Sheets/Calendar/Contacts tools backed by the
  app's own per-account tokens in `source_connections`. Register it once with
  `python scripts/install_hermes_google_mcp.py` (writes `mcp_servers.action_inbox_google`
  into `~/.hermes/config.yaml` with `${AIB_*}` env references); the runner then sets
  `AIB_USER_ID`, `AIB_ACCOUNT_ID` and `AIB_DB_PATH` on each `hermes chat` subprocess and
  Hermes expands them into the server's env at launch. No token crosses the environment —
  the server reads and refreshes credentials from the database itself. With no binding
  (any Hermes run that isn't ours, or `HERMES_GOOGLE_TOOLS=0`) it serves zero tools.
  Every tool returns a string and turns failures into an `Error:` line, so Hermes' loop
  guardrails see ordinary results; a missing scope says so and points at Settings. The
  server's stdout is the protocol channel — log to stderr only. Hermes' single-query mode
  waits up to 15s for MCP servers to come up, which covers the Google client imports.
```

- [ ] **Step 5: Live check on one todo**

With the install script run (Task 7) and both processes up, open a todo that came from Gmail and ask the agent to reply. Watch the live trace: it should show `gmail_search_threads` / `gmail_read_thread` / `gmail_send` (or `gmail_create_draft` when inputs are inferred) and no browser calls. Then check `~/.hermes/logs` (or wherever `hermes mcp test` reported the stderr log) for `[google_mcp]` lines if anything is off.

- [ ] **Step 6: Commit**

```bash
git add agent/hermes_prompt.py CLAUDE.md
git commit -m "docs+prompt: point the agent at the Google tools; document the MCP server

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** §1 scopes → Task 1; consent flow, `login_hint`, `include_granted_scopes`, Settings → Task 2; §2 server files, binding, zero tools, account resolution, all 17 tools, error text, stderr logging → Tasks 3–6; §3 runner env, off switch, install script, PyYAML → Task 7; §4 prompt → Task 8; §5 verify scripts → Tasks 1–7, manual live check and docs → Tasks 7–8.
- **Deviations from the spec, deliberate:** `gmail_read_thread` includes `to`/`subject` by reading headers directly rather than reusing `fetch_thread_messages` (which drops them); `contacts_search` adds the People API warm-up call the API requires; `google_accounts` gained a `default` flag so the agent can see which account a bare call uses.
- **Type consistency:** `Services.require(service, account) -> str`, `build_tools(svc) -> list[Callable]`, `make_server(binding) -> MCPServer`, `_run(..., binding=None)`, `_google_binding_env(user_id, account_id) -> dict[str, str]` are used with the same names and shapes in every task that mentions them.
