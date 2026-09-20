# Push Notifications With Suggested Actions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every new todo pushes a Web Push notification to the user's enrolled browsers, showing the todo's suggested actions as buttons; a button click starts the resolution run through `/ask-ai` exactly like clicking the chip in the UI.

**Architecture:** The poller (`main.py`) calls `push_notify.notify_new_todo` right after a successful save; it returns before any LLM call if the user has no subscription, otherwise ensures the three action options are cached and sends a small payload (labels, never instructions) via `pywebpush`. The service worker renders it and, on a button click, POSTs `{action_index}` to `/ask-ai` with the session cookie; the server resolves the cached instruction and starts the run with `from_suggestion=True`.

**Tech Stack:** Python 3.14 / Flask, SQLite (`db.py`), `pywebpush` 2.x (+ `py-vapid`, `cryptography`), Service Worker Push API, plain-`python` assertion scripts under `scripts/verify/` (no pytest in this repo).

**Spec:** `docs/superpowers/specs/2026-09-19-push-notifications-with-actions-design.md`

## Global Constraints

- No test framework: verification scripts are plain `python scripts/verify/<name>.py` with a `check(label, cond)` helper that prints `PASS`/`FAIL` and exits 1 on failure. Follow `scripts/verify/verify_web.py` for the Flask-client pattern.
- Any change to `static/js/app.js` or `static/css/app.css` requires bumping `VERSION` in `static/js/sw.js` (cache-first service worker). This plan bumps it to `v8` once, in Task 8.
- Every `save_*_todo` helper returns `str | None` (the `todo_id` on insert, `None` on dedup/reject). Callers test truthiness.
- The poller must never fail a poll cycle because of a notification: `notify_new_todo` never raises.
- No LLM spend for action inference on behalf of a user with zero push subscriptions.
- Push payload carries action `index` + `label` only — never `instruction`.
- Env vars: `VAPID_PRIVATE_KEY`, `VAPID_PUBLIC_KEY`, `VAPID_SUBJECT`. All three set ⇒ configured.
- Run every verify script with the venv active: `source venv/bin/activate`.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Do not edit a `.py` file while a Hermes turn is live under `flask --debug` (see CLAUDE.md).

---

## File map

| File | Responsibility |
|---|---|
| `db.py` | `push_subscriptions` table + 4 helpers; `save_*_todo` → `str \| None` |
| `push_notify.py` (new) | `configured()`, `build_payload()`, `send_to_user()`, `notify_new_todo()` |
| `agent/action_options.py` | `ensure_action_options()` — cache-then-generate, shared by route and poller |
| `app.py` | `/push/*` routes, `notifications` in `/settings`, `action_index` in `/ask-ai`, route uses `ensure_action_options` |
| `auth.py` | `/push/vapid-public-key` in the JSON GET allowlist |
| `main.py`, `pollers/{fathom,browser,system}/poller.py` | call `notify_new_todo(conn, user_id, todo_id)` on insert |
| `static/js/sw.js` | `push` + `notificationclick` handlers; `VERSION = 'v8'` |
| `static/js/app.js`, `templates/index.html` | Settings "Browser notifications" card |
| `scripts/gen_vapid_keys.py` (new) | prints a VAPID pair in `.env` form |
| `scripts/verify/verify_push.py` (new) | assertion script for the whole feature |
| `requirements.txt`, `.env.example`, `CLAUDE.md` | dependency, config, docs |
| removed: `notify.py`, `scripts/verify/verify_notify.py` | superseded local banner |

---

### Task 1: Remove the local banner; `save_*_todo` return the todo id

**Files:**
- Delete: `notify.py`, `scripts/verify/verify_notify.py`
- Modify: `db.py:624-670` (`_save_todo`), `db.py:677-711` (`save_browser_history_todo`), `db.py:713-741` (`save_fathom_todo`), `db.py:787-830` (`save_todo`), `db.py:933-960` (`save_system_todo`)
- Modify: `main.py:26,104`, `pollers/fathom/poller.py:7,43-44`, `pollers/browser/poller.py:12,246`, `pollers/system/poller.py:8,51`
- Modify: `.env.example` (remove `NOTIFY_NEW_TODOS` block), `CLAUDE.md` (remove `verify_notify.py` line and the "Desktop notifications" paragraph)
- Test: `scripts/verify/verify_push.py` (created here, grows in later tasks)

**Interfaces:**
- Produces: `_save_todo(...) -> str | None`; `save_todo(...) -> str | None`; `save_fathom_todo(...) -> str | None`; `save_browser_history_todo(...) -> str | None`; `save_system_todo(...) -> str | None`. Each returns the `todo_id` it inserted, else `None`.

- [ ] **Step 1: Write the failing test**

Create `scripts/verify/verify_push.py`:

```python
"""Verifies push notifications with suggested actions: subscription storage,
the poller-side send (stubbing pywebpush and the OpenAI call), the web routes,
and action_index resolution in /ask-ai. No network, no spend.

Usage: python scripts/verify/verify_push.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "push.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only")

from db import (  # noqa: E402
    init_db, upsert_user, save_todo, save_fathom_todo,
    save_browser_history_todo, save_system_todo,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def _gmail_result(title: str) -> dict:
    return {"should_generate_todo": True, "reasoning": "r",
            "todo": {"title": title, "importance": "high"}}


def test_save_helpers_return_ids() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")

    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com")
    check("save_todo returns the todo id", isinstance(tid, str) and tid.startswith("todo_"))
    check("save_todo returns None on dup",
          save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com") is None)

    meeting = {"recording_id": "rec1", "title": "Standup", "url": "https://x"}
    item = {"description": "Send the deck"}
    fid = save_fathom_todo(conn, uid, meeting, 0, item)
    check("save_fathom_todo returns the todo id", isinstance(fid, str) and fid.startswith("todo_fathom_"))
    check("save_fathom_todo returns None on dup", save_fathom_todo(conn, uid, meeting, 0, item) is None)

    bh = {"title": "Finish the Dynatrace signup", "relevant_link": "https://www.dynatrace.com/signup",
          "importance": "medium"}
    bid = save_browser_history_todo(conn, uid, bh)
    check("save_browser_history_todo returns the todo id",
          isinstance(bid, str) and bid.startswith("todo_browser_history_"))
    check("save_browser_history_todo returns None on dup", save_browser_history_todo(conn, uid, bh) is None)

    st = {"title": "File the tax PDF in Downloads", "importance": "low"}
    sid = save_system_todo(conn, uid, st)
    check("save_system_todo returns the todo id", isinstance(sid, str) and sid.startswith("todo_system_"))
    check("save_system_todo returns None on dup", save_system_todo(conn, uid, st) is None)


def main() -> None:
    test_save_helpers_return_ids()
    print("All checks passed.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `FAIL  save_todo returns the todo id` (it currently returns `True`).

- [ ] **Step 3: Make `_save_todo` and the four helpers return the id**

In `db.py`, `_save_todo` — change the signature's return annotation and the last line:

```python
) -> str | None:
    ...
    conn.commit()
    return todo_id if conn.total_changes > before else None
