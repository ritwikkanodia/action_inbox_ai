# Google Workspace tools for the Hermes executor

**Date:** 2026-09-20
**Status:** approved design, awaiting implementation plan

## Problem

The Hermes executor resolves todos with a browser, a terminal, and files. It has no
API access to the user's Google account, so anything in Gmail, Drive, Docs, Sheets,
Calendar or Contacts is done by driving Chrome — slow, fragile, and blind to the
credentials the app already holds. The resolution prompt even says "search the
user's mail with your email tools"; Hermes has no such tools, so that instruction
lands on the browser.

The app already obtains a Google OAuth grant per user and per Gmail account (sign-in
in `auth.py`, reconnect in `pollers/gmail/auth.py`), stored as JSON in
`source_connections`. Today that grant is `gmail.readonly` only. The goal is to
widen the grant and expose it to Hermes as first-class tools, so the agent uses the
API for Google and the browser for everything else.

## Decisions taken

- **Access level:** read and write everywhere the user chose — Gmail read/draft/send,
  Drive read/write, Docs, Sheets, Calendar (list + create events), Contacts (read).
- **Delivery:** an in-repo stdio MCP server, registered once in Hermes' config,
  bound to a user and account per run through environment variables. Credentials
  are read from the app's database inside the server; no token crosses the
  environment. Rejected: passing a bearer token to a third-party Workspace MCP
  (token expiry mid-run, no multi-account, tool surface not shaped by our rules)
  and Hermes' managed connectors (a separate grant to Nous, not the app's).

## 1. Scopes and consent

### Scope groups

A new module `google_scopes.py` is the single definition of scopes. Both OAuth
flows request `POLL_SCOPES + AGENT_SCOPES`.

| Group | Scopes |
|---|---|
| `POLL_SCOPES` (unchanged) | `https://www.googleapis.com/auth/gmail.readonly` |
| `AGENT_SCOPES` | `gmail.modify`, `drive`, `documents`, `spreadsheets`, `calendar.events`, `contacts.readonly`, `contacts.other.readonly` |

`POLL_SCOPES` stays a separate list so an account that granted only Gmail read
(every account connected before this change) keeps polling. `contacts.other.readonly`
is included because it covers addresses the user has merely corresponded with,
which is where most lookups land.

### Flow changes

- `auth.py` (`LOGIN_SCOPES`) and `pollers/gmail/auth.py` (`SCOPES`) both import the
  union from `google_scopes.py`. The `gmail.readonly` membership check in
  `complete_login` is unchanged: it decides whether a `source_connections` row is
  written at all.
- Both `authorization_url` calls add `include_granted_scopes="true"` so a
  re-consent extends the existing grant instead of replacing it.
- `gmail_auth` accepts an optional `login_hint` query parameter and forwards it to
  `authorization_url`, so "Grant agent access" on one account preselects that
  account at Google. `prompt="select_account consent"` is unchanged.
- `OAUTHLIB_RELAX_TOKEN_SCOPE=1` is already set at import in `auth.py`; the reconnect
  flow relies on it too, so it moves to `google_scopes.py` where both importers see
  it. A user who unticks a box at consent gets a token with fewer scopes rather than
  an exception.

### Credential helper

`get_gmail_service` in `pollers/gmail/auth.py` contains the refresh-persist-or-clear
logic. That moves into a general helper in the same file:

```
get_google_credentials(conn, user_id, account_id=None) -> (Credentials, set[str] granted, str resolved_account_id)
```

- Builds `Credentials.from_authorized_user_info(row["credentials"], scopes=row["credentials"]["scopes"])`.
  **The scopes passed must be the stored, granted ones**, not the requested list:
  google-auth raises `RefreshError` on refresh when the requested set is not a
  subset of what Google returns. Passing `SCOPES` (now wider than any old token)
  would break polling for every existing account.
- Refreshes and persists exactly as today; a `RefreshError` clears that account's
  row and raises, as today.
- `get_gmail_service` becomes `build("gmail", "v1", credentials=creds)` on top of it.

### Settings

`GET /settings` adds `agent_access: bool` per Gmail account, true when the stored
scopes contain every entry of `AGENT_SCOPES`. `static/js/app.js` renders, per
account, "Agent access: granted" or "Agent access not granted · Grant", where
Grant navigates to `/settings/sources/gmail/auth?login_hint=<email>`. Any edit to
`app.js` bumps `VERSION` in `static/js/sw.js`.

### Deployment note

`gmail.modify` and `drive` are restricted scopes; leaving Testing mode on the Google
Cloud project will require verification. This is a deployment fact and does not
affect the local POC, where the existing 7-day refresh expiry already applies.

## 2. The tool server: `agent/google_mcp/`

A stdio MCP server built on the `mcp` 2.x package already in the venv
(`from mcp.server.mcpserver import MCPServer`; there is no FastMCP in 2.x).

