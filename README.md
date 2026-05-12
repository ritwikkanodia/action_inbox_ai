# action_inbox_ai

A personal productivity assistant that turns signals from your Gmail and Fathom meetings (and optionally your browser history and macOS folders) into a prioritised todo list — with a per-task AI assistant built in.

## Sources

**Cross-platform, enabled by default:**

- **Gmail** — incremental polling via the History API; spam, promotions, updates, and forum threads are filtered automatically. Each inbound thread is passed to GPT to extract a structured todo.
- **Fathom** — polls recorded meetings and pulls action items from Fathom's REST API. Each user connects their own Fathom API key from the web UI Settings page.

**Optional, currently macOS-only:**

Enable by setting `ENABLED_SOURCES=gmail,fathom,browser_history,system` in `.env`.

- **Browser history** — reads the Chromium-format `History` SQLite DB and identifies pages that signal an incomplete transaction (a half-finished checkout, an open support ticket, etc.). Supports Chrome (default) and Dia; choose via `BROWSER=chrome|dia` in `.env`, or point at any other Chromium history file with `BROWSER_HISTORY_PATH=/absolute/path/to/History`. Chrome locks its history file while running, so polls may occasionally fail until you close the browser.
- **System** — snapshots `~/Downloads`, `~/Desktop`, and `~/Documents`. GPT flags files that need attention (an uninstalled `.dmg`, an unsigned contract). macOS paths are hardcoded.

## Features

- **AI todo generation** — every source feeds a structured todo (title, urgency, due date, suggested action) via GPT
- **Per-todo AI assistant** — persistent chat thread per task, with web search and email-search tools
- **Web UI** — view, prioritise, and manage todos; update status/urgency inline
- **Multi-user** — sign in with Google; each user has isolated todos and source connections

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
```

**Required env vars** (see `.env.example` for the full list):

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
OPENAI_API_KEY=...
FLASK_SECRET_KEY=...
```

**Google OAuth setup** (used for both Sign-in-with-Google and Gmail polling):

1. Create a project at https://console.cloud.google.com/
2. Enable the **Gmail API** under *APIs & Services → Library*
3. *APIs & Services → Credentials → Create credentials → OAuth client ID*
   - Application type: **Web application**
   - Authorized redirect URIs: `http://localhost:5001/oauth/gmail/callback` and `http://localhost:5001/oauth/login/callback`
4. Copy the **Client ID** and **Client secret** into `.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
5. Add yourself as a test user under *OAuth consent screen → Test users*

## Running

```bash
# Terminal 1 — poller (runs every 30s)
python main.py

# Terminal 2 — web UI
flask --app app run --debug --port 5001
```

Open <http://localhost:5001/>, sign in with Google, and connect Gmail/Fathom from the Settings page.

## Project layout

```
main.py              # poller entrypoint
app.py               # Flask app
auth.py              # Sign-in-with-Google OAuth
db.py                # SQLite schema + helpers
pollers/
  gmail/             # Gmail History API, spam filter, todo generator
  fathom/            # Fathom REST API poller
  browser/           # Chromium history reader + todo generator (opt-in)
  system/            # macOS folder snapshot + todo generator (opt-in)
agent/
  resolver.py        # per-todo AI assistant (OpenAI Responses API)
templates/           # UI
```

## License

MIT — see [LICENSE](./LICENSE).