```

`save_todo` (gmail): annotation `-> str | None`; the two early `return False` become `return None`. The final `return _save_todo(...)` is unchanged.

`save_browser_history_todo`: annotation `-> str | None`; the three early `return False` become `return None`.

`save_fathom_todo`: annotation `-> str | None` (it already `return _save_todo(...)`).

`save_system_todo`: annotation `-> str | None`; every early `return False` becomes `return None`. Open the function and check — it starts at `db.py:933`.

- [ ] **Step 4: Delete the local banner and its call sites**

```bash
git rm -q notify.py scripts/verify/verify_notify.py
```

Remove `from notify import notify_new_todo` from `main.py`, `pollers/fathom/poller.py`, `pollers/browser/poller.py`, `pollers/system/poller.py`.

Remove these lines:
- `main.py`: `            notify_new_todo(todo["title"], "gmail", todo.get("importance"))`
- `pollers/browser/poller.py`: `            notify_new_todo(title, "browser_history", todo.get("importance"))`
- `pollers/system/poller.py`: `            notify_new_todo(todo.get("title", ""), "system", todo.get("importance"))`

`pollers/fathom/poller.py` — restore the plain loop for now (Task 7 re-wires it):

```python
        for idx, item in enumerate(action_items):
            save_fathom_todo(conn, user_id, meeting, idx, item)
            saved += 1
```

`.env.example` — delete the block:

```
# macOS banner (via osascript) each time a poller saves a new todo. On by
# default; a no-op anywhere but macOS. Set to 0 to silence.
NOTIFY_NEW_TODOS=

```

`CLAUDE.md` — delete the line `python scripts/verify/verify_notify.py      # stubs osascript; no banner` and the whole `**Desktop notifications** (`notify.py`): …` paragraph (5 lines, ends with "must never cost a poll cycle.").

- [ ] **Step 5: Run the tests**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/verify/verify_links.py && python scripts/verify/verify_web.py && python -c "import main, pollers.fathom.poller, pollers.browser.poller, pollers.system.poller; print('imports ok')"`
Expected: all `PASS`, `All checks passed.` ×3, `imports ok`. (`verify_links` checks `saved` for truthiness, which a string satisfies.)

- [ ] **Step 6: Commit**

```bash
git add -A db.py main.py pollers .env.example CLAUDE.md notify.py scripts/verify
git commit -m "refactor: drop the osascript banner; save_*_todo return the todo id

The push notification that replaces it needs the id, not a bool. Every
caller tests truthiness, so nothing else changes.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `push_subscriptions` table and helpers

**Files:**
- Modify: `db.py` — `init_db` (add table after `page_views`, around line 308), new helpers after `list_all_users` (~line 437)
- Test: `scripts/verify/verify_push.py`

**Interfaces:**
- Produces:
  - `save_push_subscription(conn, user_id: str, subscription: dict, user_agent: str | None = None) -> None` — `subscription` is `{"endpoint": str, "keys": {"p256dh": str, "auth": str}}`
  - `list_push_subscriptions(conn, user_id: str) -> list[dict]` — dicts with keys `endpoint, user_id, p256dh, auth, user_agent, created_at`
  - `delete_push_subscription(conn, endpoint: str, user_id: str | None = None) -> None` — when `user_id` given, deletes only that user's row
  - `count_push_subscriptions(conn, user_id: str) -> int`

- [ ] **Step 1: Write the failing test**

Add to `scripts/verify/verify_push.py`, importing the four helpers from `db`:

```python
from db import (  # noqa: E402
    save_push_subscription, list_push_subscriptions,
    delete_push_subscription, count_push_subscriptions,
)

SUB_A = {"endpoint": "https://push.example/a", "keys": {"p256dh": "PA", "auth": "AA"}}
SUB_B = {"endpoint": "https://push.example/b", "keys": {"p256dh": "PB", "auth": "AB"}}


def test_subscription_helpers() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    other, _ = upsert_user(conn, "other@example.com")

    check("no subscriptions initially", count_push_subscriptions(conn, uid) == 0)
    save_push_subscription(conn, uid, SUB_A, "Chrome/1")
    save_push_subscription(conn, uid, SUB_A, "Chrome/2")  # same endpoint → upsert
    check("same endpoint twice is one row", count_push_subscriptions(conn, uid) == 1)
    rows = list_push_subscriptions(conn, uid)
    check("row carries keys", rows[0]["p256dh"] == "PA" and rows[0]["auth"] == "AA")
    check("upsert keeps the latest user agent", rows[0]["user_agent"] == "Chrome/2")

    save_push_subscription(conn, other, SUB_B)
    check("list is per user", [r["endpoint"] for r in list_push_subscriptions(conn, uid)] == [SUB_A["endpoint"]])

    delete_push_subscription(conn, SUB_A["endpoint"], user_id=other)
    check("delete scoped to another user is a no-op", count_push_subscriptions(conn, uid) == 1)
    delete_push_subscription(conn, SUB_A["endpoint"], user_id=uid)
    check("delete removes the owner's row", count_push_subscriptions(conn, uid) == 0)
    delete_push_subscription(conn, SUB_B["endpoint"])
    check("unscoped delete removes by endpoint", count_push_subscriptions(conn, other) == 0)
```

And call `test_subscription_helpers()` from `main()` after `test_save_helpers_return_ids()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `ImportError: cannot import name 'save_push_subscription'`.

- [ ] **Step 3: Add the table and helpers**

In `init_db`, after the `page_views` `CREATE TABLE` (and its index) add:

```python
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            endpoint    TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            p256dh      TEXT NOT NULL,
            auth        TEXT NOT NULL,
            user_agent  TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user "
        "ON push_subscriptions(user_id)"
    )
```

New section in `db.py` after `list_all_users`:

```python
# ---------------------------------------------------------------------------
# Push subscriptions — one row per enrolled browser, keyed on the push
# service's endpoint URL so re-subscribing from the same browser is an upsert.
# ---------------------------------------------------------------------------


def save_push_subscription(
    conn: sqlite3.Connection,
    user_id: str,
    subscription: dict,
    user_agent: str | None = None,
) -> None:
    keys = subscription.get("keys") or {}
    conn.execute(
        "INSERT OR REPLACE INTO push_subscriptions "
        "(endpoint, user_id, p256dh, auth, user_agent, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (subscription["endpoint"], user_id, keys["p256dh"], keys["auth"],
         user_agent, _now()),
    )
    conn.commit()


