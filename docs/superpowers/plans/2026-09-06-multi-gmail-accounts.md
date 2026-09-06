# Multi-Gmail-Account Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one signed-in user connect multiple Gmail accounts, poll them all independently, and route every Gmail todo's link, thread pane, and AI tools back to the account it came from.

**Architecture:** Add an `account_id` dimension to `source_connections` (primary key becomes `(user_id, source, account_id)`), where `account_id` is the lowercased Gmail address. Per-account poll cursors live in `user_state` under `gmail:<email>:*` keys. `todos.account_id` records provenance, driving both the deep link and the account badge. Existing Gmail connections are dropped by the migration — users reconnect once.

**Tech Stack:** Python 3, SQLite (stdlib `sqlite3`), Flask, `google-auth-oauthlib` / `google-api-python-client`, OpenAI Agents SDK, vanilla JS + Jinja templates.

**Spec:** `docs/superpowers/specs/2026-09-06-multi-gmail-accounts-design.md`

## Global Constraints

- **`account_id` is always a lowercased email address for Gmail rows, `''` for all other sources.** Never `NULL` in `source_connections`; `NULL` only ever appears in `todos.account_id`, meaning "provenance unknown".
- **`dedup_key` stays the bare `message_id`.** Do not account-scope it — doing so regenerates todos for the entire existing corpus.
- **Every OpenAI call in this repo uses `gpt-5.4-mini`.** No task here changes a model id.
- **Enums are enforced in both SQL `CHECK` constraints and Python.** `db.py` migrated databases can't gain constraints, so keep both in sync.
- **Config comes from `.env` via `load_dotenv(override=True)`** at the top of `main.py` and `app.py`, before any other import.
- **Run everything from the venv:** `source venv/bin/activate` first.

## Verification approach — read before starting

This repo has **no test suite, no pytest, no linter, and no CI**, and CLAUDE.md directs contributors to verify by running the two processes and exercising the UI. This plan does **not** introduce pytest, which would be an unrequested dependency and a departure from the documented project conventions.

Instead, the risky, pure-Python work — the migration and the `db.py` helpers — is gated by **standalone verification scripts** run with plain `python`. They are real automated assertions, just without a framework. They live in `scripts/verify/` and are committed, because the migration is destructive and worth being able to re-run.

The Flask, OAuth, poller, and UI tasks cannot be meaningfully automated without heavy mocking of Google's API, so those tasks end in **explicit manual verification steps** with exact commands and expected output, per CLAUDE.md.

**Before Task 1, take a database backup.** The migration deletes Gmail credentials:

```bash
cp gmail_events.db gmail_events.db.pre-multiaccount.bak
```

---

### Task 1: Schema and destructive migration

**Files:**
- Modify: `db.py:77-86` (fresh-schema `source_connections`), `db.py:96-115` (fresh-schema `todos`), `db.py:194-238` (migration block)
- Create: `scripts/verify/verify_migration.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `source_connections` with columns `(user_id, source, account_id, auth_type, credentials, connected_at, updated_at)` and PK `(user_id, source, account_id)`; `todos.account_id TEXT` nullable. Every later task depends on this shape.

- [ ] **Step 1: Write the verification script**

Create `scripts/verify/verify_migration.py`:

```python
"""Verifies the multi-account migration against a throwaway copy of a database.

Usage: python scripts/verify/verify_migration.py [source_db]

Builds a pre-migration-shaped database (or copies `source_db`), runs init_db,
and asserts the post-migration invariants. Never touches the real database.
"""
import os
import sqlite3
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import init_db


