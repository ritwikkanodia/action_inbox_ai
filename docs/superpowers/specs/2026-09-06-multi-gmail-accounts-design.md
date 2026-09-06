# Multiple Gmail accounts per user

**Date:** 2026-09-06
**Status:** Approved design, not yet implemented

## Problem

A user signs in with one Google account, and that same account is the only
mailbox the poller can read. `source_connections` has primary key
`(user_id, source)`, so authorizing a second Gmail account silently
**overwrites** the first — the user loses polling on the original mailbox
with no warning.

Separately, the Gmail deep link on each todo is already wrong. `save_todo`
(`db.py:709`) builds:

```
https://mail.google.com/mail/u/0/?authuser=<email>#all/<thread_id>
```

The `/u/0/` segment pins Gmail to browser profile index 0, which overrides
the `?authuser=` hint. Today the link opens whichever Google account happens
to be first in the browser, not the account the mail arrived in. This is a
bug independent of multi-account support, and the fix is a precondition for
it: routing to the right account is the whole point of the feature.

## Goals

- A user can connect N Gmail accounts from Settings, and all of them poll.
- Each Gmail todo links to the thread **in the account it came from**.
- Each Gmail todo displays which account it came from.
- One account failing (revoked token, expired refresh) does not disturb the
  others.
- Disconnecting an account stops its polling and keeps its todos.

## Non-goals

- Multi-connection for other sources. Fathom stays one API key per user.
  The schema generalizes so this is possible later, but no Fathom UI or
  poller changes are in scope.
- Filtering the todo list by account. Attribution is display-only.
- Changing which account the user signs in with, or supporting more than
  one sign-in identity. Sign-in stays single; only *data* connections fan out.
- Preserving the existing Gmail connection across the schema change. See
  "Migration" — it is dropped deliberately.

## Chosen approach

Add an `account_id` dimension to the existing `source_connections` table
rather than introducing a Gmail-specific credential store.

Two alternatives were considered and rejected:

- **A dedicated `gmail_accounts` table.** Avoids rebuilding a table other
  sources read, and makes cursors typed columns instead of stringly-keyed
  state. Rejected because it creates a second credential store, contradicting
  the documented invariant in CLAUDE.md that every credential lives in
  `source_connections`.
- **A list of accounts inside the existing `credentials` JSON blob.** No
  migration at all. Rejected because it loses per-account row uniqueness,
  makes "which accounts exist" unqueryable, and lets concurrent OAuth
  callbacks clobber each other.

The chosen approach mirrors the move this repo already made when it went
multi-user; the rebuild-and-copy migration at `db.py:218` is the structural
pattern to follow, minus the copy.

## Data model

### `source_connections`

Rebuild with a new column and a wider primary key:

```sql
CREATE TABLE source_connections (
    user_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    account_id    TEXT NOT NULL DEFAULT '',
    auth_type     TEXT NOT NULL CHECK (auth_type IN ('api_key','oauth2')),
    credentials   TEXT NOT NULL,
    connected_at  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, source, account_id)
);
```

`account_id` is the lowercased Gmail address for Gmail rows, and `''` for
single-connection sources such as Fathom. Using the address as the key —
rather than a synthetic id — means the value the deep link needs is the same
value the row is keyed by, with no extra lookup.

### `todos`

Add a nullable `account_id TEXT` column via the existing idempotent
`ALTER TABLE` migration style.

Existing Gmail todos get `NULL`, not a backfilled guess. `NULL` is the honest
value: before this change only one Gmail account could exist, but nothing in
the row records *which* address it was, and the sole existing connection has
no recorded address at all. `NULL` reads as "unknown" and falls back to
current link behaviour.

### Poll cursors

Cursor keys in `user_state` become account-namespaced:

| Today | After |
|---|---|
| `history_id` | `gmail:<email>:history_id` |
| `gmail_backfill_pending` | `gmail:<email>:backfill_pending` |
| `gmail_backfilled_email` | *removed* — the account identity is now in the key |