def list_push_subscriptions(conn: sqlite3.Connection, user_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT endpoint, user_id, p256dh, auth, user_agent, created_at "
        "FROM push_subscriptions WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    ).fetchall()
    cols = ["endpoint", "user_id", "p256dh", "auth", "user_agent", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def delete_push_subscription(
    conn: sqlite3.Connection, endpoint: str, user_id: str | None = None
) -> None:
    if user_id is None:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    else:
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
            (endpoint, user_id),
        )
    conn.commit()


def count_push_subscriptions(conn: sqlite3.Connection, user_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
```

`_now()` already exists in `db.py` and returns an ISO-8601 UTC string.

- [ ] **Step 4: Run the tests**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/verify/verify_migration.py`
Expected: all `PASS`; `verify_migration` still passes (new table is additive).

- [ ] **Step 5: Commit**

```bash
git add db.py scripts/verify/verify_push.py
git commit -m "feat(db): push_subscriptions table and helpers

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `ensure_action_options` — shared cache-then-generate

**Files:**
- Modify: `agent/action_options.py` (append after `generate_action_options`, ~line 125)
- Modify: `app.py:472-513` (`todo_action_options` becomes a thin wrapper)
- Test: `scripts/verify/verify_push.py`

**Interfaces:**
- Consumes: `generate_action_options(todo: dict, user_id: str) -> list[dict]` (existing; raises on failure)
- Produces: `ensure_action_options(conn, todo: dict, user_id: str, refresh: bool = False) -> list[dict]` — `todo` must include `todo_id` and `action_options` (the raw JSON string or `None`). Returns the cached list when present, valid, and not `refresh`; otherwise generates, writes `todos.action_options` + `updated_at`, and returns the new list. Raises whatever `generate_action_options` raises.

- [ ] **Step 1: Write the failing test**

Add to `scripts/verify/verify_push.py`:

```python
ACTIONS = [
    {"label": "Reply with dates", "detail": "Offer two slots", "instruction": "Reply proposing Tue or Thu"},
    {"label": "Decline politely", "detail": "", "instruction": "Reply declining"},
    {"label": "Forward to Sam", "detail": "", "instruction": "Forward the thread to sam@x.com"},
]


def _todo_row(conn, todo_id):
    row = conn.execute(
        "SELECT todo_id, title, suggested_action, reasoning, importance, due_date, source, "
        "account_id, action_options, source_meta FROM todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    cols = ["todo_id", "title", "suggested_action", "reasoning", "importance", "due_date",
            "source", "account_id", "action_options", "source_meta"]
    return dict(zip(cols, row))


def test_ensure_action_options() -> None:
    from agent import action_options as ao
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com")

    with mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen:
        got = ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("generates when no cache", gen.call_count == 1 and got == ACTIONS)
        cached = conn.execute("SELECT action_options FROM todos WHERE todo_id = ?", (tid,)).fetchone()[0]
        check("writes the cache", json.loads(cached) == ACTIONS)

        got = ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("cache hit skips generation", gen.call_count == 1 and got == ACTIONS)

        ao.ensure_action_options(conn, _todo_row(conn, tid), uid, refresh=True)
        check("refresh regenerates", gen.call_count == 2)

    conn.execute("UPDATE todos SET action_options = 'not json' WHERE todo_id = ?", (tid,))
    conn.commit()
    with mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen:
        ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("corrupt cache regenerates", gen.call_count == 1)

    with mock.patch.object(ao, "generate_action_options", side_effect=RuntimeError("boom")):
        conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = ?", (tid,))
        conn.commit()
        try:
            ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
            check("generation failure propagates", False)
        except RuntimeError:
            check("generation failure propagates", True)
```

Call `test_ensure_action_options()` from `main()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `AttributeError: module 'agent.action_options' has no attribute 'ensure_action_options'`.

- [ ] **Step 3: Implement `ensure_action_options`**

Append to `agent/action_options.py` (add `import sqlite3` and `from datetime import datetime, timezone` at the top alongside the existing imports):

```python
def ensure_action_options(
    conn: sqlite3.Connection, todo: dict, user_id: str, refresh: bool = False
) -> list[dict]:
    """The cached options for `todo`, generating and caching them if needed.

    Shared by the detail-pane route and the poller's push notification so the
    inference is paid for once per todo whichever side asks first. `todo` is a
    row dict carrying `todo_id` and the raw `action_options` column. Raises on
    generation failure; the caller decides how to surface that.
    """
    if todo.get("action_options") and not refresh:
        try:
            cached = json.loads(todo["action_options"])
            if isinstance(cached, list):
                return cached
        except ValueError:
            pass  # Corrupt cache — fall through and regenerate.

    actions = generate_action_options(todo, user_id)
    conn.execute(
        "UPDATE todos SET action_options = ?, updated_at = ? "
        "WHERE todo_id = ? AND user_id = ?",
        (json.dumps(actions), datetime.now(timezone.utc).isoformat(),
         todo["todo_id"], user_id),
    )
    conn.commit()
    return actions
```

- [ ] **Step 4: Make the route a thin wrapper**

Replace the body of `todo_action_options` in `app.py` (from `if row["action_options"] and ...` through `return jsonify({"actions": actions})`) with:

```python
    # Imported lazily so the OpenAI client is only built by workers that use it.
    from agent.action_options import ensure_action_options

    todo = dict(row)
    todo["todo_id"] = todo_id
    try:
        actions = ensure_action_options(
            db, todo, user_id, refresh=request.args.get("refresh") == "1"
        )
    except Exception as exc:
        app.logger.exception("Failed to generate action options")
        # 200 with an error field: the pane renders this inline next to a retry,
        # which is more useful than a silent empty section.
        return jsonify({"actions": [], "error": str(exc)})
    return jsonify({"actions": actions})
```

Leave `app.py`'s `json`/`datetime` imports alone — both are still used elsewhere.

- [ ] **Step 5: Run the tests**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/verify/verify_actions.py && python scripts/verify/verify_web.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add agent/action_options.py app.py scripts/verify/verify_push.py
git commit -m "refactor: share action-option caching between the route and the poller

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `push_notify.py` — configuration, payload, sending

**Files:**
- Create: `push_notify.py`
- Create: `scripts/gen_vapid_keys.py`
- Modify: `requirements.txt` (add `pywebpush>=2.0`), `.env.example` (VAPID block after `ENABLED_SOURCES`)
- Test: `scripts/verify/verify_push.py`

**Interfaces:**
- Consumes: `list_push_subscriptions`, `delete_push_subscription` (Task 2); `ensure_action_options` (Task 3)
- Produces:
  - `configured() -> bool`
  - `public_key() -> str`
  - `build_payload(todo: dict, actions: list[dict]) -> dict` — `{todo_id, title, body, url, actions: [{index, label}]}`
  - `send_to_user(conn, user_id: str, payload: dict) -> int` — sends to every subscription, prunes 404/410, returns successful sends; never raises
  - `notify_new_todo(conn, user_id: str, todo_id: str) -> int` — the poller entry point; never raises

- [ ] **Step 1: Write the failing test**

Add to `scripts/verify/verify_push.py`:

```python
VAPID_ENV = {
    "VAPID_PRIVATE_KEY": "x" * 43,
    "VAPID_PUBLIC_KEY": "y" * 87,
    "VAPID_SUBJECT": "mailto:dev@example.com",
}


class _PushError(Exception):
    def __init__(self, status):
        self.response = mock.Mock(status_code=status)


def test_push_notify() -> None:
    import push_notify
    from agent import action_options as ao

    with mock.patch.dict(os.environ, {"VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "", "VAPID_SUBJECT": ""}):
        check("not configured without keys", push_notify.configured() is False)
    with mock.patch.dict(os.environ, VAPID_ENV):
        check("configured with all three", push_notify.configured() is True)

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob about the Q4 deck"), uid, "a@x.com")

    payload = push_notify.build_payload(_todo_row(conn, tid), ACTIONS)
    check("payload title names the source", payload["title"] == "New todo · gmail")
    check("payload body flags high importance", payload["body"] == "[high] Reply to Bob about the Q4 deck")
    check("payload url deep-links the todo", payload["url"] == f"/#todo/{tid}")
    check("payload actions carry index and label only",
          payload["actions"] == [{"index": i, "label": a["label"]} for i, a in enumerate(ACTIONS)])
    check("payload never carries instructions", "instruction" not in json.dumps(payload))

    # No subscription: nothing generated, nothing sent.
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(push_notify, "webpush") as wp:
        check("no subscription → 0 sends", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("no subscription → no LLM call", gen.call_count == 0)
        check("no subscription → no webpush", wp.call_count == 0)

    save_push_subscription(conn, uid, SUB_A, "Chrome")
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(push_notify, "webpush") as wp:
        check("one subscription → 1 send", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("actions generated once", gen.call_count == 1)
        sent = json.loads(wp.call_args.kwargs["data"])
        check("sent payload is the todo's", sent["todo_id"] == tid and len(sent["actions"]) == 3)
        check("subscription info passed through",
              wp.call_args.kwargs["subscription_info"]["endpoint"] == SUB_A["endpoint"])
        check("ttl is a day", wp.call_args.kwargs["ttl"] == 86400)
        cached = conn.execute("SELECT action_options FROM todos WHERE todo_id = ?", (tid,)).fetchone()[0]
        check("actions cached for the detail pane", json.loads(cached) == ACTIONS)

        push_notify.notify_new_todo(conn, uid, tid)
        check("second notify uses the cache", gen.call_count == 1)

    # Generation failure: still notify, without buttons.
    conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = ?", (tid,))
    conn.commit()
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", side_effect=RuntimeError("boom")), \
         mock.patch.object(push_notify, "webpush") as wp:
        check("generator failure still sends", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("…with no actions", json.loads(wp.call_args.kwargs["data"])["actions"] == [])

    # Push-service responses.
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "WebPushException", _PushError), \
         mock.patch.object(push_notify, "webpush", side_effect=_PushError(410)):
        check("410 → 0 sends", push_notify.send_to_user(conn, uid, payload) == 0)
        check("410 prunes the subscription", count_push_subscriptions(conn, uid) == 0)

    save_push_subscription(conn, uid, SUB_A, "Chrome")
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "WebPushException", _PushError), \
         mock.patch.object(push_notify, "webpush", side_effect=_PushError(500)):
        check("500 → 0 sends", push_notify.send_to_user(conn, uid, payload) == 0)
        check("500 keeps the subscription", count_push_subscriptions(conn, uid) == 1)

    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "webpush", side_effect=OSError("network down")):
        check("unexpected error is swallowed", push_notify.send_to_user(conn, uid, payload) == 0)

    with mock.patch.dict(os.environ, {"VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "", "VAPID_SUBJECT": ""}), \
         mock.patch.object(push_notify, "webpush") as wp:
        check("unconfigured → nothing sent", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("unconfigured → webpush never called", wp.call_count == 0)

    check("unknown todo is a no-op", push_notify.notify_new_todo(conn, uid, "todo_nope") == 0)
```

Call `test_push_notify()` from `main()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `ModuleNotFoundError: No module named 'push_notify'`.

- [ ] **Step 3: Add the dependency**

```bash
source venv/bin/activate && pip install -q "pywebpush>=2.0"
```

Append to `requirements.txt`:

```
pywebpush>=2.0
```

- [ ] **Step 4: Write `push_notify.py`**

```python
"""Web Push for newly discovered todos.

The poller calls `notify_new_todo` right after a save. It returns before any
LLM call when the user has no enrolled browser — action inference is paid for
only when someone will see the buttons — and otherwise ensures the todo's
three options are cached and pushes a small payload to every subscription.

The payload carries action *labels* and indices, never instructions: the
service worker sends the index back to `/ask-ai`, and the server runs the
instruction it cached. A push can't put words in the agent's mouth.

Nothing here may take down a poll cycle: every failure is logged and swallowed.
"""
import json
import logging
import os
import sqlite3

from pywebpush import WebPushException, webpush

from db import delete_push_subscription, list_push_subscriptions

log = logging.getLogger("push_notify")

_TITLE_LIMIT = 120
TTL_SECONDS = 86400  # a device offline for a day still gets it
_warned_unconfigured = False


def configured() -> bool:
    return all(os.environ.get(k) for k in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT"))


def public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "")


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_payload(todo: dict, actions: list[dict]) -> dict:
    body = _truncate(todo.get("title") or "", _TITLE_LIMIT) or "(untitled)"
    if todo.get("importance") == "high":
        body = f"[high] {body}"
    return {
        "todo_id": todo["todo_id"],
        "title": f"New todo · {todo.get('source') or 'inbox'}",
        "body": body,
        "url": f"/#todo/{todo['todo_id']}",
        "actions": [
            {"index": i, "label": _truncate(a.get("label") or "", 40)}
            for i, a in enumerate(actions)
            if a.get("label")
        ],
    }


def send_to_user(conn: sqlite3.Connection, user_id: str, payload: dict) -> int:
    """Push `payload` to every browser the user enrolled. Returns successful
    sends. A 404/410 from the push service means the browser unsubscribed and
    the row is pruned; anything else is logged and skipped."""
    global _warned_unconfigured
    if not configured():
        if not _warned_unconfigured:
            log.info("VAPID keys not set; push notifications disabled")
            _warned_unconfigured = True
        return 0
    data = json.dumps(payload)
    sent = 0
    for sub in list_push_subscriptions(conn, user_id):
        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
                ttl=TTL_SECONDS,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                log.info("push subscription gone (%s); pruning %s", status, sub["endpoint"][:40])
                delete_push_subscription(conn, sub["endpoint"])
            else:
                log.warning("push failed (%s): %s", status, exc)
        except Exception as exc:  # network, bad key, anything — never propagate
            log.warning("push failed: %s", exc)
    return sent


def _load_todo(conn: sqlite3.Connection, user_id: str, todo_id: str) -> dict | None:
    cols = ["todo_id", "title", "suggested_action", "reasoning", "importance", "due_date",
            "source", "account_id", "action_options", "source_meta"]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
    return dict(zip(cols, row)) if row else None


def notify_new_todo(conn: sqlite3.Connection, user_id: str, todo_id: str) -> int:
    """Push a new-todo notification to every browser the user enrolled.
    Returns the number of successful sends. Never raises."""
    try:
        if not configured() or not list_push_subscriptions(conn, user_id):
            return 0
        todo = _load_todo(conn, user_id, todo_id)
        if todo is None:
            return 0
        # Imported lazily so the poller only builds the OpenAI client for this
        # when a subscribed user actually gets a new todo.
        from agent.action_options import ensure_action_options
        try:
            actions = ensure_action_options(conn, todo, user_id)
        except Exception as exc:
            log.warning("action options failed for %s; pushing without buttons: %s", todo_id, exc)
            actions = []
        return send_to_user(conn, user_id, build_payload(todo, actions))
    except Exception as exc:
        log.warning("notify_new_todo failed for %s: %s", todo_id, exc)
        return 0
```

- [ ] **Step 5: Write `scripts/gen_vapid_keys.py`**

```python
"""Print a fresh VAPID key pair in .env form. Run once per deployment:

    python scripts/gen_vapid_keys.py >> .env

Rotating the keys invalidates every existing push subscription — users
re-enable notifications from Settings.
"""
import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    vapid = Vapid()
    vapid.generate_keys()
    private = _b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64url(vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    ))
    print(f"VAPID_PRIVATE_KEY={private}")
    print(f"VAPID_PUBLIC_KEY={public}")
    print("VAPID_SUBJECT=mailto:you@example.com")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Document the config**

In `.env.example`, after the `ENABLED_SOURCES=` line and its blank line, add:

```
# Web Push (browser notifications for new todos, with the suggested actions
# as buttons). Generate once per deployment: python scripts/gen_vapid_keys.py
# All three must be set; otherwise push is off and Settings says so.
# VAPID_SUBJECT is a mailto: the push services can contact.
VAPID_PRIVATE_KEY=
VAPID_PUBLIC_KEY=
VAPID_SUBJECT=

```

- [ ] **Step 7: Run the tests and the generator**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/gen_vapid_keys.py`
Expected: all `PASS`; the generator prints three `VAPID_*=` lines (private key 43 chars, public 87 chars).

- [ ] **Step 8: Commit**

```bash
git add push_notify.py scripts/gen_vapid_keys.py requirements.txt .env.example scripts/verify/verify_push.py
git commit -m "feat: push_notify — Web Push a new todo with its suggested actions

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Web routes — subscribe, public key, test, settings field

**Files:**
- Modify: `app.py` — new routes after `update_source_settings` (~line 890); `get_settings` (~line 745); `from db import (...)` block at the top
- Modify: `auth.py:189-198` (`_wants_json_response`)
- Test: `scripts/verify/verify_push.py`

**Interfaces:**
- Consumes: `push_notify.configured()`, `push_notify.public_key()`, `push_notify.send_to_user()`; `save_push_subscription`, `delete_push_subscription`, `count_push_subscriptions`
- Produces routes: `GET /push/vapid-public-key` → `{"key"}` | 503; `POST /push/subscribe` → `{"ok": true}` | 400 | 503; `DELETE /push/subscribe` → `{"ok": true}`; `POST /push/test` → `{"sent": n}`; `/settings` JSON gains `"notifications": {"configured": bool, "subscription_count": int}`

- [ ] **Step 1: Write the failing test**

Add to `scripts/verify/verify_push.py`:

```python
def _client(uid):
    import app as app_module
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["user_email"] = "dev@example.com"
    return client


def test_push_routes() -> None:
    import push_notify
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    conn.close()
    client = _client(uid)

    with mock.patch.dict(os.environ, {"VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "", "VAPID_SUBJECT": ""}):
        check("public key 503 when unconfigured", client.get("/push/vapid-public-key").status_code == 503)
        check("subscribe 503 when unconfigured",
              client.post("/push/subscribe", json=SUB_A).status_code == 503)
        s = client.get("/settings").get_json()["notifications"]
        check("settings reports unconfigured", s == {"configured": False, "subscription_count": 0})

    with mock.patch.dict(os.environ, VAPID_ENV):
        r = client.get("/push/vapid-public-key")
        check("public key served", r.status_code == 200 and r.get_json()["key"] == VAPID_ENV["VAPID_PUBLIC_KEY"])
        check("subscribe rejects a bad body",
              client.post("/push/subscribe", json={"endpoint": "x"}).status_code == 400)
        r = client.post("/push/subscribe", json=SUB_A, headers={"User-Agent": "Chrome/T"})
        check("subscribe stores", r.status_code == 200 and r.get_json()["ok"] is True)
        s = client.get("/settings").get_json()["notifications"]
        check("settings counts the subscription", s == {"configured": True, "subscription_count": 1})

        with mock.patch.object(push_notify, "webpush") as wp:
            r = client.post("/push/test")
            check("test sends to the caller", r.get_json()["sent"] == 1)
            sent = json.loads(wp.call_args.kwargs["data"])
            check("test payload has no todo and no actions",
                  sent["url"] == "/" and sent["actions"] == [] and "todo_id" not in sent)

        r = client.delete("/push/subscribe", json={"endpoint": SUB_A["endpoint"]})
        check("unsubscribe ok", r.get_json()["ok"] is True)
        s = client.get("/settings").get_json()["notifications"]
        check("settings back to zero", s["subscription_count"] == 0)

    import app as app_module
    anon = app_module.app.test_client()
    check("public key is JSON-401 when logged out",
          anon.get("/push/vapid-public-key").status_code == 401)
```

Call `test_push_routes()` from `main()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `FAIL  public key 503 when unconfigured` (404 today).

- [ ] **Step 3: Add the routes**

In `app.py`, after `update_source_settings`:

```python
# ---------------------------------------------------------------------------
# Push notifications — per-browser enrollment. The poller does the sending
# (push_notify.notify_new_todo); these routes only manage subscriptions.
# ---------------------------------------------------------------------------


@app.route("/push/vapid-public-key", methods=["GET"])
@login_required
def push_public_key():
    import push_notify
    if not push_notify.configured():
        return jsonify({"error": "push notifications are not configured on this server"}), 503
    return jsonify({"key": push_notify.public_key()})


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    import push_notify
    if not push_notify.configured():
        return jsonify({"error": "push notifications are not configured on this server"}), 503
    data = request.get_json(force=True, silent=True) or {}
    keys = data.get("keys") or {}
    if not all(isinstance(v, str) and v for v in (data.get("endpoint"), keys.get("p256dh"), keys.get("auth"))):
        return jsonify({"error": "invalid subscription"}), 400
    user_id = current_user_id()
    assert user_id
    save_push_subscription(get_db(), user_id, data, request.headers.get("User-Agent"))
    return jsonify({"ok": True})


@app.route("/push/subscribe", methods=["DELETE"])
@login_required
def push_unsubscribe():
    data = request.get_json(force=True, silent=True) or {}
    endpoint = data.get("endpoint")
    user_id = current_user_id()
    assert user_id
    if isinstance(endpoint, str) and endpoint:
        delete_push_subscription(get_db(), endpoint, user_id=user_id)
    return jsonify({"ok": True})


@app.route("/push/test", methods=["POST"])
@login_required
def push_test():
    """Send a sample notification so the user can confirm the pipe end to end."""
    import push_notify
    user_id = current_user_id()
    assert user_id
    payload = {
        "title": "Action Inbox",
        "body": "Notifications are working. New todos will show up here.",
        "url": "/",
        "actions": [],
    }
    return jsonify({"sent": push_notify.send_to_user(get_db(), user_id, payload)})
```

Add `save_push_subscription, delete_push_subscription, count_push_subscriptions` to the `from db import (...)` block at the top of `app.py`.

In `get_settings`, add a top-level key beside `"sources"`:

```python
    import push_notify
    return jsonify({
        "sources": { ... unchanged ... },
        "notifications": {
            "configured": push_notify.configured(),
            "subscription_count": count_push_subscriptions(db, user_id),
        },
    })
```

- [ ] **Step 4: JSON 401 for the public-key GET**

In `auth.py`, `_wants_json_response`:

```python
    # GET endpoints called by fetch() in the SPA.
    # /settings/sources/gmail/auth is a redirect flow, NOT JSON.
    if request.path in ("/settings", "/push/vapid-public-key"):
        return True
```

- [ ] **Step 5: Run the tests**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/verify/verify_web.py && python scripts/verify/verify_connections.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app.py auth.py scripts/verify/verify_push.py
git commit -m "feat(web): push subscription routes and settings field

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: `/ask-ai` accepts `action_index`

**Files:**
- Modify: `app.py:517-570` (`ask_ai`)
- Test: `scripts/verify/verify_push.py`

**Interfaces:**
- Consumes: existing `runs.start(user_id, todo_id, thread, work) -> Run`; `_resolution_work(todo, thread, user_message, user_id, state, from_suggestion)`
- Produces: `POST /todos/<id>/ask-ai` with body `{"action_index": n}` behaves as `{"message": action_options[n].instruction, "from_suggestion": true}`; 400 `{"error": …}` when no cache or index out of range.

- [ ] **Step 1: Write the failing test**

Add to `scripts/verify/verify_push.py`:

```python
def test_ask_ai_action_index() -> None:
    import app as app_module
    from agent import runs
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e9", "m9", "th9", _gmail_result("Book the Booth deposit"), uid, "a@x.com")
    conn.close()
    client = _client(uid)

    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 0})
    check("no cache → 400", r.status_code == 400)

    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute("UPDATE todos SET action_options = ? WHERE todo_id = ?", (json.dumps(ACTIONS), tid))
    conn.commit()
    conn.close()

    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 5})
    check("out-of-range index → 400", r.status_code == 400)
    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": "1"})
    check("non-integer index → 400", r.status_code == 400)

    fake_run = mock.Mock(status="running", run_id="r1", thread=[], activity_snapshot=lambda: [])
    with mock.patch.object(app_module, "_resolution_work", return_value=lambda *a, **k: None) as work, \
         mock.patch.object(runs, "start", return_value=fake_run) as start:
        r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 1})
        check("valid index starts a run", r.status_code == 200 and start.call_count == 1)
        check("run seeded with the cached instruction",
              start.call_args.args[2][-1] == {"role": "user", "content": ACTIONS[1]["instruction"]})
        check("instruction passed to the executor as a suggestion",
              work.call_args.args[2] == ACTIONS[1]["instruction"] and work.call_args.args[5] is True)
