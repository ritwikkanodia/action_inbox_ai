import json
import sqlite3

from events import GmailEvent


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id   TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            type       TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            payload    TEXT NOT NULL   -- full event as JSON
        );

        CREATE TABLE IF NOT EXISTS todos (
            todo_id                TEXT PRIMARY KEY,  -- todo_<message_id>
            event_id               TEXT NOT NULL,
            message_id             TEXT NOT NULL,
            thread_id              TEXT NOT NULL,
            title                  TEXT,
            suggested_action       TEXT,
            draft                  TEXT,
            urgency                TEXT,              -- low | medium | high
            estimated_time_minutes INTEGER,
            due_date               TEXT,              -- ISO UTC, nullable
            relevant_link          TEXT,              -- action URL or Gmail thread fallback
            reasoning              TEXT NOT NULL,
            raw_llm_response       TEXT,              -- exact JSON string from the model
            status                 TEXT NOT NULL DEFAULT 'open',  -- open | ongoing | closed
            created_at             TEXT NOT NULL
        );
    """)
    conn.commit()


def get_last_history_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = 'history_id'").fetchone()
    return row[0] if row else None


def set_last_history_id(conn: sqlite3.Connection, history_id: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES ('history_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (history_id,),
    )
    conn.commit()


def _event_to_dict(event: GmailEvent) -> dict:
    return {
        "event_id": event.event_id,
        "user_id": event.user_id,
        "source": event.source,
        "type": event.type,
        "timestamp": event.timestamp,
        "actors": {
            "from": {
                "name": event.actors.from_.name,
                "email": event.actors.from_.email,
            } if event.actors.from_ else None,
            "to": event.actors.to,
            "cc": event.actors.cc,
        },
        "content": {
            "subject": event.content.subject,
            "body_text": event.content.body_text,
            "thread_id": event.content.thread_id,
            "message_id": event.content.message_id,
            "in_reply_to": event.content.in_reply_to,
        },
        "metadata": {
            "labels": event.metadata.labels,
            "is_reply": event.metadata.is_reply,
            "attachments": event.metadata.attachments,
        },
        "raw": event.raw,
    }


def save_todo(
    conn: sqlite3.Connection,
    event_id: str,
    message_id: str,
    thread_id: str,
    result: dict,
    user_email: str = "",
) -> None:
    from datetime import datetime, timezone
    todo = result.get("todo") or {}
    authuser = f"?authuser={user_email}" if user_email else ""
    relevant_link = (
        todo.get("relevant_link")
        or f"https://mail.google.com/mail/u/0/{authuser}#all/{thread_id}"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO todos (
            todo_id, event_id, message_id, thread_id,
            title, suggested_action, draft, urgency,
            estimated_time_minutes, due_date, relevant_link, reasoning,
            raw_llm_response, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            f"todo_{message_id}",
            event_id,
            message_id,
            thread_id,
            todo.get("title"),
            todo.get("suggested_action"),
            todo.get("draft"),
            todo.get("urgency"),
            todo.get("estimated_time_minutes"),
            todo.get("due_date"),
            relevant_link,
            result.get("reasoning", ""),
            result.get("_raw"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def save_event(conn: sqlite3.Connection, event: GmailEvent) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO events (event_id, user_id, type, timestamp, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event.event_id,
            event.user_id,
            event.type,
            event.timestamp,
            json.dumps(_event_to_dict(event)),
        ),
    )
    conn.commit()