### Files

- `agent/google_mcp/__main__.py` — entrypoint. Loads `.env` from the current
  directory (the repo root, set by Hermes' `cwd`), reads the binding, registers
  tools, serves stdio.
- `agent/google_mcp/services.py` — resolves an account and builds the Google API
  clients (gmail v1, drive v3, docs v1, sheets v4, calendar v3, people v1) from
  `get_google_credentials`, cached per account for the life of the process.
  Exposes `granted(account) -> set[str]` and `resolve_account(account) -> str`.
- `agent/google_mcp/tools.py` — the tool functions as plain Python. Each takes the
  services object plus its arguments and returns a string (or a JSON-serialisable
  dict). No MCP types here, so the `agents_sdk` executor can wrap the same
  functions later.

### Binding

Three environment variables, set by the runner:

| Variable | Meaning |
|---|---|
| `AIB_USER_ID` | the user whose accounts the server may read |
| `AIB_ACCOUNT_ID` | the todo's Gmail address; may be empty for todos that predate multi-account |
| `AIB_DB_PATH` | absolute path of the SQLite database the app opened |

If `AIB_USER_ID` is unset, empty, or still the literal `${AIB_USER_ID}` (Hermes
expands `${VAR}` from the launching process and leaves unresolved references
verbatim), the server registers **no tools** and serves normally. This is the
case for every Hermes invocation that is not an Action Inbox turn; it must never
break them.

Account resolution for every tool: the explicit `account` argument if given, else
`AIB_ACCOUNT_ID`, else the user's first connected Gmail account (the same fallback
`get_gmail_service` uses). An `account` that is not one of the user's connected
addresses is an error, not a lookup.

### Tools

All tools take an optional `account: str` (a connected Gmail address).

| Tool | Arguments | Notes |
|---|---|---|
| `google_accounts` | — | each connected address, whether it has agent access, and which services it has granted |
| `gmail_search_threads` | `query`, `max_results=10` | Gmail query syntax; returns thread id, subject, from, date, snippet |
| `gmail_read_thread` | `thread_id` | every message: from, to, date, body text; same formatting as `agent/tools/email.py` |
| `gmail_create_draft` | `to`, `subject`, `body`, `cc=None`, `reply_to_thread_id=None` | returns draft id and the Gmail URL |
| `gmail_send` | same as draft | returns message id and thread id |
| `drive_search` | `query`, `max_results=10` | free text; uses `fullText contains` plus `name contains`; returns id, name, mimeType, modifiedTime, webViewLink |
| `drive_read_file` | `file_id` | Docs → text/plain, Sheets → CSV, Slides → text/plain, PDF → text via pypdf, `text/*` verbatim; other types return a message naming the type. Capped at 100 000 characters with a trailing note |
| `drive_upload_file` | `name`, `content`, `mime_type="text/plain"`, `folder_id=None` | returns id and webViewLink |
| `docs_create` | `title`, `body_text=""` | returns document id and URL |
| `docs_append` | `document_id`, `text` | appends at end of body |
| `docs_replace_text` | `document_id`, `find`, `replace` | `replaceAllText`, case-sensitive; returns count |
| `sheets_read_range` | `spreadsheet_id`, `range` | A1 notation; returns rows |
| `sheets_write_range` | `spreadsheet_id`, `range`, `values` | `USER_ENTERED` |
| `sheets_append_rows` | `spreadsheet_id`, `range`, `rows` | `USER_ENTERED`, `INSERT_ROWS` |
| `calendar_list_events` | `time_min`, `time_max`, `query=None`, `calendar_id="primary"` | RFC 3339 bounds; returns id, summary, start, end, attendees, htmlLink |
| `calendar_create_event` | `summary`, `start`, `end`, `attendees=None`, `description=None`, `location=None`, `calendar_id="primary"` | `sendUpdates="all"` when attendees are given; returns id and htmlLink |
| `contacts_search` | `query`, `max_results=10` | People API `searchContacts` then `otherContacts.search`; returns name and email addresses |

Reply threading: when `reply_to_thread_id` is given, the draft or message sets
`threadId`, and `In-Reply-To` / `References` from the thread's last message's
`Message-ID`. The subject defaults to the thread's subject with `Re:` when not
provided.

### Errors

Every tool catches `googleapiclient.errors.HttpError`, `RefreshError`, and the
`RuntimeError` the credential helper raises, and returns a one-line string
beginning `Error:`. Nothing raises across the MCP boundary, so Hermes sees an
ordinary tool result and its loop guardrails work as designed.

Scope gating happens before the API call: a tool whose service scope is missing
from the account's granted set returns
`Error: <account> has not granted <Service> access. The user can grant it from
Settings → Gmail → Grant agent access.` — text the agent can relay verbatim.

