# WhatsApp agent — design

Date: 2026-09-22. Branch: `ritwik/feat/whatsapp-agent`.

## Goal

Let any user link their WhatsApp number and talk to their selected executor agent from
WhatsApp, using the same mechanism as the todo-less web chat (PR #30). Provider: Twilio's
WhatsApp API, chosen for the sandbox that works without business verification.

## Shape

WhatsApp is a **second surface on the user's one chat conversation**, not a second
conversation. A WhatsApp message starts the same background run the web Chat starts
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
2. Settings shows the steps: join the sandbox (`join <keyword>` to the Twilio number) if
   the server is on the sandbox, then send the code from WhatsApp.
3. The webhook receives the code from that number, matches it against pending entries
   for that number, stores `whatsapp:number` on the user (removing it from any other user
   who held the same number), clears the pending entry, and replies "Linked".
4. Unlink clears both keys.

The code proves control of the number. Without it, anyone could type another person's
number into Settings and receive that person's messages as agent prompts.

Reverse lookup (number → user) is a query on `user_state` where `key='whatsapp:number'`.

## Webhook

`POST /whatsapp/webhook`, no login, Twilio-signed. Validation is the documented Twilio
scheme (HMAC-SHA1 over the full URL plus sorted POST params, base64, compared to
`X-Twilio-Signature`) against `TWILIO_AUTH_TOKEN`. Two URL candidates are tried, the one
Flask reconstructed and `BASE_URL + path`, because proxies change the scheme and host.
Invalid signature or unconfigured server → 403.

Given `From` (`whatsapp:+…`) and `Body`:

- Unknown number, body is a pending code for that number → link, reply "Linked".
- Unknown number otherwise → reply with a pointer to Settings.
- Known user, run already in flight → reply "still working on your last message".
- Known user, otherwise → if the last assistant bubble asked a question and the body is
  a number (or comma-separated numbers), map it to the option labels and phrase the
  answer as the web does (`"<question> <label>"`). Start the chat turn with an
  `on_finish` hook that sends the reply. Reply with a short acknowledgement, since
  agent turns take minutes.

Replies inside the webhook are TwiML `<Message>`; the finished agent reply goes out
via Twilio's REST API (`Messages.json`, basic auth, form-encoded) in chunks under 1500
characters, split on paragraph boundaries. Every send failure is logged and swallowed:
WhatsApp must never break a run.

Formatting: the agent writes markdown; `whatsapp.format_reply` converts the common
cases (`**bold**` → `*bold*`, headings and list markers to plain lines) and appends
numbered options when the bubble carries questions.

## Config

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (e.g.
`whatsapp:+14155238886`), optional `TWILIO_SANDBOX_KEYWORD`. Unset → Settings says so,
webhook returns 403. Twilio must be pointed at `<BASE_URL>/whatsapp/webhook`; locally that
means a tunnel. The executor is the user's choice; Hermes only runs where the binary is.

## Files

- `whatsapp.py` — config, signature check, send, formatting, option-reply mapping.
- `db.py` — `whatsapp:number` / `whatsapp:pending` helpers and the reverse lookup.
- `app.py` — `_start_chat_turn` (shared by `/chat/ask-ai` and the webhook, with an
  optional `on_finish`), the webhook, two Settings routes, the `whatsapp` block in
  `/settings.json`.
- `templates/index.html`, `static/js/app.js`, `static/css/app.css` — the Settings card.
  `sw.js` cache bump.
- `scripts/verify/verify_whatsapp.py` — signature, linking, inbound → turn → outbound
  (Twilio and Hermes stubbed), option mapping, chunking, formatting.
- `.env.example`, `CLAUDE.md`.

## Out of scope

Media messages (ignored with a text reply), group chats, per-surface threads, rate limits,
Twilio's 24-hour session window and templates (the sandbox and a user-initiated
conversation stay inside it; replies arriving after 24h of silence would need a template).
