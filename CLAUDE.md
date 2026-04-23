# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
# Activate virtualenv
source venv/bin/activate

# Start the poller (Gmail + Fathom, runs forever)
python main.py

# Start the web UI (Flask, separate terminal)
python app.py
# or: flask --app app run --debug
```

Required env vars (put in `.env`):
- `OPENAI_API_KEY` — used by `todo_generator.py` and `app.py`
- `FATHOM_API_KEY` — used by `fathom_poller.py`

Gmail OAuth credentials go in `credentials.json` (downloaded from Google Cloud Console). `token.json` is auto-created on first run via browser flow.

## Architecture

This is a personal productivity assistant with two runtimes sharing one SQLite database (`gmail_events.db`):

**Poller (`main.py`)** — a `while True` loop that runs every 30 s:
1. `gmail_poller.poll()` — calls Gmail History API to get incremental changes since the last `historyId` (stored in `state` table). For each new inbound message: fetches the full message, builds a `GmailEvent`, runs spam filtering, fetches the full thread, and calls the OpenAI todo generator.
2. `fathom_poller.poll()` — hits the Fathom REST API for meetings created after the last poll timestamp; saves any action items as todos.

**Web UI (`app.py`)** — a Flask app serving:
- `GET /` — renders all todos sorted by urgency/status
- `POST /todos` — creates a user-entered todo (source=`user`)
- `PATCH /todos/<id>` — updates `due_date`, `urgency`, or `status`
- `POST /todos/<id>/ask-ai` — AI assistant for a specific todo; uses OpenAI Responses API with web search; persists conversation in `ai_thread` column as JSON

**Data model** (in `db.py`):
- `state` — key/value for `history_id` (Gmail) and `fathom_last_polled_at`
- `events` — raw `GmailEvent` payloads as JSON (append-only)
- `todos` — unified todo list with `source` field: `gmail` | `fathom` | `user`; uses `INSERT OR IGNORE` so duplicate events are safely skipped

**Key types** (`events.py`): `GmailEvent` is the central data structure passed between the poller, spam filter, thread context builder, and todo generator.

**Todo generation flow** (`todo_generator.py`): takes the last 3 thread messages (truncated to 500 chars each) + sender info → `gpt-4o` with JSON mode → structured todo fields. The `ask-ai` endpoint uses `gpt-5.2` with the Responses API (supports `web_search` tool).

**Spam filter** (`spam_filter.py`): drops events with no sender or with Gmail labels `SPAM`, `CATEGORY_PROMOTIONS`, `CATEGORY_UPDATES`, or `CATEGORY_FORUMS`.

**Gmail history ID management**: On first run, the poller bootstraps by storing the current `historyId` and returning nothing. Subsequent polls use `startHistoryId` to get only new changes. The max seen `historyId` is written back after each poll cycle.