Logging is stderr only. Stdout is the protocol channel; Hermes captures a stdio
server's stderr into its MCP log.

## 3. Runner wiring and registration

### `agent/hermes_runner.py`

`_run` gains `user_id` and `account_id` parameters (passed from `resolve`, which
already has both). It sets `AIB_USER_ID`, `AIB_ACCOUNT_ID` (empty string when the
todo has none) and `AIB_DB_PATH` (`os.path.abspath(agent.db.DB_PATH)`) on the
subprocess environment, unless `HERMES_GOOGLE_TOOLS` is `0`/`false`/`no`. That
variable is the off switch: with the binding absent the registered server serves
no tools, and nothing in `~/.hermes/config.yaml` has to change.

### `scripts/install_hermes_google_mcp.py`

One-time, idempotent. Writes this entry into `~/.hermes/config.yaml` (honouring
`HERMES_HOME`), replacing any existing `action_inbox_google` entry:

```yaml
mcp_servers:
  action_inbox_google:
    command: /abs/path/to/venv/bin/python
    args: ["-m", "agent.google_mcp"]
    cwd: /abs/path/to/action_inbox_ai
    env:
      AIB_USER_ID: "${AIB_USER_ID}"
      AIB_ACCOUNT_ID: "${AIB_ACCOUNT_ID}"
      AIB_DB_PATH: "${AIB_DB_PATH}"
```

`command` is `sys.executable` of the interpreter running the script, so it is run
from the venv. `--remove` deletes the entry. Uses PyYAML (`pyyaml>=6` added to
`requirements.txt`); Hermes' own config writes already drop YAML comments, so this
script doing the same costs nothing new. The script prints the entry it wrote and
the `hermes mcp test action_inbox_google` command to verify it.

Hermes facts this relies on, verified against the installed 0.21.3 source:
`mcp_servers.<name>.env` values are `${VAR}`-expanded from the process environment
at config load; stdio entries accept `cwd`; single-query mode (`-q`, which the
runner uses) waits up to `mcp_single_query_discovery_timeout` (15s) for servers to
come up, which covers Python startup plus the Google client imports.

## 4. Prompt

`agent/hermes_prompt.py` `INSTRUCTIONS`, in step 2, replaces the sentence that says
to search mail with "your email tools" with a short paragraph:

> The `gmail_*`, `drive_*`, `docs_*`, `sheets_*`, `calendar_*` and `contacts_*`
> tools are the user's own Google account over the API, on the account this todo
> came from unless you pass another connected one. Use them for anything in
> Gmail, Drive, Docs, Sheets, Calendar or Contacts — reading, searching, drafting,
> sending, creating — instead of the browser. The browser is for everything else.
> Sending a message, creating an event, and writing to a document are irreversible
> acts under rule 4: a reply the user asked for is authorised; one whose content
> you inferred goes to `gmail_create_draft` and a question, never `gmail_send`.

`FOLLOWUP_INSTRUCTIONS` gets one sentence: "Google is reached through the
`gmail_*`/`drive_*`/… tools, not the browser." No other prompt text changes; the
`from_suggestion` framing and login-handoff paragraphs are untouched.

## 5. Testing and documentation

`scripts/verify/verify_google_mcp.py`, plain `python`, no API spend, in the style
of the existing verify scripts. It stubs the credential helper and the Google
clients and asserts:

- no binding (unset, empty, or literal `${AIB_USER_ID}`) → zero tools registered;
- account resolution order: explicit → `AIB_ACCOUNT_ID` → first connected; an
  unknown explicit account is an error string;
- a missing scope returns the Settings-pointing error before any client call;
- `gmail_send` / `gmail_create_draft` with `reply_to_thread_id` set `threadId`,
  `In-Reply-To`, `References`, and the `Re:` subject;
- `drive_read_file` picks the right export MIME type per Google type and caps
  output at 100 000 characters;
- every tool returns a string starting `Error:` when the client raises `HttpError`.

Manual verification: run the install script; `hermes mcp test action_inbox_google`
with the three variables exported; then one live todo whose resolution is a Gmail
reply, confirming the live trace shows `gmail_send` and no browser calls.

Documentation: a bullet in CLAUDE.md's Hermes list describing the server, the
binding, the off switch, and the install script; `.env.example` entries for
`HERMES_GOOGLE_TOOLS`; the "Auth and per-user credentials" section updated to say
the grant is now Workspace-wide and per-account agent access is visible in
Settings.

## Out of scope

- Wiring the same tool functions into the `agents_sdk` executor (shaped for it;
  small follow-up).
- Drive sharing, calendar event update/delete, Gmail label management.
- A Hermes-side per-run config (`HERMES_CONFIG_PATH`); the one-time registration
  plus environment binding is simpler and leaves the user's config intact.
