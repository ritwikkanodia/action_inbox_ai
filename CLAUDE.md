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

`venv/` is gitignored and exists only in the main checkout, so a git worktree under
`.claude/worktrees/` has no `venv/bin/activate` of its own. From a worktree, use the main
checkout's interpreter directly (`/path/to/action_inbox_ai/venv/bin/python …`) or point
`source` at that path; do not create a second venv per worktree.

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
python scripts/verify/verify_executor.py    # stubs the Hermes CLI; no API spend
python scripts/verify/verify_actions.py     # stubs the OpenAI call; no API spend
python scripts/verify/verify_hermes_activity.py  # stubs Hermes' state.db; no CLI, no spend
python scripts/verify/verify_clarify.py     # clarifying-question parsing; no CLI, no spend
python scripts/verify/verify_push.py       # stubs pywebpush + the OpenAI call; no spend
python scripts/verify/verify_google_scopes.py  # scope set + credential refresh; no network
python scripts/verify/verify_google_mcp.py     # Google MCP server against fake clients; no network
python scripts/verify/verify_chat.py           # todo-less chat routes; stubs Hermes, no spend
python scripts/verify/verify_whatsapp.py       # Meta webhook, linking, replies; stubs Meta + Hermes, no spend
python scripts/verify/verify_todo_tools.py     # agents' todo tools + the db helpers the UI shares; no spend
```

## Architecture

Two runtimes share one SQLite database (`DB_PATH`, default `gmail_events.db`):

**Poller (`main.py`)** — a `while True` loop, 30s per cycle. For each user with at least one
connected source (`list_active_users`), it runs each enabled source in its own `try/except` so
one failing source never kills the cycle. Sources are gated by `ENABLED_SOURCES`; the default
set is `gmail,fathom,morning_digest`. `browser_history` and `system` are macOS-only and opt-in.
The morning digest is the exception to the per-source loop: it iterates `list_all_users`, not
just connected ones, so unconnected users get a "connect Gmail" nudge instead of nothing.
The source registry (`KNOWN_SOURCES`, the default set, `enabled_sources()`, and the labels
Settings shows) lives in `sources.py`, shared by the poller and the web UI. On top of the
server-wide set, **each user can pause any discovery source from Settings**
(`db.is_source_enabled`, a `source:<name>:enabled` key in `user_state`; unset means on).
The poller checks that flag per user, per source, every cycle, so a pause lands within one
poll interval and never needs a restart. Pausing leaves connections and cursors alone.
The morning digest is a sender, not a discovery source, and has no per-user toggle.

**New-todo notifications** (`push_notify.py`): each poller calls `notify_new_todo(conn,
user_id, todo_id)` right where it branches on the `save_*_todo` return value (which is the
`todo_id`, or `None` on a dedup), so only a real insert notifies. It returns before any LLM
call when the user has neither a push subscription nor a linked WhatsApp number — eager
action inference is paid for only when someone will see the options — otherwise
`agent.action_options.ensure_action_options` fills the same cache the detail pane reads,
once, and the notice goes down every channel the user has. **WhatsApp**: the linked number
gets the title, the suggested action and the three routes as a numbered list, and the same
notice is appended to the user's chat thread (`db.append_chat_bubble`) as an assistant
bubble carrying an `ask_user` block. That reuse is the whole design: the webhook already
maps a digit reply against the last bubble's options, and the web Chat view already renders
them as chips, so "2" from the phone and a chip click in the browser both send "New todo
from gmail: <title> (<todo_id>). How should I handle it? Decline politely" to the chat
agent, which finds the todo with `todos_get`. Only labels travel — the instruction behind
each route stays in `todos.action_options`. One known gap: the poller appends to
`chat:thread` from its own process, and a chat turn already in flight in the web process
overwrites the thread when it persists, so a notice landing mid-turn is lost from the log
(the WhatsApp message still arrives; a digit reply then reaches the agent as a bare "2").
Verified by `scripts/verify/verify_push.py`. **Push**: the payload (labels and indices,
never instructions) goes out via `pywebpush` to every browser the user enrolled from
Settings. `static/js/sw.js` shows up to
`Notification.maxActions` of them (2 on Chrome/macOS, 0 on Safari) as buttons; a click POSTs
`{action_index}` to `/ask-ai`, which runs the instruction *it* cached with
`from_suggestion=True` — a push can't put words in the agent's mouth. Then it focuses the app
at `#todo/<id>` so the live trace is in front of the user. Subscriptions live in
`push_subscriptions` keyed on the push endpoint; a 404/410 from the push service prunes the
row. Needs `VAPID_PRIVATE_KEY`/`VAPID_PUBLIC_KEY`/`VAPID_SUBJECT` (`scripts/gen_vapid_keys.py`);
unset ⇒ Settings says so and the poller logs once and skips. Every failure is swallowed — a
notification must never cost a poll cycle. Three things learned getting this to work on
macOS, none of them ours to fix: Chrome shows the action buttons under the alert's
**Options** menu, and only stays on screen long enough to use them when Chrome's alert style
is *Persistent* (System Settings → Notifications → Google Chrome) — `requireInteraction` is
set, but *Temporary* ignores it; a re-push under the same `tag` only alerts because
`renotify` is set, otherwise macOS updates the Notification Centre entry silently; and
Chrome's background push channel can go stale — FCM keeps answering 201 and queueing while
nothing reaches the service worker, even on a fresh subscription — and only a full Chrome
restart (⌘Q) reconnects it, at which point the queue drains. The service worker posts a
`push-debug` message to open pages at each stage (`received`/`shown`/`error`) so that
last case can be told apart from a broken handler without chrome:// internals.