```

Call `test_ask_ai_action_index()` from `main()`.

- [ ] **Step 2: Run it to verify it fails**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py`
Expected: `FAIL  no cache → 400` (today an `action_index`-only body is treated as "no message" and returns 200 `idle`).

- [ ] **Step 3: Resolve `action_index` in `ask_ai`**

In `ask_ai`, directly after the existing `from_suggestion = bool(data.get("from_suggestion"))` line, add:

```python
    # A notification button sends the option's index, never its text, so the
    # instruction that runs is always the one this server cached.
    if "action_index" in data:
        idx = data["action_index"]
        try:
            options = json.loads(row["action_options"] or "null")
        except ValueError:
            options = None
        if not isinstance(idx, int) or isinstance(idx, bool) or not isinstance(options, list) \
                or not 0 <= idx < len(options):
            return jsonify({"error": "no such action"}), 400
        user_message = (options[idx].get("instruction") or "").strip()
        if not user_message:
            return jsonify({"error": "no such action"}), 400
        from_suggestion = True
```

- [ ] **Step 4: Run the tests**

Run: `source venv/bin/activate && python scripts/verify/verify_push.py && python scripts/verify/verify_executor.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app.py scripts/verify/verify_push.py
git commit -m "feat(web): /ask-ai runs a cached action by index

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Poller call sites

**Files:**
- Modify: `main.py:26,98-104`, `pollers/fathom/poller.py:7,42-44`, `pollers/browser/poller.py:12,243-246`, `pollers/system/poller.py:8,48-51`

**Interfaces:**
- Consumes: `push_notify.notify_new_todo(conn, user_id, todo_id) -> int`; `save_*_todo(...) -> str | None`

- [ ] **Step 1: Wire the four sites**

`main.py` — add `from push_notify import notify_new_todo` after the `from db import ...` line, and in `_poll_gmail_account`:

```python
        if saved:
            counts["todo"] += 1
            print(f"{prefix} → TODO[{todo.get('importance')}] {_truncate(todo['title'], 60)}")
            notify_new_todo(conn, user_id, saved)