The `gmail_backfilled_email` key is replaced by a per-account
`gmail:<email>:backfilled` marker, preserving the existing "reconnect of a
known account skips backfill" behaviour in `app.py:553-563` per account.

### Migration: drop the existing Gmail connection

The migration **deletes** every Gmail row from `source_connections` and the
legacy `history_id` / `gmail_backfill_pending` / `gmail_backfilled_email`
keys from `user_state`. Users reconnect Gmail once from Settings.

This is a deliberate trade, valid because the product has no users yet. The
only existing Gmail connection is the developer's, and — verified directly
against `gmail_events.db` — its stored credentials have **no
`connected_email`**, while `user_state` holds a bare `history_id` and no
backfill keys. Since the account's address was never recorded, there is no
way to derive the account-namespaced cursor key for it. Preserving that row
would require the poller to learn the address on its first `getProfile` call,
then rewrite the row and rename the cursor key mid-flight — the most
intricate code in the feature, written to serve exactly one row that a single
click can recreate.

Dropping it removes the copy-with-`json_extract` logic, the cursor-key rename,
the adopt-on-first-poll path, and the `account_id = ''` collision guard in the
OAuth callback. The cost is one re-auth and a 3-day backfill, both acceptable.

Fathom rows are **not** dropped — they migrate across to `account_id = ''`
untouched, since the rebuild is the only reason they move at all.

Existing todos are not touched by the migration. The one existing Gmail todo
keeps `account_id = NULL` and uses the fallback link and fallback account.

### Dedup — deliberately unchanged

`dedup_key` stays the bare `message_id`, and the unique partial index stays
`(user_id, source, dedup_key)`.

Account-scoping the key (`<account>:<message_id>`) was considered and
rejected: it would make every already-seen message look new, regenerating
todos for the entire existing corpus on first poll.

The failure mode this leaves open — one email delivered to two connected
accounts, producing two different `message_id`s and therefore two todos — is
already absorbed by the 0.80 title-similarity check in `db.py:704`, which
compares against all Gmail todos for the user from the last two days
regardless of account.

## Auth and polling

`get_gmail_service(conn, user_id)` (`pollers/gmail/auth.py:49`) becomes
`get_gmail_service(conn, user_id, account_id)`. A new
`list_gmail_accounts(conn, user_id)` in `db.py` returns the connected
addresses.

The `RefreshError` handler is the sensitive part. It currently calls
`clear_source_connection(conn, user_id, "gmail")`, which under multi-account
would disconnect **every** account because one token was revoked. It must
become account-scoped. The clear-and-reprompt behaviour itself is intended
and stays — CLAUDE.md calls this out explicitly — but it must apply only to
the account whose token Google rejected.

`clear_source_connection(conn, user_id, source)` gains an optional
`account_id`; omitting it clears every account for that source, which is what
the Fathom callers want and what a "disconnect Gmail entirely" action would
want.

`_poll_gmail_for_user` (`main.py:62`) loops over `list_gmail_accounts`, each
account in its own `try/except`, mirroring the per-source isolation already
in `main()`. Log lines already prefix with the account address, so per-account
output stays readable without change.

`list_active_users` (`db.py:375`) needs no change — it tests
`EXISTS (... WHERE sc.user_id = ...)`, which is unaffected by the wider key.

### OAuth flow

`gmail_auth` (`app.py:507`) must request `prompt="select_account consent"`.
It currently sends `consent` alone, which can silently reuse the already
signed-in Google account — making "add another account" impossible to
perform from the browser even once the backend supports it.

`gmail_callback` (`app.py:521`) keys the write by the address returned from
the profile call, so re-authorizing an already-connected account updates that
row in place rather than creating a duplicate. The address is now **required**:
where the current code swallows a failed profile call and saves the row anyway
(`app.py:546`), it must instead refuse to write and surface an error, since a
row with no address cannot be keyed, linked, or polled per-account. The same
applies to the login-time credential save in `auth.py:158`.