def build_legacy_db(path: str) -> None:
    """Create a database in the pre-multi-account shape with one Gmail row."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
            picture_url TEXT, created_at TEXT NOT NULL, last_login_at TEXT
        );
        CREATE TABLE user_state (
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE source_connections (
            user_id TEXT NOT NULL, source TEXT NOT NULL, auth_type TEXT NOT NULL,
            credentials TEXT NOT NULL, connected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (user_id, source)
        );
        CREATE TABLE todos (
            todo_id TEXT PRIMARY KEY, user_id TEXT, source TEXT NOT NULL,
            dedup_key TEXT, title TEXT, suggested_action TEXT, importance TEXT,
            estimated_time_minutes INTEGER, due_date TEXT, relevant_link TEXT,
            reasoning TEXT, status TEXT NOT NULL DEFAULT 'open', decision TEXT,
            ai_thread TEXT, source_meta TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO users VALUES ('u1','dev@example.com','Dev',NULL,'2026-01-01',NULL);
        INSERT INTO source_connections VALUES
            ('u1','gmail','oauth2','{"token":"x"}','2026-01-01','2026-01-01'),
            ('u1','fathom','api_key','{"api_key":"k"}','2026-01-01','2026-01-01');
        INSERT INTO user_state VALUES
            ('u1','history_id','34079'),
            ('u1','gmail_backfilled_email','dev@example.com'),
            ('u1','system_snapshot_hash','abc');
        INSERT INTO todos (todo_id,user_id,source,dedup_key,title,status,created_at,updated_at)
        VALUES ('t1','u1','gmail','m1','Existing gmail todo','open','2026-01-01','2026-01-01'),
               ('t2','u1','user',NULL,'Manual todo','open','2026-01-01','2026-01-01');
    """)
    conn.commit()
    conn.close()


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "verify.db")
    source = sys.argv[1] if len(sys.argv) > 1 else None
    if source:
        shutil.copy(source, path)
        print(f"Using copy of {source}")
    else:
        build_legacy_db(path)
        print("Using synthetic legacy database")

    conn = sqlite3.connect(path)
    todos_before = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    init_db(conn)

    sc_cols = [r[1] for r in conn.execute("PRAGMA table_info(source_connections)")]
    check("source_connections has account_id", "account_id" in sc_cols)

    pk = [r[1] for r in conn.execute("PRAGMA table_info(source_connections)") if r[5]]
    check("PK is (user_id, source, account_id)",
          set(pk) == {"user_id", "source", "account_id"})

    gmail_rows = conn.execute(
        "SELECT COUNT(*) FROM source_connections WHERE source = 'gmail'"
    ).fetchone()[0]
    check("gmail connections dropped", gmail_rows == 0)

    fathom = conn.execute(
        "SELECT account_id FROM source_connections WHERE source = 'fathom'"
    ).fetchall()
    check("fathom rows survive with account_id=''",
          all(r[0] == "" for r in fathom))

    legacy_keys = conn.execute(
        "SELECT COUNT(*) FROM user_state WHERE key IN "
        "('history_id','gmail_backfill_pending','gmail_backfilled_email')"
    ).fetchone()[0]
    check("legacy gmail cursor keys dropped", legacy_keys == 0)

    other_state = conn.execute(
        "SELECT COUNT(*) FROM user_state WHERE key = 'system_snapshot_hash'"
    ).fetchone()[0]
    check("non-gmail user_state preserved", other_state >= 0)

    todo_cols = [r[1] for r in conn.execute("PRAGMA table_info(todos)")]
    check("todos has account_id", "account_id" in todo_cols)

    todos_after = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    check(f"todos preserved ({todos_before} -> {todos_after})",
          todos_after == todos_before)

    null_accounts = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE source='gmail' AND account_id IS NOT NULL"
    ).fetchone()[0]
    check("existing gmail todos have NULL account_id", null_accounts == 0)

    init_db(conn)
    print("PASS  init_db is idempotent (second run clean)")
    conn.close()
    shutil.rmtree(tmpdir)
    print("\nAll migration checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
source venv/bin/activate && python scripts/verify/verify_migration.py
```

Expected: `FAIL  source_connections has account_id` and exit code 1.

- [ ] **Step 3: Update the fresh schema in `db.py`**

In the `conn.executescript` block, replace the `source_connections` definition (`db.py:77-86`) with:

```sql
        CREATE TABLE IF NOT EXISTS source_connections (
            user_id       TEXT NOT NULL,
            source        TEXT NOT NULL,
            account_id    TEXT NOT NULL DEFAULT '',
            auth_type     TEXT NOT NULL
                              CHECK (auth_type IN ('api_key','oauth2')),
            credentials   TEXT NOT NULL,
            connected_at  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (user_id, source, account_id)
        );
```

In the same script block, add `account_id TEXT,` to the `todos` table definition immediately after the `source` column's `CHECK` clause.

- [ ] **Step 4: Add the todos column migration**

Next to the existing `if "user_id" not in todo_cols:` guard (`db.py:191-193`), add:

```python
    if "account_id" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN account_id TEXT")
```

- [ ] **Step 5: Replace the `source_connections` rebuild**

First, **split the rebuild flag in two.** `needs_sc_rebuild` currently means
"this database predates multi-user" and drives the legacy-user backfill. If you
simply repoint it at `account_id`, every current database looks legacy and
`upsert_user(conn, _legacy_user_email())` creates a spurious user row. Replace
`db.py:195` (`needs_sc_rebuild = "user_id" not in sc_cols`) with two flags:

```python
    # Pre-multi-user databases: source_connections has no user_id at all.
    needs_user_id_backfill = "user_id" not in sc_cols
    # Pre-multi-account databases: the rebuild this migration performs.
    needs_sc_rebuild = "account_id" not in sc_cols
```

Then update the legacy-rows probe just below it to use the *user_id* flag, so
it keeps its original meaning:

```python
    has_legacy_sc_rows = needs_user_id_backfill and bool(
        conn.execute("SELECT 1 FROM source_connections LIMIT 1").fetchone()
    )
```

Leave `needs_backfill = has_orphan_todos or has_legacy_sc_rows or has_legacy_state`
exactly as it is — with `has_legacy_sc_rows` now correctly scoped, it no longer
misfires on current databases.

Now replace the `if needs_sc_rebuild:` block (`db.py:216-238`) with a rebuild
that deliberately **excludes** Gmail rows:

```python
    # `account_id` widens the primary key so one user can connect several
    # accounts per source. Gmail rows are deliberately NOT carried across:
    # the only pre-existing connection has no recorded address, so its
    # account-namespaced cursor key can't be derived. Users reconnect once.
    # See docs/superpowers/specs/2026-09-06-multi-gmail-accounts-design.md.
    if needs_sc_rebuild:
        conn.execute("""
            CREATE TABLE source_connections_new (
                user_id       TEXT NOT NULL,
                source        TEXT NOT NULL,
                account_id    TEXT NOT NULL DEFAULT '',
                auth_type     TEXT NOT NULL
                                  CHECK (auth_type IN ('api_key','oauth2')),
                credentials   TEXT NOT NULL,
                connected_at  TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (user_id, source, account_id)
            )
        """)
        if needs_user_id_backfill:
            # Very old database: stamp the legacy user onto every row.
            conn.execute(
                "INSERT INTO source_connections_new "
                "(user_id, source, account_id, auth_type, credentials, connected_at, updated_at) "
                "SELECT ?, source, '', auth_type, credentials, connected_at, updated_at "
                "FROM source_connections WHERE source != 'gmail'",
                (legacy_user_id,),
            )
        else:
            conn.execute(
                "INSERT INTO source_connections_new "
                "(user_id, source, account_id, auth_type, credentials, connected_at, updated_at) "
                "SELECT user_id, source, '', auth_type, credentials, connected_at, updated_at "
                "FROM source_connections WHERE source != 'gmail'"
            )
        conn.execute("DROP TABLE source_connections")
        conn.execute("ALTER TABLE source_connections_new RENAME TO source_connections")
        # Legacy single-account Gmail cursors are meaningless now that cursors
        # are namespaced per account. Dropping them makes the next poll after
        # reconnect start from a clean backfill.
        conn.execute(
            "DELETE FROM user_state WHERE key IN "
            "('history_id', 'gmail_backfill_pending', 'gmail_backfilled_email')"
        )
```

- [ ] **Step 6: Run the verification script against the synthetic database**

```bash
source venv/bin/activate && python scripts/verify/verify_migration.py
```

Expected: every line `PASS`, ending in `All migration checks passed.`

- [ ] **Step 7: Run it against a copy of the real database**

```bash
source venv/bin/activate && python scripts/verify/verify_migration.py gmail_events.db
```

Expected: all `PASS`, with `todos preserved (23 -> 23)`. The real `gmail_events.db` is untouched — the script copies it.

- [ ] **Step 8: Commit**

```bash
git add db.py scripts/verify/verify_migration.py
git commit -m "feat: add account_id dimension to source_connections and todos

Widens the source_connections primary key to (user_id, source, account_id)
so one user can connect several Gmail accounts. Existing Gmail rows and
their legacy cursor keys are dropped: the only such row has no recorded
address, so its account-namespaced cursor key cannot be derived. Users
reconnect once.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Account-aware connection helpers

**Files:**
- Modify: `db.py:434-486` (`get_source_connection`, `set_source_credentials`, `clear_source_connection`)
- Create: `scripts/verify/verify_connections.py`

**Interfaces:**
- Consumes: the schema from Task 1.
- Produces:
  - `get_source_connection(conn, user_id, source, account_id=None) -> dict | None` — with `account_id=None`, returns the earliest-connected row for that source. Result dict gains an `"account_id"` key.
  - `set_source_credentials(conn, user_id, source, auth_type, credentials, account_id="") -> None`
  - `clear_source_connection(conn, user_id, source, account_id=None) -> None` — `None` clears every account for that source.
  - `list_gmail_accounts(conn, user_id) -> list[dict]` — `[{"account_id": str, "connected_at": str}]`, ordered by `connected_at` ascending.

- [ ] **Step 1: Write the verification script**

Create `scripts/verify/verify_connections.py`:

```python
"""Verifies account-aware source_connections helpers."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import (
    init_db,
    upsert_user,
    get_source_connection,
    set_source_credentials,
    clear_source_connection,
    list_gmail_accounts,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "a"}, account_id="a@example.com")
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "b"}, account_id="b@example.com")

    accounts = list_gmail_accounts(conn, user_id)
    check("two gmail accounts listed", len(accounts) == 2)
    check("accounts carry addresses",
          {a["account_id"] for a in accounts} == {"a@example.com", "b@example.com"})

    row_a = get_source_connection(conn, user_id, "gmail", "a@example.com")
    check("account a fetched by id", row_a["credentials"]["token"] == "a")
    check("result carries account_id", row_a["account_id"] == "a@example.com")

    # Second connect must not overwrite the first.
    row_b = get_source_connection(conn, user_id, "gmail", "b@example.com")
    check("account b independent", row_b["credentials"]["token"] == "b")

    # Re-authorizing an existing account updates in place, no duplicate.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "a2"}, account_id="a@example.com")
    check("reconnect does not duplicate", len(list_gmail_accounts(conn, user_id)) == 2)
    check("reconnect updates credentials",
          get_source_connection(conn, user_id, "gmail", "a@example.com")
          ["credentials"]["token"] == "a2")

    # account_id=None returns *a* row rather than failing (legacy fallback).
    check("None account_id falls back to first",
          get_source_connection(conn, user_id, "gmail") is not None)

    # Fathom keeps working with the default empty account.
    set_source_credentials(conn, user_id, "fathom", "api_key", {"api_key": "k"})
    check("fathom stored under ''",
          get_source_connection(conn, user_id, "fathom")["account_id"] == "")

    # Scoped clear removes only that account.
    clear_source_connection(conn, user_id, "gmail", "a@example.com")
    check("scoped clear removes one", len(list_gmail_accounts(conn, user_id)) == 1)
    check("other account survives",
          get_source_connection(conn, user_id, "gmail", "b@example.com") is not None)
    check("fathom untouched by gmail clear",
          get_source_connection(conn, user_id, "fathom") is not None)

    # Unscoped clear removes all accounts for the source.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "c"}, account_id="c@example.com")
    clear_source_connection(conn, user_id, "gmail")
    check("unscoped clear removes all", list_gmail_accounts(conn, user_id) == [])
    check("fathom still untouched",
          get_source_connection(conn, user_id, "fathom") is not None)

    print("\nAll connection-helper checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
source venv/bin/activate && python scripts/verify/verify_connections.py
```

Expected: `ImportError: cannot import name 'list_gmail_accounts' from 'db'`.

- [ ] **Step 3: Rewrite the three helpers and add the lister**

Replace `get_source_connection`, `set_source_credentials`, and `clear_source_connection` in `db.py` with:

```python
def get_source_connection(
    conn: sqlite3.Connection,
    user_id: str,
    source: str,
    account_id: str | None = None,
) -> dict | None:
    """Fetch one connection. With account_id=None, returns the earliest-connected
    row for the source — the fallback used by legacy todos with no provenance."""
    sql = (
        "SELECT source, account_id, auth_type, credentials, connected_at, updated_at "
        "FROM source_connections WHERE user_id = ? AND source = ?"
    )
    params: tuple = (user_id, source)
    if account_id is not None:
        sql += " AND account_id = ?"
        params += (account_id,)
    sql += " ORDER BY connected_at ASC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return {
        "source": row[0],
        "account_id": row[1],
        "auth_type": row[2],
        "credentials": json.loads(row[3]),
        "connected_at": row[4],
        "updated_at": row[5],
    }


def list_gmail_accounts(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    """Every connected Gmail account for a user, oldest connection first."""
    rows = conn.execute(
        "SELECT account_id, connected_at FROM source_connections "
        "WHERE user_id = ? AND source = 'gmail' ORDER BY connected_at ASC",
        (user_id,),
    ).fetchall()
    return [{"account_id": r[0], "connected_at": r[1]} for r in rows]


def set_source_credentials(
    conn: sqlite3.Connection,
    user_id: str,
    source: str,
    auth_type: str,
    credentials: dict,
    account_id: str = "",
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO source_connections
            (user_id, source, account_id, auth_type, credentials, connected_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, source, account_id) DO UPDATE SET
            auth_type    = excluded.auth_type,
            credentials  = excluded.credentials,
            updated_at   = excluded.updated_at
        """,
        (user_id, source, account_id, auth_type, json.dumps(credentials), now, now),
    )
    conn.commit()


def clear_source_connection(
    conn: sqlite3.Connection,
    user_id: str,
    source: str,
    account_id: str | None = None,
) -> None:
    """Disconnect one account, or every account for the source when account_id
    is None."""
    sql = "DELETE FROM source_connections WHERE user_id = ? AND source = ?"
    params: tuple = (user_id, source)
    if account_id is not None:
        sql += " AND account_id = ?"
        params += (account_id,)
    conn.execute(sql, params)
    conn.commit()
```

- [ ] **Step 4: Run the verification script**

```bash
source venv/bin/activate && python scripts/verify/verify_connections.py
```

Expected: every line `PASS`.

- [ ] **Step 5: Re-run the migration script to confirm no regression**

```bash
source venv/bin/activate && python scripts/verify/verify_migration.py
```

Expected: all `PASS`.

- [ ] **Step 6: Commit**

```bash
git add db.py scripts/verify/verify_connections.py
git commit -m "feat: make source_connections helpers account-aware

get/set/clear now take an optional account_id, and list_gmail_accounts
enumerates a user's connected mailboxes. Clearing without an account_id
removes every account for the source, which is what the Fathom callers
and a full Gmail disconnect want.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Per-account deep link and todo provenance

**Files:**
- Modify: `db.py:684-728` (`save_todo`), `db.py:535-571` (`_save_todo`)
- Create: `scripts/verify/verify_links.py`

**Interfaces:**
- Consumes: `todos.account_id` from Task 1.
- Produces:
  - `gmail_thread_url(thread_id: str, account_id: str | None) -> str` in `db.py` — the single source of truth for Gmail deep links, imported by `app.py` in Task 9.
  - `save_todo(conn, event_id, message_id, thread_id, result, user_id, account_id="")` — the `gmail_email` parameter is renamed to `account_id`; callers in `main.py` already pass the address positionally, so Task 6 updates the call site.
  - `_save_todo(..., account_id: str | None = None)` — new keyword-only parameter.

- [ ] **Step 1: Write the verification script**

Create `scripts/verify/verify_links.py`:

```python
"""Verifies Gmail deep-link construction and todo account provenance."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import init_db, upsert_user, gmail_thread_url, save_todo


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    url = gmail_thread_url("t123", "dev@example.com")
    check("link targets the account mailbox",
          url == "https://mail.google.com/mail/u/dev@example.com/#all/t123")
    check("link does not pin profile index 0", "/u/0/" not in url)
    check("link does not use the broken authuser form", "authuser" not in url)

    fallback = gmail_thread_url("t123", None)
    check("None account falls back to index 0",
          fallback == "https://mail.google.com/mail/u/0/#all/t123")
    check("empty account falls back to index 0",
          gmail_thread_url("t123", "") == fallback)

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    result = {
        "should_generate_todo": True,
        "reasoning": "needs a reply",
        "todo": {"title": "Reply to Acme", "importance": "high"},
    }
    saved = save_todo(conn, "evt1", "m1", "t1", result, user_id, "a@example.com")
    check("todo saved", saved)

    row = conn.execute(
        "SELECT account_id, relevant_link FROM todos WHERE dedup_key = 'm1'"
    ).fetchone()
    check("account_id persisted", row[0] == "a@example.com")
    check("relevant_link targets that account",
          row[1] == "https://mail.google.com/mail/u/a@example.com/#all/t1")

    # A todo saved with no account keeps NULL provenance and the fallback link.
    result2 = {
        "should_generate_todo": True,
        "reasoning": "x",
        "todo": {"title": "Totally unrelated subject line here", "importance": "low"},
    }
    save_todo(conn, "evt2", "m2", "t2", result2, user_id, "")
    row2 = conn.execute(
        "SELECT account_id, relevant_link FROM todos WHERE dedup_key = 'm2'"
    ).fetchone()
    check("empty account stores NULL", row2[0] is None)
    check("empty account uses fallback link", "/u/0/" in row2[1])

    print("\nAll link and provenance checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
source venv/bin/activate && python scripts/verify/verify_links.py
```

Expected: `ImportError: cannot import name 'gmail_thread_url' from 'db'`.

- [ ] **Step 3: Add `gmail_thread_url` to `db.py`**

Add near the other module-level helpers, above `save_todo`:

```python
def gmail_thread_url(thread_id: str, account_id: str | None) -> str:
    """Deep link to a thread in a specific mailbox.

    Gmail resolves an email address in the `/u/` slot to the right profile.
    The older `/u/0/?authuser=<email>` form does not work: `/u/0/` pins profile
    index 0 and overrides the authuser hint, so links opened whichever account
    happened to be first in the browser.
    """
    if not account_id:
        return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
    return f"https://mail.google.com/mail/u/{account_id}/#all/{thread_id}"
```

- [ ] **Step 4: Add `account_id` to `_save_todo`**

Add `account_id: str | None = None,` to the keyword-only parameters of `_save_todo`, then update the INSERT to include the column:

```python
    conn.execute(
        """
        INSERT OR IGNORE INTO todos (
            todo_id, user_id, account_id, source, dedup_key, title, suggested_action,
            importance, estimated_time_minutes, due_date, relevant_link, reasoning,
            status, decision, source_meta, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
        """,
        (
            todo_id, user_id, account_id or None, source, dedup_key, title,
            suggested_action, importance, estimated_time_minutes, due_date,
            relevant_link, reasoning or "", decision,
            json.dumps(source_meta) if source_meta else None, now, now,
        ),
    )
```

- [ ] **Step 5: Update `save_todo`**

Rename the `gmail_email: str = ""` parameter to `account_id: str = ""`, and replace the link construction (`db.py:707-710`) with:

```python
    relevant_link = todo.get("relevant_link") or gmail_thread_url(thread_id, account_id)
```

Delete the now-dead `authuser = ...` line above it. Then pass the account through to `_save_todo` by adding `account_id=account_id or None,` to its keyword arguments.

- [ ] **Step 6: Run the verification script**

```bash
source venv/bin/activate && python scripts/verify/verify_links.py
```

Expected: every line `PASS`.

- [ ] **Step 7: Commit**

```bash
git add db.py scripts/verify/verify_links.py
git commit -m "fix: route Gmail deep links to the originating account

The link was built as /u/0/?authuser=<email>, where /u/0/ pins profile
index 0 and overrides the authuser hint — so todo links already opened
the wrong account. Replaces it with /u/<email>/, and records the source
account on the todo so the link can be built per account.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Account-scoped Gmail service and revocation isolation

**Files:**
- Modify: `pollers/gmail/auth.py:49-77` (`get_gmail_service`)

**Interfaces:**
- Consumes: `get_source_connection(..., account_id)` and `clear_source_connection(..., account_id)` from Task 2.
- Produces: `get_gmail_service(conn, user_id, account_id: str | None = None)`. With `account_id=None` it resolves the user's first connected account — the fallback path used by legacy todos in Tasks 9 and 10.

- [ ] **Step 1: Rewrite `get_gmail_service`**

Replace the function body in `pollers/gmail/auth.py` with:

```python
def get_gmail_service(
    conn: sqlite3.Connection, user_id: str, account_id: str | None = None
):
    """Build a Gmail client for one connected account.

    account_id=None resolves to the user's first connected account, which is
    the fallback for todos created before per-account provenance existed.
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
    creds = Credentials.from_authorized_user_info(row["credentials"], SCOPES)

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
            set_source_credentials(
                conn, user_id, "gmail", "oauth2",
                json.loads(creds.to_json()), account_id=resolved,
            )
        else:
            clear_source_connection(conn, user_id, "gmail", resolved)
            raise RuntimeError(
                f"Gmail credentials expired for {resolved or 'this account'}. "
                "Re-authorize via the settings page."
            )

    return build("gmail", "v1", credentials=creds)
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
source venv/bin/activate && python -c "from pollers.gmail.auth import get_gmail_service; import inspect; print(inspect.signature(get_gmail_service))"
```

Expected: `(conn: sqlite3.Connection, user_id: str, account_id: str | None = None)`

- [ ] **Step 3: Verify revocation clears only one account**

```bash
source venv/bin/activate && python - <<'PY'
import sqlite3
from db import init_db, upsert_user, set_source_credentials, list_gmail_accounts, clear_source_connection
conn = sqlite3.connect(":memory:"); init_db(conn)
uid, _ = upsert_user(conn, "dev@example.com")
set_source_credentials(conn, uid, "gmail", "oauth2", {"token": "a"}, account_id="a@x.com")
set_source_credentials(conn, uid, "gmail", "oauth2", {"token": "b"}, account_id="b@x.com")
clear_source_connection(conn, uid, "gmail", "a@x.com")
remaining = [a["account_id"] for a in list_gmail_accounts(conn, uid)]
assert remaining == ["b@x.com"], remaining
print("PASS  revoking one account leaves the other connected:", remaining)
PY
```

Expected: `PASS  revoking one account leaves the other connected: ['b@x.com']`

- [ ] **Step 4: Commit**

```bash
git add pollers/gmail/auth.py
git commit -m "fix: scope Gmail token revocation to a single account

get_gmail_service now takes an account_id, and the RefreshError path
clears only that account. Previously it cleared the whole gmail source,
which under multi-account would disconnect every mailbox because one
token was revoked.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Per-account poll cursors

**Files:**
- Modify: `pollers/gmail/poller.py:1-20` (imports and key constants), `pollers/gmail/poller.py:185-260` (`poll`), `db.py:495-510` (history-id wrappers)

**Interfaces:**
- Consumes: `get_user_state` / `set_user_state` / `clear_user_state` (unchanged).
- Produces:
  - `db.gmail_state_key(account_id: str, suffix: str) -> str` returning `f"gmail:{account_id}:{suffix}"`
  - `db.get_gmail_history_id(conn, user_id, account_id) -> str | None`
  - `db.set_gmail_history_id(conn, user_id, account_id, history_id) -> None`
  - `pollers.gmail.poller.poll(service, conn, user_id, account_id) -> list[GmailEvent]`
  - Module constants `BACKFILL_PENDING_SUFFIX = "backfill_pending"` and `BACKFILLED_SUFFIX = "backfilled"`, imported by `app.py` in Task 7.

- [ ] **Step 1: Write the verification script**

Create `scripts/verify/verify_cursors.py`:

```python
"""Verifies per-account cursor key namespacing."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import (
    init_db, upsert_user, gmail_state_key,
    get_gmail_history_id, set_gmail_history_id,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    check("key is namespaced by account",
          gmail_state_key("a@x.com", "history_id") == "gmail:a@x.com:history_id")

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")

    set_gmail_history_id(conn, uid, "a@x.com", "100")
    set_gmail_history_id(conn, uid, "b@x.com", "200")

    check("account a cursor isolated", get_gmail_history_id(conn, uid, "a@x.com") == "100")
    check("account b cursor isolated", get_gmail_history_id(conn, uid, "b@x.com") == "200")
    check("unknown account has no cursor",
          get_gmail_history_id(conn, uid, "c@x.com") is None)

    set_gmail_history_id(conn, uid, "a@x.com", "150")
    check("advancing a leaves b alone",
          (get_gmail_history_id(conn, uid, "a@x.com"),
           get_gmail_history_id(conn, uid, "b@x.com")) == ("150", "200"))

    print("\nAll cursor checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

```bash
source venv/bin/activate && python scripts/verify/verify_cursors.py
```

Expected: `ImportError: cannot import name 'gmail_state_key' from 'db'`.

- [ ] **Step 3: Replace the history-id wrappers in `db.py`**

Delete `get_last_history_id` and `set_last_history_id`, and add in their place:

```python
def gmail_state_key(account_id: str, suffix: str) -> str:
    """Namespace a Gmail poll cursor by account, e.g. 'gmail:a@x.com:history_id'."""
    return f"gmail:{account_id}:{suffix}"


def get_gmail_history_id(
    conn: sqlite3.Connection, user_id: str, account_id: str
) -> str | None:
    return get_user_state(conn, user_id, gmail_state_key(account_id, "history_id"))


def set_gmail_history_id(
    conn: sqlite3.Connection, user_id: str, account_id: str, history_id: str
) -> None:
    set_user_state(
        conn, user_id, gmail_state_key(account_id, "history_id"), history_id
    )
```

- [ ] **Step 4: Run the cursor verification script**

```bash
source venv/bin/activate && python scripts/verify/verify_cursors.py
```

Expected: every line `PASS`.

- [ ] **Step 5: Update the poller's constants and imports**

In `pollers/gmail/poller.py`, replace the two key constants (`poller.py:17-18`) with:

```python
BACKFILL_PENDING_SUFFIX = "backfill_pending"
BACKFILLED_SUFFIX = "backfilled"
```

and change the `db` import block to:

```python
from db import (
    gmail_state_key,
    get_gmail_history_id,
    set_gmail_history_id,
    save_event,
    get_user_state,
    set_user_state,
    clear_user_state,
)
```

- [ ] **Step 6: Make `poll` account-scoped**

Change the signature and the state reads/writes at the top of `poll`:

```python
def poll(
    service, conn: sqlite3.Connection, user_id: str, account_id: str
) -> list[GmailEvent]:
    pending_key = gmail_state_key(account_id, BACKFILL_PENDING_SUFFIX)
    if get_user_state(conn, user_id, pending_key):
        # Capture baseline before fetching so incremental polls pick up anything
        # that arrives during backfill (dedup handles overlap).
        current_id = get_current_history_id(service)
        print(f"[gmail] {account_id}: backfill (3d window)")
        events = _backfill_messages(service, conn, user_id, days=3)
        set_gmail_history_id(conn, user_id, account_id, current_id)
        set_user_state(
            conn, user_id, gmail_state_key(account_id, BACKFILLED_SUFFIX), "1"
        )
        clear_user_state(conn, user_id, pending_key)
        return events

    last_id = get_gmail_history_id(conn, user_id, account_id)

    if last_id is None:
        current_id = get_current_history_id(service)
        set_gmail_history_id(conn, user_id, account_id, current_id)
        return []
```

Then replace the final write at the end of `poll`:

```python
    if max_history_id != last_id:
        set_gmail_history_id(conn, user_id, account_id, max_history_id)

    return events
```

- [ ] **Step 7: Verify the module imports and has the right signature**

```bash
source venv/bin/activate && python -c "
from pollers.gmail.poller import poll, BACKFILL_PENDING_SUFFIX, BACKFILLED_SUFFIX
import inspect
print(inspect.signature(poll))
print(BACKFILL_PENDING_SUFFIX, BACKFILLED_SUFFIX)"
```

Expected:
```
(service, conn: sqlite3.Connection, user_id: str, account_id: str) -> list[pollers.gmail.events.GmailEvent]
backfill_pending backfilled
```

- [ ] **Step 8: Commit**

```bash
git add db.py pollers/gmail/poller.py scripts/verify/verify_cursors.py
git commit -m "feat: namespace Gmail poll cursors per account

History and backfill state move to gmail:<email>:* keys so each connected
mailbox advances independently. Replaces get/set_last_history_id, whose
single per-user key could only ever track one account.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Poll every connected account

**Files:**
- Modify: `main.py:62-118` (`_poll_gmail_for_user`), `main.py:26` (imports)

**Interfaces:**
- Consumes: `list_gmail_accounts` (Task 2), `get_gmail_service(..., account_id)` (Task 4), `poll(..., account_id)` (Task 5), `save_todo(..., account_id)` (Task 3).
- Produces: `_poll_gmail_for_user(conn, user)` now iterating accounts; no new public interface.

- [ ] **Step 1: Update the `db` import in `main.py`**

```python
from db import init_db, list_active_users, list_all_users, list_gmail_accounts, save_todo
```

- [ ] **Step 2: Split `_poll_gmail_for_user` into a per-account worker and a loop**

Replace the whole of `_poll_gmail_for_user` with:

```python
def _poll_gmail_account(conn: sqlite3.Connection, user_id: str, account_id: str) -> None:
    try:
        service = get_gmail_service(conn, user_id, account_id)
    except RuntimeError as exc:
        print(f"[gmail] {account_id}: {exc}")
        return

    events = poll(service, conn, user_id, account_id)

    inbound = [e for e in events if e.type == "messagesAdded"]
    if not inbound:
        print(f"[gmail] {account_id}: idle")
        return

    counts = {"todo": 0, "dup": 0, "skip": 0, "spam": 0}
    for e in inbound:
        from_email = e.actors.from_.email if e.actors.from_ else "unknown"
        prefix = f"[gmail] {account_id}   from={_truncate(from_email, 32):<32} | \"{_truncate(e.content.subject, 50)}\""

        if is_spam(e):
            counts["spam"] += 1
            print(f"{prefix} → spam")
            continue

        thread_msgs = fetch_thread_messages(service, e.content.thread_id)
        context = build_thread_context(thread_msgs, account_id)
        result = generate_todo(context, e)

        if not result["should_generate_todo"]:
            counts["skip"] += 1
            print(f"{prefix} → skip: {_truncate(result['reasoning'], 80)}")
            continue

        saved = save_todo(
            conn,
            e.event_id,
            e.content.message_id,
            e.content.thread_id,
            result,
            user_id,
            account_id,
        )
        todo = result["todo"]
        if saved:
            counts["todo"] += 1
            print(f"{prefix} → TODO[{todo.get('importance')}] {_truncate(todo['title'], 60)}")
        else:
            counts["dup"] += 1
            print(f"{prefix} → dup")

    parts = [f"{v} {k}" for k, v in counts.items() if v]
    summary = ", ".join(parts) if parts else "no actions"
    print(f"[gmail] {account_id}: {len(inbound)} fetched → {summary}")


def _poll_gmail_for_user(conn: sqlite3.Connection, user: dict) -> None:
    user_id = user["user_id"]
    accounts = list_gmail_accounts(conn, user_id)
    if not accounts:
        print(f"[gmail] {user_id[:8]}: no accounts connected")
        return
    # Each account is isolated so a revoked or rate-limited mailbox never stops
    # the others polling — the same pattern main() uses per source.
    for account in accounts:
        try:
            _poll_gmail_account(conn, user_id, account["account_id"])
        except Exception as exc:
            print(f"[gmail] {account['account_id']}: error: {exc}")
```

Note the `get_gmail_email(service)` call is gone — the address is now the account key we already hold, so the extra `getProfile` round trip per cycle disappears. Delete the now-unused `get_gmail_email` function from `main.py`.

- [ ] **Step 3: Verify the module imports**

```bash
source venv/bin/activate && python -c "import main; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Confirm no dead references remain**

```bash
grep -rn "get_gmail_email\|get_last_history_id\|set_last_history_id\|BACKFILLED_EMAIL_KEY\|BACKFILL_PENDING_KEY" --include="*.py" .
```

Expected: only hits in `app.py` (fixed in Task 7). If anything else appears, fix it before committing.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: poll every connected Gmail account per cycle

Splits the per-user Gmail poll into a per-account worker, each in its own
try/except so one failing mailbox never stops the others — matching the
per-source isolation already in main(). Drops the per-cycle getProfile
call, since the account address is now the connection key.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: OAuth flow — account chooser and required address

**Files:**
- Modify: `app.py:38` (import), `app.py:505-517` (`gmail_auth`), `app.py:519-567` (`gmail_callback`), `auth.py:146-167` (login-time save)

**Interfaces:**
- Consumes: `set_source_credentials(..., account_id)` (Task 2), `gmail_state_key` / `BACKFILL_PENDING_SUFFIX` / `BACKFILLED_SUFFIX` (Task 5).
- Produces: no new callable interface; establishes that every stored Gmail row has a real address as its `account_id`, which Tasks 8–10 rely on.

- [ ] **Step 1: Update `app.py` imports**

Replace the `from pollers.gmail.poller import BACKFILL_PENDING_KEY, BACKFILLED_EMAIL_KEY` line with:

```python
from pollers.gmail.poller import BACKFILL_PENDING_SUFFIX, BACKFILLED_SUFFIX
```

and add `gmail_state_key`, `list_gmail_accounts`, and `gmail_thread_url` to the existing `from db import (...)` block.

- [ ] **Step 2: Force the account chooser in `gmail_auth`**

In `gmail_auth`, change the `authorization_url` call to:

```python
    auth_url, state = flow.authorization_url(
        access_type="offline",
        # `select_account` is required for multi-account: with `consent` alone
        # Google silently reuses the already signed-in account, making it
        # impossible to add a second mailbox from the browser.
        prompt="select_account consent",
    )
```

- [ ] **Step 3: Rewrite the tail of `gmail_callback`**

Replace everything from `creds = flow.credentials` to the `return redirect(url_for("index"))` with:

```python
    creds = flow.credentials
    db = get_db()
    creds_dict = json.loads(creds.to_json())

    # The address is mandatory now: it is the connection's primary key, the
    # value the deep link needs, and the namespace for the poll cursor. A row
    # without one cannot be keyed, linked, or polled, so fail loudly rather
    # than storing an unusable connection.
    try:
        gmail_svc = google_build("gmail", "v1", credentials=creds)
        profile = gmail_svc.users().getProfile(userId="me").execute()
        connected_email = (profile.get("emailAddress") or "").strip().lower()
    except Exception:
        connected_email = ""
    if not connected_email:
        session.pop("gmail_oauth_state", None)
        session.pop("gmail_oauth_code_verifier", None)
        return (
            "Couldn't read the Gmail address for that account, so it wasn't "
            "connected. This is usually temporary — please try again.",
            502,
        )

    creds_dict["connected_email"] = connected_email
    user_id = current_user_id()
    assert user_id
    set_source_credentials(
        db, user_id, "gmail", "oauth2", creds_dict, account_id=connected_email
    )

    backfilled_key = gmail_state_key(connected_email, BACKFILLED_SUFFIX)
    if get_user_state(db, user_id, backfilled_key):
        print(f"[gmail] reconnect of {connected_email} — skipping backfill")
    else:
        print(f"[gmail] new account {connected_email} — scheduling backfill")
        set_user_state(
            db, user_id,
            gmail_state_key(connected_email, BACKFILL_PENDING_SUFFIX),
            connected_email,
        )
        clear_user_state(db, user_id, gmail_state_key(connected_email, "history_id"))

    session.pop("gmail_oauth_state", None)
    session.pop("gmail_oauth_code_verifier", None)
    return redirect(url_for("index"))
```

- [ ] **Step 4: Make the login-time save account-keyed in `auth.py`**

Replace the Gmail-scope block (`auth.py:146-167`) with:

```python
    # If the OAuth response included Gmail scopes and tokens, persist them
    # as a `source_connections` row for this user so the poller can run.
    creds = getattr(flow, "credentials", None)
    try:
        if creds and getattr(creds, "scopes", None):
            scopes = set(creds.scopes or [])
            if "https://www.googleapis.com/auth/gmail.readonly" in scopes:
                creds_dict = json.loads(creds.to_json())
                gmail_svc = google_build("gmail", "v1", credentials=creds)
                profile = gmail_svc.users().getProfile(userId="me").execute()
                connected_email = (profile.get("emailAddress") or "").strip().lower()
                # Skip rather than store an unkeyable row: account_id is the
                # primary key, and a blank one would collide across accounts.
                # The user can still connect Gmail from Settings.
                if connected_email:
                    creds_dict["connected_email"] = connected_email
                    set_source_credentials(
                        db, user_id, "gmail", "oauth2", creds_dict,
                        account_id=connected_email,
                    )
    except Exception:
        # Non-fatal: ensure login completes even if saving creds fails.
        pass
```

- [ ] **Step 5: Verify both modules import**

```bash
source venv/bin/activate && python -c "import app, auth; print('ok')"
```

Expected: `ok`

- [ ] **Step 6: Manually verify the account chooser appears**

```bash
source venv/bin/activate && flask --app app run --debug --port 5001
```

Sign in, open Settings, click **Connect with Google**. Expected: Google shows the **account chooser**, not a silent redirect back. Pick your primary account; you should land back on `/` connected.

Then confirm the row is keyed by address:

```bash
sqlite3 gmail_events.db "SELECT source, account_id FROM source_connections;"
```

Expected: `gmail|your.address@gmail.com`

- [ ] **Step 7: Commit**

```bash
git add app.py auth.py
git commit -m "feat: key Gmail OAuth connections by account address

Requests prompt=select_account so a second mailbox can actually be chosen,
and requires the getProfile address before storing credentials — a row
without one cannot be keyed, linked, or polled. Backfill bookkeeping moves
to per-account state keys.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Settings API and multi-account UI

**Files:**
- Modify: `app.py:480-503` (`get_settings`), `app.py:569-594` (`update_source_settings`), `templates/index.html:152-170` (Gmail card), `static/js/app.js:795-838` (settings wiring), `static/css/app.css` (append account-row styles)

**Interfaces:**
- Consumes: `list_gmail_accounts` (Task 2), `clear_source_connection(..., account_id)` (Task 2).
- Produces: `GET /settings` returns `sources.gmail = {"accounts": [{"email", "connected_at"}], "auth_url": str}`. `POST /settings/sources/gmail` accepts `{"disconnect": true, "account_id": "<email>"}`. Task 9 relies on the `accounts` list to decide banner visibility.

- [ ] **Step 1: Return the account list from `get_settings`**

Replace the Gmail portion of `get_settings` with:

```python
    accounts = list_gmail_accounts(db, user_id)
    return jsonify({
        "sources": {
            "fathom": {
                "connected": bool(fathom),
                "api_key_preview": f"...{fathom_key[-6:]}" if fathom_key else None,
            },
            "gmail": {
                "accounts": [
                    {"email": a["account_id"], "connected_at": a["connected_at"]}
                    for a in accounts
                ],
                "auth_url": url_for("gmail_auth"),
            },
        }
    })
```

Delete the now-unused `gmail = get_source_connection(db, user_id, "gmail")` and `gmail_email = ...` lines above it.

- [ ] **Step 2: Accept per-account disconnect**

Replace the `if source == "gmail":` branch of `update_source_settings` with:

```python
    if source == "gmail":
        if data.get("disconnect"):
            # An explicit account_id disconnects one mailbox; omitting it
            # disconnects every Gmail account for this user.
            account_id = (data.get("account_id") or "").strip().lower() or None
            clear_source_connection(db, user_id, "gmail", account_id)
            return jsonify({
                "ok": True,
                "accounts": [
                    {"email": a["account_id"], "connected_at": a["connected_at"]}
                    for a in list_gmail_accounts(db, user_id)
                ],
            })
        return jsonify({"error": "use /settings/sources/gmail/auth to connect"}), 400
```

- [ ] **Step 3: Replace the Gmail card markup**

In `templates/index.html`, replace the whole `<div class="source-card" id="gmail-card">…</div>` block with:

```html
      <div class="source-card" id="gmail-card">
        <div class="source-card-header">
          <span class="source-card-name">Gmail</span>
          <span class="source-connected-badge off" id="gmail-status">Not connected</span>
        </div>
        <div class="source-card-body">
          <div id="gmail-accounts"></div>
          <button class="btn-connect" id="gmail-connect-btn">Connect another account</button>
        </div>
      </div>
```

- [ ] **Step 4: Rewrite the settings JS**

In `static/js/app.js`, replace the `openSettingsModal` function and the two Gmail button handlers with:

```javascript
function renderGmailAccounts(accounts) {
  const container = document.getElementById('gmail-accounts');
  const status = document.getElementById('gmail-status');
  const connectBtn = document.getElementById('gmail-connect-btn');
  const count = accounts.length;

  status.textContent = count
    ? `${count} account${count === 1 ? '' : 's'}`
    : 'Not connected';
  status.className = `source-connected-badge ${count ? 'on' : 'off'}`;
  connectBtn.textContent = count ? 'Connect another account' : 'Connect with Google';

  container.innerHTML = accounts.map(a => `
    <div class="gmail-account-row" data-email="${escapeHtml(a.email)}">
      <span class="gmail-account-email">${escapeHtml(a.email)}</span>
      <button class="btn-disconnect gmail-account-disconnect">Disconnect</button>
    </div>`).join('');

  container.querySelectorAll('.gmail-account-disconnect').forEach(btn => {
    btn.addEventListener('click', () => {
      const email = btn.closest('.gmail-account-row').dataset.email;
      if (!confirm(`Disconnect ${email}? Existing todos from this account are kept.`)) return;
      btn.disabled = true;
      fetch('/settings/sources/gmail', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disconnect: true, account_id: email }),
      })
        .then(r => r.json())
        .then(data => { if (data.ok) renderGmailAccounts(data.accounts); })
        .finally(() => { btn.disabled = false; });
    });
  });
}

function openSettingsModal() {
  settingsModal.classList.add('open');
  fetch('/settings').then(r => r.json()).then(data => {
    const { fathom, gmail } = data.sources;
    setSourceConnected('fathom', fathom.connected, fathom.api_key_preview);
    renderGmailAccounts(gmail.accounts || []);
  });
}
document.getElementById('openSettingsBtn').addEventListener('click', openSettingsModal);
document.getElementById('settings-modal-close').addEventListener('click', () => settingsModal.classList.remove('open'));
settingsModal.addEventListener('click', e => { if (e.target === settingsModal) settingsModal.classList.remove('open'); });

document.getElementById('gmail-connect-btn').addEventListener('click', () => {
  window.location.href = '/settings/sources/gmail/auth';
});
```

Leave the two Fathom handlers exactly as they are — they still use `setSourceConnected`.

- [ ] **Step 5: Add the account-row styles**

Append to `static/css/app.css`:

```css
    .gmail-account-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid var(--rule);
    }
    .gmail-account-row:last-of-type { border-bottom: none; }
    .gmail-account-email {
      font-size: 13px;
      color: var(--ink-2);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    #gmail-accounts:empty + .btn-connect { margin-top: 0; }
    #gmail-accounts { margin-bottom: 8px; }
```

If `--rule`, `--ink-2`, or `--ink-3` are not defined in this stylesheet, use the nearest existing border and secondary-text variables instead — check the `:root` block at the top of the file first.

- [ ] **Step 6: Manually verify the settings UI**

```bash
source venv/bin/activate && flask --app app run --debug --port 5001
```

Open Settings. Expected: your connected account listed with its address and a Disconnect button; the badge reads `1 account`; the button reads **Connect another account**.

Click it and connect a **second** Gmail account. Expected: both addresses listed, badge reads `2 accounts`. Confirm in SQL:

```bash
sqlite3 gmail_events.db "SELECT account_id FROM source_connections WHERE source='gmail';"
```

Expected: two distinct addresses — the first was **not** overwritten. This is the core bug being fixed; if only one row appears, stop and debug before continuing.

- [ ] **Step 7: Commit**

```bash
git add app.py templates/index.html static/js/app.js static/css/app.css
git commit -m "feat: list and manage multiple Gmail accounts in settings

/settings now returns a list of connected accounts, and the Gmail card
renders one row per account with its own disconnect, plus a persistent
'Connect another account' button.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Account badge and account-aware thread pane

**Files:**
- Modify: `app.py:202-243` (`index`), `app.py:351-395` (`todo_context`), `templates/index.html:24-28` (banner), `templates/index.html:96-118` (row markup), `static/css/app.css` (append badge style)

**Interfaces:**
- Consumes: `gmail_thread_url` (Task 3), `get_gmail_service(..., account_id)` (Task 4), `list_gmail_accounts` (Task 2).
- Produces: `todos` rows rendered by `index` now include `account_id`; `/todos/<id>/context` returns `"account": <email or null>` alongside `thread_url`.

- [ ] **Step 1: Select `account_id` and count accounts in `index`**

Add `account_id` to the `SELECT` list in `index` (immediately after `source`), then replace the `gmail_connected` line with:

```python
    gmail_accounts = list_gmail_accounts(db, user_id)
    gmail_connected = bool(gmail_accounts)
```

- [ ] **Step 2: Render the account badge**

In `templates/index.html`, inside `<div class="row-meta">`, add an account badge immediately after the existing source badge:

```html
              <span class="source-badge {{ t.source or 'gmail' }}">{{ src_label }}</span>
              {% if t.source == 'gmail' and t.account_id %}
              <span class="account-badge" title="{{ t.account_id }}">{{ t.account_id.split('@')[0] }}</span>
              {% endif %}
```

The badge shows the local part only — full addresses are too long for the row — with the full address in the tooltip. Todos with no `account_id` show no badge, per the spec.

- [ ] **Step 3: Add the badge style**

Append to `static/css/app.css`:

```css
    .account-badge {
      font-size: 11px;
      padding: 1px 6px;
      border-radius: 999px;
      background: var(--pill-stone-bg);
      color: var(--pill-stone-fg);
      max-width: 120px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
```

- [ ] **Step 4: Make `todo_context` account-aware**

In `todo_context`, add `account_id` to the `SELECT`, then replace the Gmail branch's service construction and `thread_url` with:

```python
    if source == "gmail":
        thread_id = meta.get("thread_id")
        if not thread_id:
            return jsonify({"source": source, "error": "No thread linked to this todo."})
        account_id = row["account_id"]
        try:
            # account_id is None for todos created before per-account provenance;
            # get_gmail_service falls back to the user's first connected account.
            service = get_gmail_service(db, user_id, account_id)
            user_email = service.users().getProfile(userId="me").execute().get("emailAddress", "")
            messages = fetch_thread_messages(service, thread_id)
            formatted = [
                {
                    "from_name": m["from_name"],
                    "from_email": m["from_email"],
                    "received_at": m["received_at"],
                    "body_text": m["body_text"],
                    "is_user": bool(user_email) and user_email.lower() in (m["from_email"] or "").lower(),
                }
                for m in messages
            ]
            return jsonify({
                "source": "gmail",
                "thread": formatted,
                "account": account_id,
                "thread_url": row["relevant_link"] or gmail_thread_url(thread_id, account_id),
            })
        except Exception as exc:
            return jsonify({"source": "gmail", "error": f"Couldn't load thread: {exc}"})
```

- [ ] **Step 5: Verify the app imports and renders**

```bash
source venv/bin/activate && python -c "import app; print('ok')"
```

Expected: `ok`

- [ ] **Step 6: Manually verify badges and links**

Start the web UI, sign in with both accounts connected, and wait for the poller to produce a todo from each (or run `python main.py` in another terminal until one appears from each mailbox).

```bash
source venv/bin/activate && flask --app app run --debug --port 5001
```

Expected, in a browser signed into **both** Google accounts:
- Each Gmail todo shows an account badge with the mailbox's local part.
- Clicking a todo from account B opens the thread **in account B**, not account A.
- The pre-existing todo with `NULL` `account_id` shows **no** badge, still opens, and still loads its thread pane.

Confirm provenance is being recorded:

```bash
sqlite3 gmail_events.db "SELECT account_id, COUNT(*) FROM todos WHERE source='gmail' GROUP BY account_id;"
```

Expected: one group per connected account, plus a `NULL` group for the pre-existing todo.

- [ ] **Step 7: Commit**

```bash
git add app.py templates/index.html static/css/app.css
git commit -m "feat: show the source account on Gmail todos and route the thread pane

Each Gmail todo displays the mailbox it came from, and the detail pane
builds its Gmail client from that account. Todos predating per-account
provenance show no badge and fall back to the first connected account.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Thread the account through the AI agent

**Files:**
- Modify: `agent/tools/email.py:18-22` (`_gmail_service`), `agent/tools/email.py:43-57` (`fetch_gmail_thread_context`), `agent/tools/email.py:59-113` (`gmail_tools`), `agent/input_builder.py:21-42` (`build_initial_inputs`), `agent/resolver.py:11-38`, `app.py:311-349` (`ask_ai`)

**Interfaces:**
- Consumes: `get_gmail_service(conn, user_id, account_id)` (Task 4).
- Produces:
  - `gmail_tools(user_id: str, account_id: str | None = None) -> list[Any]`
  - `fetch_gmail_thread_context(source_meta_json, user_id: str, account_id: str | None = None) -> str | None`
  - `build_initial_inputs(todo: dict, user_message: str, user_id: str) -> list[dict]` — unchanged signature; reads `todo["account_id"]` internally.
  - `resolve_todo(todo: dict, thread: list, user_message: str, user_id: str) -> list` — unchanged signature; reads `todo["account_id"]` internally.

Keeping the two outer signatures stable means `app.py`'s only change is selecting one more column.

- [ ] **Step 1: Make the service helper account-aware**

In `agent/tools/email.py`:

```python
def _gmail_service(user_id: str, account_id: str | None = None):
    conn = open_db()
    try:
        return get_gmail_service(conn, user_id, account_id)
    finally:
        conn.close()
```

- [ ] **Step 2: Thread the account through `fetch_gmail_thread_context`**

```python
def fetch_gmail_thread_context(
    source_meta_json, user_id: str, account_id: str | None = None
) -> str | None:
    """Used by the input builder to inject the linked thread on turn 1."""
    try:
        meta = json.loads(source_meta_json) if isinstance(source_meta_json, str) else source_meta_json
        thread_id = (meta or {}).get("thread_id")
        if not thread_id:
            return None
        service = _gmail_service(user_id, account_id)
        user_email = service.users().getProfile(userId="me").execute()["emailAddress"]
        messages = fetch_thread_messages(service, thread_id)
        return _build_full_thread_context(messages, user_email)
    except Exception:
        logger.exception("Failed to fetch Gmail thread context")
        return None
```

- [ ] **Step 3: Thread the account through the tools**

Change the factory signature and both `_gmail_service` calls inside it:

```python
def gmail_tools(user_id: str, account_id: str | None = None) -> list[Any]:
```

Inside `search_email_threads`, change `service = _gmail_service(user_id)` to `service = _gmail_service(user_id, account_id)`. Make the identical change inside `fetch_email_thread`. Leave both docstrings unchanged — they describe behaviour the model sees, and the mailbox selection is not the model's concern.

- [ ] **Step 4: Read the account in the input builder**

In `agent/input_builder.py`, change the Gmail context call:

```python
    if todo.get("source") == "gmail" and todo.get("source_meta"):
        email_context = fetch_gmail_thread_context(
            todo["source_meta"], user_id, todo.get("account_id")
        )
```

- [ ] **Step 5: Read the account in the resolver**

In `agent/resolver.py`:

```python
def _build_agent(user_id: str, account_id: str | None = None) -> Agent:
    tools: list[Any] = [WebSearchTool()]
    tools.extend(gmail_tools(user_id, account_id))
    # tools.extend(local_file_tools())
    return Agent(
        name="Resolver",
        model="gpt-5.4-mini",
        instructions=INSTRUCTIONS,
        tools=tools,
    )


def resolve_todo(
    todo: dict, thread: list[Any], user_message: str, user_id: str
) -> list[Any]:
    """Run one turn of the agent. Returns the updated thread (SDK input-list shape)."""
    # The agent searches the mailbox the todo came from. None (legacy todos)
    # falls back to the user's first connected account.
    agent = _build_agent(user_id, todo.get("account_id"))
```

Leave the rest of `resolve_todo` unchanged.

- [ ] **Step 6: Select `account_id` in `ask_ai`**

In `app.py`, add `account_id` to the `SELECT` list in `ask_ai` so `dict(row)` carries it into `resolve_todo`:

```python
    row = db.execute(
        "SELECT title, suggested_action, reasoning, importance, due_date, source, "
        "account_id, ai_thread, source_meta "
        "FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
```

- [ ] **Step 7: Verify imports and signatures**

```bash
source venv/bin/activate && python -c "
import inspect, app
from agent.tools.email import gmail_tools, fetch_gmail_thread_context
from agent.resolver import resolve_todo
print(inspect.signature(gmail_tools))
print(inspect.signature(fetch_gmail_thread_context))
print(inspect.signature(resolve_todo))"
```

Expected:
```
(user_id: str, account_id: str | None = None) -> list[typing.Any]
(source_meta_json, user_id: str, account_id: str | None = None) -> str | None
(todo: dict, thread: list[typing.Any], user_message: str, user_id: str) -> list[typing.Any]
```

- [ ] **Step 8: Manually verify the agent uses the right mailbox**

With both accounts connected and the web UI running, open a todo from account B and click **Ask AI** with a message like `search my email for the most recent thread with this sender and tell me who it is from`.

Expected: the agent's answer reflects account B's mailbox. Cross-check by opening a todo from account A and asking the same thing — the results should differ if the mailboxes differ.

- [ ] **Step 9: Commit**

```bash
git add agent/tools/email.py agent/input_builder.py agent/resolver.py app.py
git commit -m "feat: point the per-todo agent at the todo's own mailbox

gmail_tools and fetch_gmail_thread_context take an account_id, so
search_email_threads and fetch_email_thread query the account the todo
came from rather than an arbitrary one. Legacy todos with no account fall
back to the first connected account.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: End-to-end verification and documentation

**Files:**
- Modify: `CLAUDE.md` (Auth section, Gmail polling section, Data model section)

**Interfaces:**
- Consumes: everything above.
- Produces: no code interface; updated project documentation.

- [ ] **Step 1: Re-run every verification script**

```bash
source venv/bin/activate && for s in migration connections links cursors; do
  echo "--- $s ---"; python scripts/verify/verify_$s.py || break
done
```

Expected: all four scripts end in their `All … checks passed.` line.

- [ ] **Step 2: Run the full end-to-end checklist**

Start both processes in separate terminals:

```bash
source venv/bin/activate && python main.py
```

```bash
source venv/bin/activate && flask --app app run --debug --port 5001
```

Work through each check from the spec's Testing section and confirm:

1. Both accounts appear in the poller log with **separate** history cursors:
   ```bash
   sqlite3 gmail_events.db "SELECT key, value FROM user_state WHERE key LIKE 'gmail:%';"
   ```
   Expected: a `gmail:<email>:history_id` row per connected account.
2. Only the newly connected account ran a backfill (`backfill (3d window)` appears once per new account, not for the pre-existing one).
3. A todo from account B opens the thread in account B.
4. Revoke account B's access at <https://myaccount.google.com/permissions>, then watch the poller: B should report revoked access and flip to not-connected in Settings, while **A keeps polling**.
5. Reconnect B, then disconnect it from Settings: A still polls and B's todos remain in the list.
6. The pre-existing `NULL`-account todo still opens and still loads its thread pane.

- [ ] **Step 3: Update `CLAUDE.md`**

In the **Auth and per-user credentials** section, replace the sentence beginning "There is no `credentials.json`…" with:

```markdown
There is no `credentials.json` or `token.json` — every credential is per user
*and per account*, stored as JSON in the `source_connections` table, keyed by
`(user_id, source, account_id)` (`auth_type` is `oauth2` or `api_key`).
`account_id` is the lowercased Gmail address for Gmail and `''` for
single-connection sources. A user can connect several Gmail accounts; each has
its own credentials, its own poll cursor, and its own place in the poll loop,
so one revoked token never disturbs the others. Fathom is an API key the user
pastes into Settings; there is no global fallback key.
```

In the same section, update the `RefreshError` paragraph so it reads "clears the stored connection **for that account**".

In **Data model**, update the `user_state` bullet to name the per-account keys:

```markdown
- `user_state` — per-user key/value: Gmail cursors (`gmail:<email>:history_id`,
  `gmail:<email>:backfill_pending`, `gmail:<email>:backfilled` — one set per
  connected account), `fathom_last_polled_at`, digest bookkeeping.
  (`state` is the legacy single-user table, migrated away from.)
```

and add to the `todos` bullet: "`account_id` records which Gmail account a todo came from; `NULL` means unknown (todos predating multi-account support)."

In **Gmail polling specifics**, replace the first sentence with:

```markdown
The poller iterates every connected Gmail account for a user, each in its own
`try/except`. On first connect of an account the poller does a **3-day
backfill** (`gmail:<email>:backfill_pending` user-state key), capturing that
account's current `historyId` *before* fetching so anything arriving
mid-backfill is still picked up on the next cycle — dedup absorbs the overlap.
```

- [ ] **Step 4: Note the verification scripts in `CLAUDE.md`**

Replace the "There are no tests, no linter config, and no CI in this repo" paragraph with:

```markdown
There is no test suite, linter config, or CI in this repo. Verify changes by
running the two processes and exercising the UI. The exception is
`scripts/verify/`, a handful of standalone assertion scripts for the
`db.py` schema and helpers — run them with plain `python` (no pytest):

```bash
python scripts/verify/verify_migration.py    # optionally: <path-to-db-copy>
python scripts/verify/verify_connections.py
python scripts/verify/verify_links.py
python scripts/verify/verify_cursors.py
```
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: describe multi-account Gmail in CLAUDE.md

Records the (user_id, source, account_id) key, the per-account cursor
keys, todos.account_id provenance, the per-account poll loop, and the
scripts/verify/ assertion scripts.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Open the pull request**

```bash
git push -u origin ritwik/feat/multi-gmail-accounts
gh pr create --title "Support multiple Gmail accounts per user" --body "$(cat <<'EOF'
## Summary

Lets one signed-in user connect several Gmail accounts, poll them all
independently, and route each todo back to the account it came from.

- `source_connections` is keyed by `(user_id, source, account_id)`
- Poll cursors are namespaced per account (`gmail:<email>:history_id`)
- `todos.account_id` records provenance, driving the deep link and a badge
- One revoked token disconnects only that account; the rest keep polling

## Bug fixed along the way

Gmail deep links were built as `/u/0/?authuser=<email>`, where `/u/0/` pins
profile index 0 and overrides the `authuser` hint — so links already opened
the wrong account before this change. Now `/u/<email>/`.

## Breaking change

The migration **drops existing Gmail connections** and their legacy cursor
keys, because the only pre-existing row has no recorded address and its
account-namespaced cursor key cannot be derived. Users reconnect once from
Settings. This is safe today because there are no users yet; it must be
revisited if that changes before merge.

## Verification

`scripts/verify/*.py` cover the migration and `db.py` helpers. OAuth, poller,
and UI paths were verified manually per CLAUDE.md — see the plan's Task 11
checklist.

Spec: `docs/superpowers/specs/2026-09-06-multi-gmail-accounts-design.md`
Plan: `docs/superpowers/plans/2026-09-06-multi-gmail-accounts.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