```

`pollers/fathom/poller.py` — add `from push_notify import notify_new_todo` after the `from db import ...` line, and:

```python
        for idx, item in enumerate(action_items):
            todo_id = save_fathom_todo(conn, user_id, meeting, idx, item)
            if todo_id:
                notify_new_todo(conn, user_id, todo_id)
            saved += 1
```

`pollers/browser/poller.py` — add `from push_notify import notify_new_todo` after `from pollers.browser import generator as browser_history_generator`, and:

```python
        todo_id = save_browser_history_todo(conn, user_id, todo)
        if todo_id:
            saved += 1
            log.info("saved: %r | reasoning: %s", title, reasoning)
            notify_new_todo(conn, user_id, todo_id)
        else:
            log.info("skipped (duplicate): %r", title)
```

`pollers/system/poller.py` — add `from push_notify import notify_new_todo` after `from pollers.system import snapshot as system_snapshot`, and:

```python
        todo_id = save_system_todo(conn, user_id, todo)
        if todo_id:
            saved += 1
            print(f"[system] saved: {todo.get('title')!r}")
            notify_new_todo(conn, user_id, todo_id)
```

- [ ] **Step 2: Verify imports and the existing scripts**

Run: `source venv/bin/activate && python -c "import main, pollers.fathom.poller, pollers.browser.poller, pollers.system.poller; print('imports ok')" && python scripts/verify/verify_push.py`
Expected: `imports ok`, all `PASS`.

- [ ] **Step 3: Commit**

```bash
git add main.py pollers
git commit -m "feat(poller): push a notification for every newly saved todo

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Service worker — `push` and `notificationclick`