**Web UI (`app.py`)** — Flask, multi-user, every route behind `@login_required` except
`/login`, the OAuth callbacks, `/digest/preview`, `/stats`, and the PWA routes
(`/manifest.webmanifest`, `/sw.js`, `/offline`) — Chrome fetches those before a session
exists, and a redirect to `/login` would make the app non-installable. **The service worker
serves `/static/` cache-first**, so any edit to `static/js/app.js` or `static/css/app.css` must
bump `VERSION` in `static/js/sw.js`; otherwise `activate` keeps the old cache and the browser
goes on running the frontend you just replaced, while the server-side half of the change works
fine — a confusing way to lose an afternoon. Key routes:

- `GET /` — todos for the current user, ordered closed-last, then importance, then recency
- `POST /todos` — user-entered todo (`source='user'`)
- `PATCH /todos/<id>` — any of `db.TODO_EDITABLE_FIELDS` (`title`, `due_date`, `importance`,
  `status`, `decision`, `suggested_action`); 400 on a bad enum, 404 on another user's id
- `GET /todos/<id>/actions` — the three inferred ways to close the todo; `?refresh=1` re-infers
- `POST /todos/<id>/ask-ai` — starts one agent turn in the background and returns immediately;
  posting with no message returns the existing thread without an LLM call
- `GET /todos/<id>/run`, `POST /todos/<id>/run/stop` — poll and stop the running turn
- `GET /todos/<id>/context` — source context (e.g. the Gmail thread) for the detail pane
- `POST /todos/<id>/reset-thread` — clears `ai_thread`
- `GET /settings` — the Settings *page*: the same shell as `/` with the settings view in front
  (`initial_view`), so Back is instant and the digest's "connect Gmail" link lands somewhere
  real. The frontend swaps views with `pushState`; the header link and Back are real anchors.
  The Gmail OAuth callback redirects here, so a freshly connected account is on screen.
- `GET /settings.json` — what the settings view fetches; `POST /settings/sources/<source>`,
  `/settings/sources/gmail/auth` — source connections
- `POST /settings/sources/<source>/enabled` — per-user pause/resume of a discovery source
- `POST /settings/executor` — which agent resolves this user's todos (see "Discovery and execution are decoupled")
- `GET /digest/preview?user_id=…[&format=json]` — renders a user's digest without sending it
- `GET /chat` — the todo-less chat, a third view in the same shell; `POST /chat/ask-ai`,
  `GET /chat/run`, `POST /chat/run/stop`, `POST /chat/reset-thread` mirror the todo routes
- `GET|POST /whatsapp/webhook` — Meta's subscription handshake and inbound messages (public,
  signature-checked); `POST /settings/whatsapp/link`, `/settings/whatsapp/unlink` — number linking

**The chat is the todo routes with the todo taken out.** One conversation per user, held in
`user_state` under `chat:thread` (the display log) and `chat:executor_state`; "New chat"
clears both and there is no history. The run registry keys it on the fixed id `chat`, and the
same `_resolution_work` runs it with a different `persist` callable, so stop, failure records
and clarifying-question chips behave exactly as they do on a todo. Executors are handed
`executor.chat_todo()` — a pseudo-todo with `source='chat'` — and both prompt builders swap the
"Todo" section for a short direct-chat framing (`input_builder.CHAT_CONTEXT`); Hermes names
the session `aib-chat-<nonce>`, and the Google tools fall back to the user's first connected
account. The frontend reuses the detail pane's thread and composer code, resolving URLs from
the active thread id (`aiUrl`), so only one composer exists in the DOM at a time: opening the
chat clears the todo selection. Verified by `scripts/verify/verify_chat.py`.

