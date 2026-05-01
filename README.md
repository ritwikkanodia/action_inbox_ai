# active_ai

A personal productivity assistant that watches your Gmail, Fathom meetings, browser history, and macOS system state, then turns action items into a prioritised todo list — with an AI assistant built in for each task.

## Sources

- **Gmail** — incremental polling via the History API; spam, promotions, updates, and forum threads are filtered automatically. Each inbound thread is passed to GPT-4o to extract a structured todo.
- **Fathom** — polls recorded meetings and pulls action items directly from Fathom's REST API.
- **Browser (Dia)** — reads Chrome-compatible history from the Dia browser and identifies pages that signal an incomplete transaction (e.g. a half-finished checkout, an open support ticket).
- **System** — snapshots your Downloads, Desktop, and Documents folders on macOS. GPT flags files that need attention (e.g. an uninstalled `.dmg`, an unsigned contract sitting in Downloads).

## Features

- **AI todo generation** — every source feeds a structured todo (title, urgency, due date, suggested action) via GPT-4o
- **Per-todo AI assistant** — persistent chat thread per task, powered by GPT with web search
- **Web UI** — view, prioritise, and manage todos; update status/urgency inline
- **Dedup-safe** — all sources use `INSERT OR IGNORE` so duplicate events are skipped safely

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys
```

**Required env vars** (`.env`):
```
OPENAI_API_KEY=...
FATHOM_API_KEY=...
```

**Gmail OAuth** — download `credentials.json` from Google Cloud Console (Gmail API enabled). The browser flow runs automatically on first start.

## Running

```bash
# Terminal 1 — poller (all sources, runs every 30s)
python main.py

# Terminal 2 — web UI
flask --app app run --debug --port 5001
```

Open `http://localhost:5001/`


## Project layout

```
main.py              # poller entrypoint
app.py               # Flask app
db.py                # SQLite schema + helpers
pollers/
  gmail/             # Gmail History API, spam filter, todo generator
  fathom/            # Fathom REST API poller
  browser/           # Dia browser history reader + todo generator
  system/            # macOS folder snapshot + todo generator
agent/
  resolver.py        # per-todo AI assistant (Responses API + web search)
templates/
  index.html         # UI
```