**Files:**
- Modify: `static/js/sw.js` (`VERSION` → `'v8'`; append handlers at the end)

**Interfaces:**
- Consumes: payload `{todo_id?, title, body, url, actions: [{index, label}]}`; `POST /todos/<id>/ask-ai {action_index}` (Task 6)

- [ ] **Step 1: Bump the cache version**

```js
const VERSION = 'v8';
```

- [ ] **Step 2: Append the push handlers**

```js
// ---------------- Push notifications ----------------
// The poller sends {todo_id, title, body, url, actions:[{index,label}]}. Only
// the label travels; a button click sends the index back and the server runs
// the instruction it cached, so a payload can't put words in the agent's mouth.

self.addEventListener('push', (event) => {
  let payload = null;
  try { payload = event.data ? event.data.json() : null; } catch (e) { payload = null; }
  if (!payload || typeof payload !== 'object') {
    payload = { title: 'New todo', body: '', url: '/', actions: [] };
  }
  const maxActions = Number.isInteger(Notification.maxActions) ? Notification.maxActions : 2;
  const actions = (payload.actions || []).slice(0, maxActions).map((a) => ({
    action: `run:${a.index}`,
    title: a.label,
  }));
  event.waitUntil(self.registration.showNotification(payload.title || 'New todo', {
    body: payload.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    // Same todo re-pushed replaces its banner instead of stacking a second one.
    tag: payload.todo_id || 'action-inbox',
    data: payload,
    actions,
  }));
});

self.addEventListener('notificationclick', (event) => {
  const payload = event.notification.data || {};
  event.notification.close();
  const target = new URL(payload.url || '/', self.location.origin).href;
  const m = /^run:(\d+)$/.exec(event.action || '');

  const startRun = (m && payload.todo_id)
    ? fetch(`/todos/${encodeURIComponent(payload.todo_id)}/ask-ai`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_index: Number(m[1]) }),
      }).catch(() => null)
    : Promise.resolve(null);

  // Whether or not the run started (401 when the session expired, 400 when
  // the cache changed), bring the todo in front of the user: an open window
  // is navigated and focused, otherwise a new one is opened.
  const show = self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    .then((wins) => {
      const win = wins.find((w) => new URL(w.url).origin === self.location.origin);
      if (win) return win.navigate(target).then((w) => (w || win).focus()).catch(() => win.focus());
      return self.clients.openWindow(target);
    });

  event.waitUntil(startRun.then(() => show));
});
```