That login-time save writes under the sign-in address, making the sign-in
account the first connected account naturally.

## Links, UI, and the agent

### Deep link

```
https://mail.google.com/mail/u/<email>/#all/<thread_id>
```

Gmail accepts an address in the `u/` slot and resolves it to the right
profile. This replaces the broken `/u/0/?authuser=` form at `db.py:709`.
Where `account_id` is `NULL`, fall back to the existing `/u/0/#all/<thread_id>`
form — the same behaviour as today, no worse.

The same fix applies to the fallback URL at `app.py:391`.

### Settings

`GET /settings` returns Gmail as a list:

```json
"gmail": {
  "accounts": [{"email": "...", "connected_at": "..."}],
  "auth_url": "/settings/sources/gmail/auth"
}
```

`POST /settings/sources/gmail` accepts an `account_id` for per-account
disconnect. The settings modal in `templates/index.html:152` renders one row
per connected account with its own Disconnect button, plus a persistent
"Connect another account" button.

The not-connected banner at `templates/index.html:24` keys off "zero
accounts" rather than "no connection row".

### Todo attribution

Each Gmail todo row shows an account badge. Display-only — no account filter,
and no change to the existing source filter or sort order. A todo with
`NULL` `account_id` shows no badge.

### Agent and thread context

`gmail_tools(user_id)` (`agent/tools/email.py:59`) becomes
`gmail_tools(user_id, account_id)`, so `search_email_threads` and
`fetch_email_thread` search the mailbox the todo actually came from instead of
an arbitrary account. `_build_agent` (`agent/resolver.py:11`) and
`resolve_todo` thread the todo's `account_id` through.

`fetch_gmail_thread_context` (`agent/tools/email.py:43`) and the
`/todos/<id>/context` route (`app.py:375`) likewise select the service by the
todo's `account_id`.

For a todo with `NULL` `account_id`, all of these fall back to the user's
first connected account.

### Disconnected accounts

Per the approved design, disconnecting keeps the todos. Their thread pane and
AI email tools will fail, because the credentials are gone. That failure
surfaces through the existing error paths: `/todos/<id>/context` already
returns `{"error": "Couldn't load thread: ..."}` on exception, and the agent
tools already log and degrade. No new "stale account" UI is in scope.

## Testing

This repo has no test suite, no linter, and no CI. Per CLAUDE.md, verification
is running both processes and exercising the UI. The checks that matter:

1. **Migration lands cleanly.** Run `init_db` against a copy of
   `gmail_events.db`. Confirm `source_connections` has the new shape, the
   Gmail row and legacy cursor keys are gone, the Fathom row (if any)
   survived, and all 23 todos are intact.
2. **Reconnect works.** Connect Gmail from Settings; confirm the row is keyed
   by the real address and a backfill runs.
3. **Two accounts poll independently.** Connect a second account; confirm the
   log shows both with separate history cursors, and that only the new one
   backfills.
4. **Links route correctly.** A todo from account B opens the thread in
   account B, in a browser signed into both.
5. **Isolation on failure.** Revoke account B's token; confirm B flips to
   not-connected and A keeps polling.
6. **Disconnect.** Disconnect B; confirm A still polls and B's todos remain.
7. **Legacy todo.** The existing `NULL`-account Gmail todo still opens and
   still loads its thread pane via the fallback account.

## Risks

- **The migration is destructive by design.** It drops Gmail credentials.
  That is the accepted trade above, but it means the change cannot ship after
  real users exist without revisiting this decision — anyone connected at
  deploy time is silently signed out of Gmail until they reconnect. If users
  arrive before this ships, the migration must be reconsidered.
- **The `source_connections` rebuild touches every stored credential**,
  including Fathom's. Mitigation: follow the proven pattern at `db.py:218`
  and verify against a database copy first.
- **Requiring the profile call to succeed** makes OAuth stricter than today.
  A transient Google failure now blocks connecting rather than silently
  saving an unusable row. This is intended, but the error surfaced to the
  user must say "try again" rather than fail opaquely.
