# WhatsApp agent — design

Date: 2026-09-22, revised 2026-09-23 (Twilio → Meta Cloud API). Branch: `ritwik/feat/whatsapp-agent`.

## Goal

Let any user link their WhatsApp number and talk to their selected executor agent from
WhatsApp, using the same mechanism as the todo-less web chat (PR #30). Provider: Meta's
WhatsApp Cloud API, direct. Twilio was the first draft; Meta was chosen once it was clear
that (a) the eventual home is a verified Meta business account, so Twilio would have been
rework, and (b) the agent never initiates, so every conversation is a user-initiated
service conversation, which is free and outside the business-initiated limits that
verification raises. Only Meta's free *test* number is capped (five allowlisted recipients);
a real number on an unverified account already serves anyone who messages first.

## Shape

WhatsApp is a **second surface on the user's one chat conversation**, not a second
conversation. A WhatsApp message starts the same background run `/chat/ask-ai` starts
(`runs` registry keyed on `(user_id, "chat")`, `_resolution_work` with `chat_todo()`,
persisted to `user_state` under `chat:thread` / `chat:executor_state`). The web `/chat`
view shows the run live because it polls the same run. When the run finishes, the last
assistant bubble is sent back to WhatsApp. Turns typed in the web view do not go to
WhatsApp; only replies to WhatsApp-originated messages do. "New chat" in the web view
resets the conversation for both surfaces.

## Linking

Settings gets a **WhatsApp** card.

1. User enters their number (E.164). Server stores `whatsapp:pending` in `user_state`:
   `{number, code, expires_at}` with a random 6-digit code, valid 15 minutes.
2. Settings shows the steps: on a test number, get added to its recipient list first;
   save the app's number; send the code from WhatsApp. The card polls while pending.
3. The webhook receives the code from that number, matches it against unexpired pending
   entries for that number, stores `whatsapp:number` on the user (removing it from any other
   user who held the same number), clears the pending entry, and sends "Linked". This check
   runs before the existing-link lookup, so a number can be re-verified into another
   account: the phone that sent the code is the proof.
4. Unlink clears both keys.

The code proves control of the number. Without it, anyone could type another person's
number into Settings and receive that person's messages as agent prompts.

Reverse lookup (number → user) is a query on `user_state` where `key='whatsapp:number'`.

## Webhook

`GET /whatsapp/webhook` — Meta's one-time subscription handshake: when `hub.mode` is
`subscribe` and `hub.verify_token` equals `META_WA_VERIFY_TOKEN`, echo `hub.challenge`.

`POST /whatsapp/webhook`, no login, signed: `X-Hub-Signature-256` is HMAC-SHA256 of the
raw request body with the app secret, so the body is read raw before parsing. Invalid
signature or unconfigured server → 403 / 404. Once the signature checks out the route
always answers 200: anything else makes Meta redeliver, and a message that broke once will
break again. A bounded in-memory set of recent message ids drops Meta's redeliveries and
duplicate deliveries so a repeat never starts a second agent turn.

One delivery carries `entry[].changes[].value.messages[]` (and `statuses[]`, skipped). For
each message, given the sender and text (`text.body`; a tapped button or list row's title;
None for media):

- Body is a pending code for that number → link, send "Linked".
- Unknown number otherwise → send a pointer to Settings.
- Known user, non-text → send a text-only notice.
- Known user, run already in flight → send "still working on your last message".
- Known user, otherwise → if the last assistant bubble asked a question and the body is
  a number (or comma-separated numbers), map it to the option labels and phrase the
  answer as the web does (`"<question> <label>"`). Start the chat turn with an
  `on_finish` hook that sends the reply. Send a short acknowledgement, since agent turns
  take minutes and the webhook response itself carries no message.

Sends are JSON POSTs to `graph.facebook.com/<version>/<phone-number-id>/messages` with a
bearer token, chunked under 4000 characters on paragraph boundaries. Every send failure is
logged and swallowed: WhatsApp must never break a run.

Formatting: the agent writes markdown; `whatsapp.format_reply` converts the common cases
(`**bold**` → `*bold*`, headings and list markers to plain lines) and appends numbered
options when the bubble carries questions.

## Config

`META_WA_PHONE_NUMBER_ID`, `META_WA_ACCESS_TOKEN`, `META_WA_APP_SECRET`,
`META_WA_VERIFY_TOKEN`; optional `META_WA_PHONE_NUMBER` (display), `META_WA_TEST_NUMBER`
(the card mentions the allowlist), `META_GRAPH_VERSION` (default v23.0). Unset → Settings
says so, webhook returns 404. The webhook must be reachable at `<BASE_URL>/whatsapp/webhook`
over HTTPS; locally that means a tunnel. The executor is the user's choice; Hermes only runs
where the binary is.

## Files

- `whatsapp.py` — config, handshake, signature, payload parsing, redelivery guard, send,
  formatting, option-reply mapping.
- `db.py` — `whatsapp:number` / `whatsapp:pending` helpers and the reverse lookup.
- `app.py` — `_start_chat_turn` (shared by `/chat/ask-ai` and the webhook, with an
  optional `on_finish`), the two webhook routes, `_handle_whatsapp_message`, two Settings
  routes, the `whatsapp` block in `/settings.json`.
- `templates/index.html`, `static/js/app.js`, `static/css/app.css` — the Settings card.
  `sw.js` cache bump.
- `scripts/verify/verify_whatsapp.py` — handshake, signature, linking, inbound → turn →
  outbound (Meta and Hermes stubbed), redelivery, option mapping, chunking, formatting.
- `.env.example`, `CLAUDE.md`.

## Out of scope

Media messages (answered with a text notice), group chats, per-surface threads, rate
limits, Meta's interactive reply buttons and lists as an alternative to numbered text
(a later upgrade to `format_reply`), and the 24-hour window and templates: the agent only
ever replies to a user-initiated message, minutes later, so it stays inside the window; a
reply to a message more than a day old would need an approved template.