**WhatsApp is a second surface on that same chat** (`whatsapp.py`, routes in `app.py`), over
Meta's WhatsApp Cloud API, direct — no BSP in between. A user links their number from
Settings: the server issues a 6-digit code (`whatsapp:pending` in `user_state`, 15 minutes),
the user sends it from their WhatsApp to the app's number, and the webhook matches it and
stores `whatsapp:number` — one per user, one user per number. A pending code is checked
*before* the existing link, so a number can be re-verified into another account; the phone
that sent the code is the proof, and that proof is what stops someone routing another
person's WhatsApp into their own account. From then on a message from that number is a chat
turn for that user, started through the same `_start_chat_turn` the web view uses, so `/chat`
shows it live; an `on_finish` hook sends the last assistant bubble back with
`whatsapp.send_message`, chunked under Meta's 4096-character cap. Clarifying questions render
as a numbered list, and a digit reply is mapped back to the option and phrased exactly as a
chip click would be. Because a turn takes minutes and Meta's webhook response carries no
message, the "On it" acknowledgement, "still working" (a message arriving mid-run), "Linked"
and the not-linked pointer all go out as ordinary API sends. Meta specifics, all in
`whatsapp.py`: `GET /whatsapp/webhook` is the one-time subscription handshake
(`hub.verify_token` → echo `hub.challenge`); `POST` is signed with `X-Hub-Signature-256`,
HMAC-SHA256 of the raw body with the app secret, so the body must be read raw before any
parsing; one delivery can carry several messages plus status receipts (skipped); and Meta
redelivers until it sees a 200 and can deliver twice anyway, so the route always returns 200
once the signature checks out and a bounded set of recent message ids drops repeats.
Non-text messages get a text-only notice; a tapped reply button or list row arrives as its
title and is treated as text. Needs `META_WA_PHONE_NUMBER_ID`/`META_WA_ACCESS_TOKEN`/
`META_WA_APP_SECRET`/`META_WA_VERIFY_TOKEN` (`META_WA_PHONE_NUMBER` for the card,
`META_WA_TEST_NUMBER=1` on the free test number, whose recipient allowlist Settings then
mentions) and the webhook pointed at `<BASE_URL>/whatsapp/webhook` — a tunnel locally;
`scripts/setup_whatsapp_meta.sh` is a guided walk through the Meta dashboard that fills all of
that in. The
agent's *replies* are always user-initiated and inside the 24-hour service window: free, and
not counted against the business-initiated limits that Meta's business verification raises.
The new-todo notice (`push_notify.send_whatsapp`) is the one business-initiated send. It
goes as free-form text first, since that carries the option details, and Meta only
delivers that inside the window — a linked number that has not messaged the app in the
last 24 hours gets error `131047` (`whatsapp.REENGAGEMENT_ERROR`). On exactly that code
the notice is resent as the approved template named by `META_WA_NOTICE_TEMPLATE`
(`whatsapp.send_template`, six placeholders from `push_notify.template_params`: source,
title, suggested action, three option labels padded with a dash — placeholders may not be
empty or contain newlines, so the template body carries the structure). Any other error,
or no template configured, drops the notice; the chat bubble lands either way.
`scripts/create_whatsapp_template.py` submits that template (`new_todo_notice`, UTILITY,
en) to the WABA and polls for approval; the WABA id it needs is `entry[].id` on any
webhook delivery, and Meta charges a utility rate for a template sent outside the window,
nothing inside it. `whatsapp._post` raises `GraphError` (a `RuntimeError`, so the
swallowing `send_message` wrapper is unchanged) carrying Meta's status and code; `send_text`
is the raising primitive under both. No SDK: the signature is one HMAC and
the send one JSON POST,
both stubbed by `scripts/verify/verify_whatsapp.py`. Every outbound failure is logged and
swallowed.