- [ ] **Step 3: Syntax check**

Run: `node --check static/js/sw.js && echo ok`
Expected: `ok`. (If `node` is not installed, open `http://localhost:5001/sw.js` in the browser after Task 9 and check the DevTools console for parse errors.)

- [ ] **Step 4: Commit**

```bash
git add static/js/sw.js
git commit -m "feat(sw): show pushed todos with action buttons; run one on click

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Settings card — enable, disable, send test

**Files:**
- Modify: `templates/index.html:196-224` (add a card after the Fathom card, inside `#settings-body`)
- Modify: `static/js/app.js:1267-1273` (`openSettingsModal`) and append handlers after the Fathom handlers (~line 1300)

**Interfaces:**
- Consumes: `GET /settings` → `notifications: {configured, subscription_count}`; `GET /push/vapid-public-key`; `POST`/`DELETE /push/subscribe`; `POST /push/test`

- [ ] **Step 1: Add the card markup**

In `templates/index.html`, after the closing `</div>` of `#fathom-card` (before `</div>` of `#settings-body`):

```html
      <div class="source-card" id="push-card">
        <div class="source-card-header">
          <span class="source-card-name">Browser notifications</span>
          <span class="source-connected-badge off" id="push-status">Off</span>
        </div>
        <div class="source-card-body">
          <span class="source-card-hint" id="push-hint">Get a notification for every new todo, with its suggested actions as buttons.</span>
          <div class="source-key-row" id="push-connect-row">
            <button class="btn-connect" id="push-enable-btn">Enable in this browser</button>
          </div>
          <div id="push-connected-row">
            <button class="btn-connect" id="push-test-btn">Send test</button>
            <button class="btn-disconnect" id="push-disable-btn">Disable</button>
          </div>
        </div>
      </div>
```

- [ ] **Step 2: Wire the card in `app.js`**

Replace `openSettingsModal` with:

```js
function openSettingsModal() {
  settingsModal.classList.add('open');
  fetch('/settings').then(r => r.json()).then(data => {
    const { fathom, gmail } = data.sources;
    setSourceConnected('fathom', fathom.connected, fathom.api_key_preview);
    renderGmailAccounts(gmail.accounts || []);
    renderPushCard(data.notifications || { configured: false, subscription_count: 0 });
  });
}
```

Append after the Fathom disconnect handler:

```js
// ---------------- Browser notifications ----------------
// Enrolment is per browser: the subscription lives in this browser's push
// manager and a copy on the server. State is read from the browser, not the
// server, so a subscription revoked elsewhere never shows as "on" here.

function urlBase64ToUint8Array(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function currentPushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

function setPushState(on, hint) {
  const status = document.getElementById('push-status');
  status.textContent = on ? 'On' : 'Off';
  status.className = `source-connected-badge ${on ? 'on' : 'off'}`;
  document.getElementById('push-connect-row').style.display = on ? 'none' : 'flex';
  document.getElementById('push-connected-row').style.display = on ? 'flex' : 'none';
  if (hint !== undefined) document.getElementById('push-hint').textContent = hint;
}

async function renderPushCard(info) {
  const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!info.configured || !supported) {
    setPushState(false, !info.configured
      ? 'Not configured on this server (VAPID keys missing).'
      : 'This browser does not support push notifications.');
    document.getElementById('push-connect-row').style.display = 'none';
    return;
  }
  if (Notification.permission === 'denied') {
    setPushState(false, 'Blocked in browser settings — allow notifications for this site to enable.');
    document.getElementById('push-connect-row').style.display = 'none';
    return;
  }
  const sub = await currentPushSubscription();
  setPushState(!!sub, sub
    ? 'This browser is enrolled. New todos arrive as notifications with their suggested actions.'
    : 'Get a notification for every new todo, with its suggested actions as buttons.');
}

document.getElementById('push-enable-btn').addEventListener('click', async () => {
  const btn = document.getElementById('push-enable-btn');
  btn.disabled = true;
  try {
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      renderPushCard({ configured: true });
      return;
    }
    const { key } = await fetch('/push/vapid-public-key').then(r => r.json());
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
    const r = await fetch('/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    if (!r.ok) throw new Error((await r.json()).error || 'subscribe failed');
    await renderPushCard({ configured: true });
  } catch (err) {
    alert(`Couldn't enable notifications: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('push-disable-btn').addEventListener('click', async () => {
  const sub = await currentPushSubscription();
  if (sub) {
    const endpoint = sub.endpoint;
    await sub.unsubscribe().catch(() => null);
    await fetch('/push/subscribe', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint }),
    }).catch(() => null);
  }
  await renderPushCard({ configured: true });
});

