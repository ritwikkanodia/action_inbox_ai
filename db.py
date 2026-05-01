import difflib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pollers.gmail.events import GmailEvent


def _normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url.strip())
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "/").rstrip("/") or "/"
    return f"{host}{path}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(conn: sqlite3.Connection) -> None:
    # Fresh DBs get the final schema with CHECK constraints. Existing DBs are
    # migrated below via ALTER TABLE; SQLite can't add CHECK constraints to an
    # existing table without a rebuild, so legacy rows keep their permissive
    # schema and we enforce enums in Python in the save_* helpers.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_connections (
            source        TEXT PRIMARY KEY,
            auth_type     TEXT NOT NULL
                              CHECK (auth_type IN ('api_key','oauth2')),
            credentials   TEXT NOT NULL,
            connected_at  TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id   TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            type       TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            payload    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS todos (
            todo_id                TEXT PRIMARY KEY,
            source                 TEXT NOT NULL
                                       CHECK (source IN ('gmail','fathom','browser_history','system','user')),
            dedup_key              TEXT,
            title                  TEXT,
            suggested_action       TEXT,
            urgency                TEXT CHECK (urgency IS NULL OR urgency IN ('low','medium','high')),
            estimated_time_minutes INTEGER,
            due_date               TEXT,
            relevant_link          TEXT,
            reasoning              TEXT,
            status                 TEXT NOT NULL DEFAULT 'open'
                                       CHECK (status IN ('open','ongoing','closed')),
            decision               TEXT CHECK (decision IS NULL OR decision IN ('accepted','rejected')),
            ai_thread              TEXT,
            source_meta            TEXT,
            created_at             TEXT NOT NULL,
            updated_at             TEXT NOT NULL
        );
    """)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}

    if "draft" in cols:
        conn.execute("ALTER TABLE todos DROP COLUMN draft")
        cols.discard("draft")

    legacy_cols = {"event_id", "message_id", "thread_id", "raw_llm_response"}
    needs_migration = bool(legacy_cols & cols) or "dedup_key" not in cols

    if needs_migration:
        if "dedup_key" not in cols:
            conn.execute("ALTER TABLE todos ADD COLUMN dedup_key TEXT")
        if "source_meta" not in cols:
            conn.execute("ALTER TABLE todos ADD COLUMN source_meta TEXT")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE todos ADD COLUMN updated_at TEXT")

        # Backfill source_meta from legacy columns and dedup_key from source rules.
        rows = conn.execute(
            "SELECT todo_id, source, "
            + ", ".join(
                c if c in cols else f"NULL AS {c}"
                for c in ("event_id", "message_id", "thread_id", "raw_llm_response")
            )
            + ", created_at FROM todos"
        ).fetchall()
        for todo_id, source, event_id, message_id, thread_id, raw_llm_response, created_at in rows:
            meta = {
                k: v for k, v in {
                    "event_id": event_id,
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "raw_llm_response": raw_llm_response,
                }.items() if v
            }
            if source == "gmail":
                dedup = message_id or None
            elif source == "fathom":
                dedup = message_id or None
            elif source == "browser_history":
                dedup = event_id or None
            else:
                dedup = None
            conn.execute(
                "UPDATE todos SET dedup_key = COALESCE(dedup_key, ?), "
                "source_meta = COALESCE(source_meta, ?), "
                "updated_at = COALESCE(updated_at, ?) WHERE todo_id = ?",
                (dedup, json.dumps(meta) if meta else None, created_at, todo_id),
            )

        for col in ("event_id", "message_id", "thread_id", "raw_llm_response"):
            if col in cols:
                conn.execute(f"ALTER TABLE todos DROP COLUMN {col}")

    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedup
            ON todos(source, dedup_key) WHERE dedup_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_todos_source_status_created
            ON todos(source, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_todos_status_created
            ON todos(status, created_at DESC);
    """)
    conn.commit()


_VALID_URGENCY = {"low", "medium", "high"}


def _save_todo(
    conn: sqlite3.Connection,
    *,
    todo_id: str,
    source: str,
    dedup_key: str | None,
    title: str | None,
    suggested_action: str | None = None,
    urgency: str | None = None,
    estimated_time_minutes: int | None = None,
    due_date: str | None = None,
    relevant_link: str | None = None,
    reasoning: str | None = "",
    source_meta: dict | None = None,
) -> bool:
    if urgency not in _VALID_URGENCY:
        urgency = None
    now = _now()
    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO todos (
            todo_id, source, dedup_key, title, suggested_action, urgency,
            estimated_time_minutes, due_date, relevant_link, reasoning,
            status, source_meta, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            todo_id, source, dedup_key, title, suggested_action, urgency,
            estimated_time_minutes, due_date, relevant_link, reasoning or "",
            json.dumps(source_meta) if source_meta else None, now, now,
        ),
    )
    conn.commit()
    return conn.total_changes > before


def get_source_connection(conn: sqlite3.Connection, source: str) -> dict | None:
    row = conn.execute(
        "SELECT source, auth_type, credentials, connected_at, updated_at "
        "FROM source_connections WHERE source = ?",
        (source,),
    ).fetchone()
    if not row:
        return None
    return {
        "source": row[0],
        "auth_type": row[1],
        "credentials": json.loads(row[2]),
        "connected_at": row[3],
        "updated_at": row[4],
    }