**Images sent to the WhatsApp number reach the agent; nothing else non-text does.** Meta
puts no bytes in the webhook, only a media id, so `whatsapp.download_media` makes the two
Graph calls (id → short-lived URL, URL → bytes, both bearer-authenticated) and the handler
saves the file under `UPLOADS_DIR/<user_id>/<message_id>.<ext>` (default
`~/.action_inbox_ai/uploads`) *before* starting the turn, so a failed fetch costs a
"couldn't fetch that image" notice and not a run. The thread bubble is a placeholder,
`📎 Image: <caption>` — the web Chat view renders no images, so nothing on the frontend
changed — and the file travels out of band as `images=[path]`, a new optional argument on
`executor.resolve` that both executors take. Hermes gets the first path on the CLI's
`--image` flag (it takes one) and every path in an "Attached images" prompt section that
points at its `vision_analyze` tool, which is what lets a later turn look at the file again.
The Agents SDK sends the text plus one base64 `input_image` part per file, and
`input_builder.strip_image_parts` reduces that turn back to its text before the thread is
persisted, so the bytes are never re-sent on the next turn or stored in the row. A photo
skips the digit-reply mapping: a caption of "2" is a caption, not an answer. Documents,
stickers, audio, location and contact cards still get the text-only notice. Verified by
`scripts/verify/verify_whatsapp.py` (parser, download, handler) and
`scripts/verify/verify_executor.py` (the command line, the prompt, the SDK content part).

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

The grant requested at sign-in and on reconnect is Workspace-wide (`google_scopes.py`:
Gmail modify, Drive, Docs, Sheets, Calendar events, Contacts) so the Hermes agent's
Google tools can use it. Accounts connected before that carry only `gmail.readonly` and
keep polling — refresh always uses the scopes stored on the token, never the requested
list, because google-auth raises when the requested set exceeds the granted one. Settings
shows per account whether agent access is granted, with a Grant link that re-consents
with `login_hint` and `include_granted_scopes`.

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
  (todos predating multi-account support). `action_options` caches the three
  suggested actions as JSON; `NULL` means they haven't been inferred yet.
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

Model choice is per call site, in **`llm_models.py`**, because the five sites differ in both
volume and blast radius. `POLLER` (`gpt-5.6-luna`) is used by `pollers/gmail/todo_generator.py`,
`pollers/browser/generator.py` and `pollers/system/generator.py` — it runs on every item the
pollers see and only decides "is there a todo here", so it wants the cheapest current model.
`ACTIONS` (`gpt-5.6-terra`) is `agent/action_options.py`: one cached call per todo, whose output
becomes an agent's marching orders, so judgement there is cheap to buy and expensive to skip.
`AGENT` (`gpt-6-astra`) is `agent/resolver.py`, the Agents-SDK loop the deployed container runs;
it is also where spend can run away, since every tool result re-enters the context. Override any
of them with `OPENAI_MODEL_POLLER` / `OPENAI_MODEL_ACTIONS` / `OPENAI_MODEL_AGENT`. They are read
at import time, so `load_dotenv` has to have run first — it does, at the top of both entrypoints.
The generators use Chat Completions with `response_format={"type": "json_object"}`.

**Todo generation** (`pollers/gmail/todo_generator.py`): thread context + sender → JSON with a
`should_generate_todo` boolean and a `reasoning` sentence. Returning `false` is a first-class
outcome, not an error — newsletters, receipts, OTPs, and sign-in requests should be skipped
there, and the poller logs the reasoning.

**Suggested actions** (`agent/action_options.py`): todo + thread context → JSON with three
`{label, detail, instruction}` options for closing the item. This is a plain Chat Completions
call, not an agent turn — it only *proposes* the routes. Clicking one sends its `instruction`
to `/ask-ai` as the user message, so a suggested action and a hand-typed one take exactly the
same path. Options are generated on first open of a todo's detail pane and cached in
`todos.action_options`, so reopening costs nothing.

**Clarifying questions** (`agent/clarify.py`). The resolution prompts forbid inventing a fact
about the user — a rating, an opinion, an experience, a figure — and require the agent to stop
before any irreversible act whose inputs it inferred rather than confirmed. That only works if
asking is cheap, so an agent that needs something appends a fenced ` ```ask_user ` block to its
reply holding `{questions: [{question, header, options: [{label, detail}], multiSelect}]}`.
`app._thread_for_client` splits that off the prose at **read** time and hands it to the frontend
as a `questions` field on the bubble; `static/js/app.js` renders the options as chips and always
adds its own "Something else…" that focuses the composer, so the prompts ask for 2-3 options,
never a free-text one. Parsing on read rather than on write is what makes the chips survive a
reload — the raw block stays in `todos.ai_thread`, so every read re-derives them and no column
was added. Every parse failure degrades to the untouched reply: a malformed block must cost the
user chips, never the answer. Both executors get this, since neither knows the protocol exists.

**Discovery and execution are decoupled.** The pollers generate todos; resolution runs on a
*selectable executor* behind `agent/executor.py`. `app.py` calls `executor.resolve` and knows
nothing about which one is configured. The contract:

```
resolve(todo, thread, user_message, user_id, state,
        cancel=None, progress=None, from_suggestion=False) -> (thread, state)
