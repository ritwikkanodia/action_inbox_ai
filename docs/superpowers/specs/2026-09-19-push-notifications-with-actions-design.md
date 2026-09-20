# Push notifications with suggested actions

**Date:** 2026-09-19
**Status:** Approved design, not yet implemented

## Problem

A user learns about a new todo only by opening the app (or from the next
morning's digest). The poller already knows the moment a todo is saved, and
the app already knows the three ways to close it — but nothing brings either
to the user.

A first attempt (PR #26) fired a macOS banner from the poller via `osascript`
/ `terminal-notifier`. It could not carry action buttons (`osascript` has
none; `terminal-notifier` 3.x dropped them), a click just opened Script
Editor, it only worked on the machine running the poller, and it notified
every user's todos to whoever owned that machine. This design replaces it.

## Goals

- Every new todo produces a notification on the user's devices, wherever
  the poller runs — including the Railway deploy.
- The notification shows the todo's suggested actions as buttons. Clicking
  one starts the resolution run exactly as clicking the chip in the UI does.
- Clicking the banner itself opens the todo in the app.
- Each user gets only their own todos. Enrolling is the user's choice, per
  browser, from Settings.
- No LLM spend on action inference for a user who has no way to see it.

## Non-goals

- Three buttons on every platform. Chrome caps notification actions at 2 on
  macOS; Safari shows none. The banner body always opens the todo, where all
  three chips are.
- Handling `pushsubscriptionchange` (browser-initiated subscription
  rotation). Rare; the user re-enables from Settings.
- An importance filter. Every new todo pushes; a threshold is an easy knob
  later.
- A follow-up notification when the run finishes or stops to ask a
  clarifying question. Those land in the UI thread; a "needs your answer"
  push is a natural next step, not this one.
- A local (osascript) fallback. `notify.py` and its call sites are removed.

## Design

### Overview

```
poller (main.py)                     web (app.py)                 browser
────────────────                     ────────────                 ───────
save_*_todo → todo_id
   │
   ▼
push_notify.notify_new_todo
   ├─ no subscriptions? → return
   ├─ ensure_action_options ──────── shared with GET /todos/<id>/actions
   ├─ build payload
   └─ pywebpush.webpush ──▶ push service ──▶ sw.js `push` → showNotification
                                                          │
                                        POST /ask-ai ◀────┤ button: {action_index}
                                        {action_index}    └ body: open /#todo/<id>
```

The poller sends; the browser acts; the web app runs the turn. Auth is the
session cookie the service worker already has — no new credential, no
internal endpoint.

### Data

New table, created idempotently in `init_db`:

```sql
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint    TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions(user_id);
```

`endpoint` is the push service's unique URL for one browser profile, so
re-subscribing from the same browser is an upsert, not a duplicate.
`created_at` is ISO-8601 UTC like every other timestamp in `db.py`.

Helpers in `db.py`:

- `save_push_subscription(conn, user_id, subscription: dict, user_agent) -> None`
  — `subscription` is the browser's `PushSubscription.toJSON()`:
  `{endpoint, keys: {p256dh, auth}}`. `INSERT OR REPLACE` on endpoint.
- `list_push_subscriptions(conn, user_id) -> list[dict]` — rows as dicts.
- `delete_push_subscription(conn, endpoint) -> None`.
- `count_push_subscriptions(conn, user_id) -> int` — for Settings.

**`save_*_todo` return `str | None`** (the `todo_id`, or `None` when the
row was deduped or rejected) instead of `bool`. Every existing caller tests
truthiness, so they keep working; the poller needs the id to build the
payload. Applies to `save_todo`, `save_fathom_todo`,
`save_browser_history_todo`, `save_system_todo`. `_save_todo` returns the
id it was given on insert, `None` otherwise.

### Configuration

| Variable | Meaning |
|---|---|
| `VAPID_PRIVATE_KEY` | Base64url raw EC private key, as `pywebpush` expects |
| `VAPID_PUBLIC_KEY` | Matching uncompressed public key, base64url — sent to the browser |
| `VAPID_SUBJECT` | `mailto:` contact required by the push services |

`scripts/gen_vapid_keys.py` prints a fresh pair in `.env` form; run once per
deployment. `push_notify.configured()` is true when all three are set. When
not configured: `POST /push/subscribe` returns 503 with an explanatory
error, Settings shows "not configured on this server", and the poller skips
push after logging once at startup.

New dependency: `pywebpush` (brings `py-vapid`, `cryptography`).

### Poller side — `push_notify.py`

```python
def notify_new_todo(conn, user_id: str, todo_id: str) -> int:
    """Push a new-todo notification to every browser the user enrolled.
    Returns the number of successful sends. Never raises."""
```

Steps, in order:

1. `list_push_subscriptions`. **Empty ⇒ return 0 before anything else.**
   This is what keeps eager action inference from costing money for users
   who never enabled notifications.
2. Load the todo row. `ensure_action_options(conn, todo, user_id)` (below)
   returns the cached options or generates and caches them. A generation
   failure logs and continues with an empty list — the notification still
   goes out, just without buttons.
3. Build the payload:

   ```json
   {
     "todo_id": "todo_…",
     "title": "New todo · gmail",
     "body": "[high] Reply to Bob about the Q4 deck",
     "url": "/#todo/todo_…",
     "actions": [{"index": 0, "label": "Reply with dates"}, {"index": 1, "label": "…"}, {"index": 2, "label": "…"}]
   }
   ```

   `body` is the todo title truncated to 120 chars, prefixed `[high] ` when
   importance is high. Labels only — never instructions — so the payload
   stays small and carries nothing the agent will act on. `url` is relative;
   the service worker resolves it against its own origin.
4. `webpush()` to each subscription with `ttl=86400` (a device offline for a
   day still gets it). A `WebPushException` whose response status is 404 or
   410 means the browser unsubscribed: delete that row. Any other exception
   is logged and swallowed.

Called from the four poller save sites, in the `if saved:` branch, replacing
the `notify_new_todo(title, source, importance)` calls from PR #26:
`main.py` (Gmail), `pollers/fathom/poller.py`, `pollers/browser/poller.py`,
`pollers/system/poller.py`.

`ensure_action_options(conn, todo: dict, user_id, refresh=False) -> list[dict]`
moves the cache-then-generate logic out of `app.todo_action_options` into
`agent/action_options.py`: return the parsed `todos.action_options` when
present, valid, and `refresh` is false; otherwise call
`generate_action_options`, write the result back with `updated_at`, and
return it. Raises on generation failure (the route surfaces the error
inline; the poller catches it). The route becomes a thin wrapper and
`?refresh=1` keeps working.

### Web side — `app.py`

All new routes are `@login_required`; `_wants_json_response` already treats
non-GET as JSON, and `GET /push/vapid-public-key` is added to its GET
allowlist so a 401 comes back as JSON rather than a redirect.

- `GET /push/vapid-public-key` → `{"key": …}` (503 when not configured).
- `POST /push/subscribe` — body is `PushSubscription.toJSON()`. Validates
  `endpoint` and both keys are non-empty strings; upserts; `{"ok": true}`.
- `DELETE /push/subscribe` — body `{"endpoint"}`. Deletes only if the row
  belongs to the caller. `{"ok": true}` either way.
- `POST /push/test` — sends a fixed sample payload (no todo, no actions;
  `url: "/"`) to the caller's subscriptions. Returns `{"sent": n}`. Exists
  so a user can confirm the pipe end to end, and so verification of this
  feature does not depend on new mail arriving.
- `GET /settings` gains
  `"notifications": {"configured": bool, "subscription_count": int}`.
- `POST /todos/<id>/ask-ai` accepts `{"action_index": n}` as an alternative
  to `message`. The server reads `todos.action_options`, takes
  `[n].instruction`, and proceeds as if that text had been posted with
  `from_suggestion: true`. Missing cache or out-of-range index ⇒ 400. The
  service worker sends the index and never the text, so the instruction that
  runs is always the one the server cached — a push payload cannot put words
  in the agent's mouth. `static/js/app.js` keeps sending the text; both paths
  land in the same `runs.start`.

### Frontend

**Settings modal** — new "Browser notifications" section:

- State on open: `configured` from `/settings`; enrolled = `await
  registration.pushManager.getSubscription()` is non-null.
- **Enable** → `Notification.requestPermission()`; on `granted`, fetch the
  public key, `pushManager.subscribe({userVisibleOnly: true,
  applicationServerKey})`, `POST /push/subscribe`. On `denied`, show the
  OS-level hint ("blocked in browser settings").
- **Disable** → `subscription.unsubscribe()` then `DELETE /push/subscribe`.
- **Send test** → `POST /push/test`; shown only when enrolled.
- Not configured ⇒ the section says so and the buttons are hidden.

**`sw.js`** — bump `VERSION` to `v8` (per CLAUDE.md: any `/static/` change
must bump it, and `app.js` changes here).

- `push`: parse `event.data.json()`; on parse failure show a generic
  "New todo" so the browser's "site updated in the background" fallback
  never appears. `showNotification(title, {body, icon: '/static/icons/icon-192.png',
  tag: todo_id, data: payload, actions: payload.actions.slice(0,
  Notification.maxActions || 2).map(a => ({action: 'run:' + a.index,
  title: a.label}))})`. `tag` makes a re-push for the same todo replace the
  banner instead of stacking. `event.waitUntil` around it.
- `notificationclick`: `notification.close()`. If `event.action` starts with
  `run:` → `fetch('/todos/<id>/ask-ai', {method: 'POST', headers:
  {'Content-Type': 'application/json'}, body: {action_index}})`. Then, for
  both a button and a body click, focus an existing app window if one is
  open (navigate it to `/#todo/<id>`) or `clients.openWindow`. A non-2xx from
  `/ask-ai` (401 session expired, 400 stale cache, 5xx) is not retried — the
  window opens at the todo either way and the user can click the chip.
  `event.waitUntil` around the whole chain so the worker isn't killed
  mid-fetch.

### Removal

`notify.py`, `scripts/verify/verify_notify.py`, the `NOTIFY_NEW_TODOS` entry
in `.env.example`, and the "Desktop notifications" paragraph in `CLAUDE.md`
go. The `save_fathom_todo` return-value fix stays (generalized to
`str | None`).

### Error handling summary

| Where | Failure | Behaviour |
|---|---|---|
| poller | no subscriptions | return before any LLM call |
| poller | action generation raises | log; push without buttons |
| poller | push service 404/410 | delete that subscription |
| poller | any other push error | log, swallow; poll cycle unaffected |
| web | VAPID not configured | subscribe 503; Settings explains; poller logs once |
| web | `action_index` with no cache / out of range | 400 |
| sw | payload not JSON | generic "New todo" banner |
| sw | `/ask-ai` non-2xx | open todo; no retry |
| browser | permission denied | Settings shows hint; nothing stored |

### Testing

`scripts/verify/verify_push.py` — plain-`python` assertion script in the
house style, stubbing `pywebpush.webpush` and `generate_action_options`
(no network, no spend):

- no subscription ⇒ `notify_new_todo` returns 0, generator not called,
  `webpush` not called
- one subscription, no cache ⇒ generator called once, `todos.action_options`
  populated, `webpush` called once with a payload whose `actions` carry
  `index` + `label` and no `instruction`
- cache already present ⇒ generator not called
- generator raises ⇒ push still sent with `actions: []`
- `webpush` raises with status 410 ⇒ subscription row deleted; 500 ⇒ kept
- `save_push_subscription` twice with the same endpoint ⇒ one row
- Flask test client: `POST /push/subscribe` stores a row; `DELETE` removes
  it; `POST /ask-ai {action_index: 1}` calls `runs.start` (stubbed) with the
  cached instruction and `from_suggestion=True`; index 5 ⇒ 400; unconfigured
  VAPID ⇒ subscribe 503
- `save_*_todo` return the id on insert and `None` on dedup

Manual, once: enable in Settings on the dev Mac, Send test, confirm the
banner; then trigger a real todo, click an action button, and see the run
appear in the UI at that todo.

## Files touched

- `db.py` — table, four helpers, `save_*_todo` return type
- `push_notify.py` — new
- `agent/action_options.py` — `ensure_action_options`
- `main.py`, `pollers/{fathom,browser,system}/poller.py` — call sites
- `app.py` — push routes, settings field, `action_index` in `/ask-ai`
- `auth.py` — GET allowlist entry
- `static/js/app.js`, `static/js/sw.js` (v8), `templates/index.html`
- `scripts/gen_vapid_keys.py`, `scripts/verify/verify_push.py` — new
- `requirements.txt`, `.env.example`, `CLAUDE.md`
- removed: `notify.py`, `scripts/verify/verify_notify.py`