def set_source_credentials(
    conn: sqlite3.Connection, source: str, auth_type: str, credentials: dict
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO source_connections (source, auth_type, credentials, connected_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            auth_type    = excluded.auth_type,
            credentials  = excluded.credentials,
            updated_at   = excluded.updated_at
        """,
        (source, auth_type, json.dumps(credentials), now, now),
    )
    conn.commit()


def clear_source_connection(conn: sqlite3.Connection, source: str) -> None:
    conn.execute("DELETE FROM source_connections WHERE source = ?", (source,))
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


def get_fathom_last_polled_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = 'fathom_last_polled_at'").fetchone()
    return row[0] if row else None


def set_fathom_last_polled_at(conn: sqlite3.Connection, ts: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES ('fathom_last_polled_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ts,),
    )
    conn.commit()


def get_browser_history_last_polled_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = 'browser_history_last_polled_at'").fetchone()
    return row[0] if row else None


def set_browser_history_last_polled_at(conn: sqlite3.Connection, ts: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES ('browser_history_last_polled_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ts,),
    )
    conn.commit()


def get_open_browser_history_titles(conn: sqlite3.Connection, limit: int = 40) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM todos "
        "WHERE source = 'browser_history' AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def save_browser_history_todo(conn: sqlite3.Connection, todo: dict) -> bool:
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    norm = _normalize_url(todo.get("relevant_link"))
    if not norm:
        return False
    # Fuzzy-match guard against any browser_history todo (any status) in last 30d.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos "
        "WHERE source = 'browser_history' AND created_at >= ?",
        (cutoff,),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.80:
            return False
    dedup = hashlib.sha1(norm.encode()).hexdigest()[:12]
    return _save_todo(
        conn,
        todo_id=f"todo_browser_history_url_{dedup}",
        source="browser_history",
        dedup_key=dedup,
        title=title,
        suggested_action=todo.get("suggested_action", ""),
        urgency=todo.get("urgency", "medium"),
        relevant_link=todo.get("relevant_link", ""),
        reasoning=todo.get("reasoning", ""),
        source_meta={"normalized_url": norm, "raw_url": todo.get("relevant_link", "")},
    )


def save_fathom_todo(conn: sqlite3.Connection, meeting: dict, idx: int, item: dict) -> None:
    recording_id = str(meeting.get("recording_id", ""))
    dedup = f"{recording_id}_{idx}"
    meeting_title = meeting.get("meeting_title") or meeting.get("title", "")
    assignee = item.get("assignee") or {}
    reasoning = f"Action item from Fathom meeting: {meeting_title}"
    if assignee.get("name"):
        reasoning += f" — assigned to {assignee['name']}"
    _save_todo(
        conn,
        todo_id=f"todo_fathom_{dedup}",
        source="fathom",
        dedup_key=dedup,
        title=item.get("description", "(no description)"),
        suggested_action=item.get("description", ""),
        urgency="medium",
        relevant_link=item.get("recording_playback_url") or meeting.get("url", ""),
        reasoning=reasoning,
        source_meta={
            "recording_id": recording_id,
            "meeting_title": meeting_title,
            "assignee": assignee or None,
            "item_index": idx,
        },
    )


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
) -> bool:
    todo = result.get("todo") or {}
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos WHERE source = 'gmail' AND created_at >= ?",
        (cutoff,),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.80:
            return False
    authuser = f"?authuser={user_email}" if user_email else ""
    relevant_link = (
        todo.get("relevant_link")
        or f"https://mail.google.com/mail/u/0/{authuser}#all/{thread_id}"
    )
    return _save_todo(
        conn,
        todo_id=f"todo_{message_id}",
        source="gmail",
        dedup_key=message_id,
        title=title,
        suggested_action=todo.get("suggested_action"),
        urgency=todo.get("urgency"),
        estimated_time_minutes=todo.get("estimated_time_minutes"),
        due_date=todo.get("due_date"),
        relevant_link=relevant_link,
        reasoning=result.get("reasoning", ""),
        source_meta={
            "event_id": event_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "raw_llm_response": result.get("_raw"),
        },
    )


def save_user_todo(
    conn: sqlite3.Connection,
    title: str,
    urgency: str = "medium",
    due_date: str | None = None,
    suggested_action: str = "",
) -> str:
    todo_id = f"todo_user_{uuid.uuid4().hex[:12]}"
    _save_todo(
        conn,
        todo_id=todo_id,
        source="user",
        dedup_key=None,
        title=title,
        suggested_action=suggested_action,
        urgency=urgency,
        due_date=due_date,
        reasoning="",
    )
    return todo_id


def get_system_last_polled_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = 'system_last_polled_at'").fetchone()
    return row[0] if row else None


def set_system_last_polled_at(conn: sqlite3.Connection, ts: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES ('system_last_polled_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ts,),
    )
    conn.commit()


def get_open_system_todos(conn: sqlite3.Connection, limit: int = 20) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM todos "
        "WHERE source = 'system' AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def save_system_todo(conn: sqlite3.Connection, todo: dict) -> bool:
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos WHERE source = 'system' AND created_at >= ?",
        (cutoff,),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.60:
            return False
    uid = uuid.uuid4().hex[:12]
    return _save_todo(
        conn,
        todo_id=f"todo_system_{uid}",
        source="system",
        dedup_key=None,
        title=title,
        suggested_action=todo.get("suggested_action", ""),
        urgency="low",
        reasoning=todo.get("reasoning", ""),
    )


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