```

`thread` is the display log (`{role, content}` bubbles — also a valid Agents-SDK input list, so
executors can read each other's threads). `state` is an opaque per-executor string persisted in
`todos.executor_state`; nothing outside the executor interprets it. Implementations are imported
lazily, so picking one never pays for the other's dependencies.

`from_suggestion` says the message came from clicking an inferred option rather than being
typed. It exists because the user consented to a four-word label, not to the model-written
sentence behind it: an instruction that asserts something about them ("reflects a positive
experience") would otherwise reach the agent as their own words and defeat the rule against
inventing facts about the user. Executors that build a prompt frame such a turn as a route,
not a statement.

**How well that holds depends on the model.** Measured against a deliberately poisoned
instruction ("...one sentence that reflects a positive experience"), a `gpt-5-mini`-class agent
ignored the framing in all three placements tried and wrote an invented 5-star review; a
`gpt-6-astra` one stripped the presumption and asked "how would you rate *your own* experience",
offering "no product-use experience to review" as an option. Weaker models follow the concrete
directive and drop the abstract constraint. Treat the framing as real but model-dependent, and
keep `agent/action_options.py` not generating presumptuous instructions in the first place —
that is the fix that does not depend on which model is behind the executor.

`cancel` is an `agent.runs.CancelToken` — honouring it is best-effort and per-executor. Hermes
attaches its subprocess to the token, so a stop kills it (and its process group — the CLI drives
a browser, and killing only the parent would leave it holding the pipes the run is blocked on).
`agents_sdk` has nothing to interrupt and ignores it.

`progress` is an optional callback taking one `{tool, detail}` event, so the UI can show what
the agent is doing before it has finished doing it. Best-effort in the same way: Hermes tails
its own session store (see below) to produce events, `agents_sdk` reports nothing because
`Runner.run_sync` surfaces nothing until it returns. **No events is normal, not a stalled run** —
the UI must still read as working with an empty trace.

**Turns run in the background** (`agent/runs.py`). Resolution used to happen inline in the
ask-ai request, which left no handle on a run in flight — the only thing connected to it was
the request blocking on it, so "stop" had nothing to talk to. `POST /ask-ai` now registers a run
on a background thread and returns at once; the client polls `GET /run` and can `POST /run/stop`.
The registry is per-process and deliberately not persisted: a restart (including the debug
reloader) kills the run with it and leaves the todo where it was. At most one run exists per
(user, todo). **What a stopped or failed run leaves behind depends on whether the agent had
acted.** A turn interrupted before its first tool call is discarded — nothing happened, and
dropping it keeps the log honest. A turn interrupted *after* one is recorded: the user's
message plus a bubble listing every tool call the turn made, persisted to `ai_thread`, and the
Hermes session name kept (the executor tags it onto the `ExecutorError`), so the next turn's
agent remembers the same calls. The reason is a Booth deposit run that composed and clicked
Send on an email to admissions two seconds before the user hit Stop: under the old
"stopped turns leave nothing behind" rule the app held no record of it anywhere. The agent
may already have sent mail or submitted a form before a stop lands — that is precisely why
the record exists, not a reason to omit it.

Returning immediately also gets the turn out from under gunicorn's request timeout, which a
600s Hermes run would otherwise blow through. The flip side is that the registry lives in one
process: with more than one gunicorn worker a poll can land on a worker that never saw the run
and answer `idle`. The UI treats that as "stop watching", and the run still persists its result
when it finishes — but keep the web runtime at a single worker for the stop button to be
reliable.

**The executor is chosen per user, in Settings.** The choice lives in `user_state` under
the `executor` key (`db.get_executor_choice`); `TODO_EXECUTOR` is only the server-wide
default for users who have not chosen, and defaults to `hermes`. `GET /settings` returns an
`executor` block — the selection, the default, and every option with a `ready` verdict from
`executor.readiness` (Hermes: the binary resolves; Agents SDK: the package imports and
`OPENAI_API_KEY` is set) — and `POST /settings/executor` refuses an option that is not ready,
so a saved choice always starts a turn on the next message. `ask_ai` reads the choice when
a turn starts and hands it to `_resolution_work`, which passes `executor=` to
`executor.resolve`; a turn already running finishes on the executor it started with. The
readiness probes are cheap on purpose (a `which`, a `find_spec`, an env lookup) because
Settings runs them on every open — "ready" means the turn will start, not that it will
succeed. Switching mid-thread is allowed: both executors read the same display log, but
neither understands the other's `executor_state`, so the Agents SDK returns `None` for it
(which drops the Hermes session name) and Hermes, finding none, opens a fresh session with
the full task framing. The SDK side has one accommodation for this: a thread it did not
start carries no task context in its bubbles (Hermes keeps that in its prompt), so
`resolver.resolve_todo` prepends its hidden bootstrap turn when the first item is not one.

The two executors:

- **`hermes`** — shells out to the locally installed Hermes Agent CLI, which brings its own
  browser, terminal, file, and desktop tools. Needs the binary on the host, so it does **not**
  work in the deployed container.
- **`agents_sdk`** (`agent/sdk_executor.py` → `agent/resolver.py`) — the original in-process
  OpenAI Agents SDK agent. The only executor that runs without a local CLI, so **the deployed
  container must set this as `TODO_EXECUTOR`** (and Hermes will show as not ready there, so
  nobody can pick it). Keeps no out-of-band state; leaves `executor_state` NULL.

Adding an executor means one module implementing `resolve`, an entry in `executor.EXECUTORS`
(name, label, description — what Settings shows), a branch in `executor._load`, and a probe
in `executor.readiness`.

- `agent/hermes_runner.py` and `agent/hermes_activity.py` are the only files that know about
  Hermes (plus `hermes_prompt.py`, which builds the text). The runner runs
  `hermes chat -q <prompt> -Q --yolo -c <session-name> --create-if-missing`. `-Q` keeps stdout
  to the final reply alone, and `--create-if-missing` lets the first turn open the thread.
  **Not** the top-level `-z` one-shot: `-z` accepts `--resume` but does not restore the
  conversation. Measured against a fresh-session control, a resumed `-z` run recalled nothing
  from the turn before it — what looked like continuity was Hermes' cross-session *memory*
  recalling facts, not the thread. `chat` appends to the named session instead: same session
  id, message count growing. Set `HERMES_BIN` if the binary isn't on `PATH`,
  `HERMES_TIMEOUT_SECONDS` (default 600) to bound a wedged run, and `HERMES_YOLO=0` to fall
  back to Hermes' approval rules (expect blocked runs — there is no TTY to approve at).
- **The browser is headed by default.** `AGENT_BROWSER_HEADED=1` is set on the subprocess, so
  the agent's Chrome is a window you can watch rather than the headless one Hermes defaults to.
  It's set in the environment rather than in `~/.hermes/config.yaml` so it applies to Action
  Inbox runs and nothing else. `HERMES_BROWSER_HEADED=0` restores headless. Two things about
  this are Hermes' behavior, not ours: `browser.use_real_profile` (on in the user's config)
  means the window is a *copy* of the real Chrome profile — same logins; and a browser
  surviving from an earlier run is re-attached to rather than relaunched, so a headless one
  left over from other Hermes use makes the next run invisible until it ages out
  (`browser.inactivity_timeout`, 120s).
- **By default, a login the agent's window acquires does not survive the turn.** Each turn
  is its own `hermes chat` process, and Hermes terminates the real-profile Chrome from an
  `atexit` hook when that process ends. The next turn relaunches it and re-mirrors the *auth*
  databases — cookies, login data — out of the user's real Chrome over the copy, on every
  launch, not only when the full tree is first copied. So the only sign-in the agent ever has
  is the one the user's own Chrome has; signing in inside the agent's window is erased before
  the next turn can use it. Telling the agent otherwise is what first stranded a Trustpilot
  todo mid-resolution. A login wall should still be *tried* first ("Continue with Google")
  rather than reported on sight, since the copied profile usually carries the session.
- **That re-mirror is also why the browser may refuse to launch at all.** Hermes backs up
  each auth database with SQLite, and a *running* Chrome holds `Login Data`, `Login Data For
  Account` and `Web Data` (passwords and autofill) with a write lock; `Cookies` — the one that
  actually carries sessions — copies fine. Hermes fails closed on any locked one
  ("never launch a silently signed-out session"), has no knob to relax that, and deliberately
  will not quit the browser itself. `HERMES_AUTOCLOSE_CHROME=1` (`agent/chrome_profile.py`)
  makes that decision once instead of per run: a graceful AppleScript quit of the user's
  Chrome before the turn, `open -n` relaunch after. Never a force-kill — a Chrome that will
  not quit (an unsaved-changes prompt) is left alone and the run proceeds to Hermes' own
  error. The relaunch needs `-n` because the agent's browser is the *same Chrome binary* on a
  different profile, and a plain `open -a` just raises that instead. The same-bundle fact
  also means the agent must never `open <url>` or `tell application "Google Chrome"` to put
  a page in front of the user: macOS routes both to *its own* window, the title it reads
  back confirms nothing, and the page dies with the turn. The prompt says so.
- **`HERMES_PERSISTENT_BROWSER=1` makes both of the above mostly moot** (`agent/agent_browser.py`).
  Before launching, Hermes looks for a Chrome *already running* on its profile copy
  (`DevToolsActivePort` + `/json/version` handshake); if it finds one it attaches, **skips the
  snapshot** (its own comment marks overlaying a live profile as never-do), and does not
  terminate it on exit — no `Popen` handle, "not ours to terminate, by design". So the runner
  launches that Chrome itself, detached, and keeps it: no per-turn snapshot, so the user's
  Chrome is never touched; and a sign-in done in the agent's window **does** persist to the
  next turn. The login-handoff paragraph in `hermes_prompt.py` is selected by this flag, since
  the right instruction inverts: ephemeral → "sign in in your own browser, here is a link";
  persistent → "sign in in my window, it stays open". The trade is freshness — the copy no
  longer re-mirrors from the real Chrome each turn, so a site the user logs into there is not
  seen here; `agent_browser.refresh_snapshot()` is the manual reset, and the autoclose is
  what bootstraps the very first snapshot. Measured on the Trustpilot todo: 4 turns, user's
  Chrome untouched throughout, one-time sign-up in the agent's window, review submitted and
  verified at its permanent URL.
- **Google is reached over the API, not the browser** (`agent/google_mcp/`). A stdio MCP
  server in this repo serves Gmail/Drive/Docs/Sheets/Calendar/Contacts tools backed by the
  app's own per-account tokens in `source_connections`. Register it once with
  `python scripts/install_hermes_google_mcp.py` (writes `mcp_servers.action_inbox_google`
  into `~/.hermes/config.yaml` with `${AIB_*}` env references); the runner then sets
  `AIB_USER_ID`, `AIB_ACCOUNT_ID` and `AIB_DB_PATH` on each `hermes chat` subprocess and
  Hermes expands them into the server's env at launch. No token crosses the environment —
  the server reads and refreshes credentials from the database itself. With no binding
  (any Hermes run that isn't ours, or `HERMES_GOOGLE_TOOLS=0`, which blanks the three
  `AIB_*` variables on the subprocess env rather than omitting them, so a value this
  process happened to have inherited can't leak through) it serves zero tools.
  Every tool returns a string and turns failures into an `Error:` line, so Hermes' loop
  guardrails see ordinary results; a missing scope says so and points at Settings. The
  server's stdout is the protocol channel — log to stderr only. Hermes' single-query mode
  waits up to 15s for MCP servers to come up, which covers the Google client imports.
- **Both executors can read and edit the user's todo list** (`agent/todo_tools.py`):
  `todos_list`, `todos_get`, `todos_create`, `todos_update` — and deliberately no delete, so
  an agent acting on a prompt built from email content can never erase the list; closing is
  `todos_update(status="closed")`, and the row staying is what keeps dedup from regenerating
  a polled todo. The tools are plain closures over `(db_path, user_id, current_todo_id)` and
  call the **same `db.py` helpers the UI routes do** — `list_todos` (the inbox ordering),
  `get_todo`, `save_user_todo`, `update_todo_fields` (which owns `TODO_EDITABLE_FIELDS` and
  the enum checks, so the PATCH route and the agent can't disagree about what is editable).
  Hermes gets them from the existing `action_inbox_google` MCP server, which serves them
  alongside the Google tools from the same binding plus one more variable, `AIB_TODO_ID`
  (blank on a chat turn, where `todos_update` then needs an explicit id); re-run
  `scripts/install_hermes_google_mcp.py` once after pulling this so the config forwards it.
  `HERMES_GOOGLE_TOOLS=0` blanks the whole binding, so it turns these off too. The Agents SDK
  wraps the same closures in `function_tool` inside `resolver._build_agent`. Both prompts say
  when to use them: add a follow-up you uncovered, edit when asked, close the current todo
  only once the outcome is verified — never because a draft exists. Verified by
  `scripts/verify/verify_todo_tools.py`.
- **Don't edit a `.py` file while a turn is live under `flask --debug`.** The reloader
  restarts the web process; the run registry is in-memory, so the reply has nowhere to land,
  and a `finally` (the Chrome restore, say) never runs. Hermes, in its own process group,
  finishes as an orphan. It looks like a mysterious failure; it is a self-inflicted one.
- **Live tool activity comes from Hermes' own database, not stdout.** `-Q` suppresses tool
  previews, but nothing needs to be streamed: Hermes writes each message of a turn to
  `~/.hermes/state.db` *while the turn runs* — verified by polling `messages` against a live
  session with `sessions.ended_at` still NULL. `agent/hermes_activity.py` tails that read-only
  and reports `{tool, detail}` events through the executor contract's optional `progress`
  callback; `agent/runs.py` buffers them on the run, and `GET /todos/<id>/run` returns them
  alongside `thread`. Only tool calls are reported — the same rows carry the model's reasoning,
  which is long and would bury the trace. The live trace itself is **not persisted** — it is
  a progress indicator for one turn in flight — but `app._resolution_work` keeps its own copy
  of the same events, and that copy is what gets rendered into the record bubble when a turn
  is stopped or fails after acting (see the background-runs section). Every failure in the
  watcher is swallowed on purpose — it is reading another program's private schema, and must
  never be able to take down the resolution itself. That also means the record is only as
  complete as the watcher was: a stop that lands before the watcher's next poll can miss the
  last call, so treat the record as "at least this" rather than "exactly this".
- **`--yolo` is deliberate and load-bearing.** A run with no TTY that stops for an approval
  prompt blocks until the timeout. It also means a full-access agent acts on prompts built
  from email content, which is attacker-controlled text; this is an accepted risk of the
  local POC, not an oversight. With the Google tools registered the same agent holds
  send-mail, Drive-write, Docs/Sheets-write and Calendar-create over the API, so an
  instruction smuggled in an email is one `gmail_send` away from acting rather than a
  multi-step browser sequence; `HERMES_GOOGLE_TOOLS=0` blanks the binding for a run where
  that is not acceptable.
- **Sessions are the conversation.** `todos.executor_state` holds the Hermes session *name*
  (`aib-<todo_id>-<nonce>`), not an id, and it is stable for the life of the thread — every
  turn continues the same session. `todos.ai_thread` is just a display log of
  `{role, content}` bubbles, not agent state. Session titles are globally unique, which is why
  the name carries a random suffix: keyed on the todo id alone, the name would resolve back to
  the session `/reset-thread` just cleared and silently resume it. `/reset-thread` clears both
  columns — clearing only the log would leave the agent still remembering.
- Each invocation gets a fresh system prompt, so a continuing turn must restate the task
  framing (`build_followup_prompt`). Sending a bare user message makes the agent forget it is a
  resolution agent and start asking clarifying questions.
- A failed run is **not** retried: by the time it fails the agent may already have sent mail or
  submitted a form. Failures render as a bubble in the thread, because `static/js/app.js` reads
  `data.thread` without checking the status code — a non-200 would vanish silently.

`agent/resolver.py` and its tools (`tools/browser/`, `tools/local_files.py`, `tools/email.py`)
are the **`agents_sdk`** executor, built on the **OpenAI Agents SDK** (`openai-agents`).
`tools/email.py` is also used by the Hermes path, via `hermes_prompt.py`, to inline the Gmail
thread. For that executor:

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
2. Add the name to `KNOWN_SOURCES` in `sources.py`, and to `DEFAULT_ENABLED_SOURCES` only if
   it's cross-platform and safe on by default. Add a `DISCOVERY_SOURCES` entry (label and
   description) so Settings can show its pause toggle — sources without a connection UI get
   a generated card from that entry alone.
3. Add a `save_<source>_todo` helper in `db.py` with a stable `dedup_key`, and extend the
   `source` CHECK constraint *and* the Python-side enum validation.
4. Wire it into the per-user block in `main()` inside its own `try/except`, gated on
   `wants(<name>)` so the user's pause applies.

## Deployment

`railway.json` runs both runtimes in one container:
`python main.py & exec gunicorn --bind 0.0.0.0:${PORT:-8000} app:app`. Set `BASE_URL` in
production — it's used for OAuth redirect URIs and digest links, and `OAUTHLIB_INSECURE_TRANSPORT`
is only enabled when it points at localhost.