document.getElementById('push-test-btn').addEventListener('click', async () => {
  const btn = document.getElementById('push-test-btn');
  btn.disabled = true;
  try {
    const { sent } = await fetch('/push/test', { method: 'POST' }).then(r => r.json());
    document.getElementById('push-hint').textContent = sent
      ? 'Test sent — it should appear in a moment.'
      : 'Nothing sent — this browser may have unsubscribed. Disable and enable again.';
  } finally {
    btn.disabled = false;
  }
});
```

- [ ] **Step 3: Syntax check and verify the page still renders**

Run: `node --check static/js/app.js && echo ok && source venv/bin/activate && python scripts/verify/verify_web.py`
Expected: `ok`, all `PASS`.

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/js/app.js
git commit -m "feat(ui): browser-notifications card in Settings

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: Docs, keys, and end-to-end check

**Files:**
- Modify: `CLAUDE.md` (verify list; architecture section), `.env` (local keys — not committed)

- [ ] **Step 1: Document in `CLAUDE.md`**

In the verify-script block, add after the `verify_clarify.py` line:

```
python scripts/verify/verify_push.py       # stubs pywebpush + the OpenAI call; no spend
```

In the Architecture section, after the morning-digest paragraph (the one ending "instead of nothing."), add:

```markdown
**Push notifications** (`push_notify.py`): each poller calls `notify_new_todo(conn, user_id,
todo_id)` right where it branches on the `save_*_todo` return value (which is the `todo_id`,
or `None` on a dedup), so only a real insert notifies. It returns before any LLM call when the
user has no push subscription — eager action inference is paid for only when someone will see
the buttons — otherwise `agent.action_options.ensure_action_options` fills the same cache the
detail pane reads, and the payload (labels and indices, never instructions) goes out via
`pywebpush` to every browser the user enrolled from Settings. `static/js/sw.js` shows up to
`Notification.maxActions` of them (2 on Chrome/macOS, 0 on Safari) as buttons; a click POSTs
`{action_index}` to `/ask-ai`, which runs the instruction *it* cached with
`from_suggestion=True` — a push can't put words in the agent's mouth. Then it focuses the app
at `#todo/<id>` so the live trace is in front of the user. Subscriptions live in
`push_subscriptions` keyed on the push endpoint; a 404/410 from the push service prunes the
row. Needs `VAPID_PRIVATE_KEY`/`VAPID_PUBLIC_KEY`/`VAPID_SUBJECT` (`scripts/gen_vapid_keys.py`);
unset ⇒ Settings says so and the poller logs once and skips. Every failure is swallowed — a
notification must never cost a poll cycle.
```

- [ ] **Step 2: Generate local keys**

```bash
source venv/bin/activate && python scripts/gen_vapid_keys.py >> .env
```

Then edit the appended `VAPID_SUBJECT=mailto:you@example.com` line in `.env` to the user's address. `.env` is gitignored — confirm with `git status --short .env` (no output).

- [ ] **Step 3: Run the whole verify suite**

Run:

```bash
source venv/bin/activate && for s in verify_migration verify_connections verify_links verify_cursors verify_web verify_executor verify_actions verify_hermes_activity verify_clarify verify_push; do python scripts/verify/$s.py > /dev/null && echo "$s ok" || echo "$s FAIL"; done
```

Expected: ten `ok` lines.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: push notifications with suggested actions

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Manual end-to-end (Flask is already running under `--debug`; restart the poller)**

1. Restart `python main.py` (the poller tab) so it loads `push_notify`.
2. Open `http://localhost:5001`, hard-reload once so `sw.js` v8 activates (DevTools → Application → Service Workers shows v8 / no "waiting").
3. Settings → Browser notifications → **Enable in this browser** → allow the Chrome prompt. Badge flips to **On**.
4. **Send test** → a banner "Action Inbox / Notifications are working…" appears. Clicking it focuses the app.
5. Trigger a real todo (send yourself an email that reads like a request, e.g. "Can you send me the Q4 deck by Friday?"). Within ~60s the poller logs `→ TODO[...]`, and a banner "New todo · gmail" appears with two action buttons (hover the banner / click the ⌄ on macOS to reveal them).
6. Click one → the app comes to the front at that todo with the run in progress and the chosen chip marked.
7. Record the outcome (which browser, which buttons showed, whether the run started) in the PR description.

---

## Self-review

**Spec coverage:** Data (Task 2, Task 1 for the return type) ✓ · Configuration + `gen_vapid_keys.py` (Task 4) ✓ · Poller side incl. no-subscription early return, generation-failure fallback, 404/410 pruning, `ttl` (Task 4) and call sites (Task 7) ✓ · `ensure_action_options` and thin route (Task 3) ✓ · Web routes, settings field, JSON-401 allowlist (Task 5) ✓ · `/ask-ai action_index` (Task 6) ✓ · Settings card (Task 9) ✓ · `sw.js` v8 handlers incl. `tag`, `maxActions`, focus-or-open, non-2xx → open anyway (Task 8) ✓ · Removal of `notify.py`, verify script, env entry, CLAUDE.md paragraph (Task 1) ✓ · Testing list (Tasks 1–6 cover every bullet; manual in Task 10) ✓ · `requirements.txt`, `.env.example`, `CLAUDE.md` (Tasks 4, 10) ✓.

**Type consistency:** `notify_new_todo(conn, user_id, todo_id) -> int` used identically in Tasks 4 and 7. `ensure_action_options(conn, todo, user_id, refresh=False)` identical in Tasks 3, 4. `send_to_user(conn, user_id, payload)` identical in Tasks 4, 5. `delete_push_subscription(conn, endpoint, user_id=None)` identical in Tasks 2, 4, 5. Payload keys `todo_id/title/body/url/actions[{index,label}]` identical in Tasks 4, 8; the test payload in Task 5 omits `todo_id`, which Task 8 handles via the `'action-inbox'` tag fallback.
