import difflib
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pollers.gmail.events import GmailEvent


LEGACY_USER_EMAIL_DEFAULT = "ritwikkanodia2@gmail.com"

# State keys that used to live in the global `state` table but are actually
# per-user poll cursors. Migrated into `user_state` for the legacy user.
_LEGACY_USER_STATE_KEYS = (
    "history_id",
    "fathom_last_polled_at",
    "browser_history_last_polled_at",
    "system_last_polled_at",
    "system_snapshot_hash",
)


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


def _legacy_user_email() -> str:
    return os.environ.get("LEGACY_USER_EMAIL", LEGACY_USER_EMAIL_DEFAULT)


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

        CREATE TABLE IF NOT EXISTS users (
            user_id       TEXT PRIMARY KEY,
            email         TEXT NOT NULL UNIQUE,
            name          TEXT,
            picture_url   TEXT,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_state (
            user_id TEXT NOT NULL,
            key     TEXT NOT NULL,
            value   TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        CREATE TABLE IF NOT EXISTS source_connections (
            user_id       TEXT NOT NULL,
            source        TEXT NOT NULL,
            auth_type     TEXT NOT NULL
                              CHECK (auth_type IN ('api_key','oauth2')),
            credentials   TEXT NOT NULL,
            connected_at  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (user_id, source)
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
            user_id                TEXT,
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

    # ---- Multi-user migration ----------------------------------------------
    # Refresh todos column set after any prior migrations so we can decide
    # whether to add user_id.
    todo_cols = {row[1] for row in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "user_id" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN user_id TEXT")

    sc_cols = {row[1] for row in conn.execute("PRAGMA table_info(source_connections)").fetchall()}
    needs_sc_rebuild = "user_id" not in sc_cols

    has_orphan_todos = bool(
        conn.execute("SELECT 1 FROM todos WHERE user_id IS NULL LIMIT 1").fetchone()
    )
    has_legacy_sc_rows = needs_sc_rebuild and bool(
        conn.execute("SELECT 1 FROM source_connections LIMIT 1").fetchone()
    )
    placeholders = ",".join("?" * len(_LEGACY_USER_STATE_KEYS))
    has_legacy_state = bool(
        conn.execute(
            f"SELECT 1 FROM state WHERE key IN ({placeholders}) LIMIT 1",
            _LEGACY_USER_STATE_KEYS,
        ).fetchone()
    )
    needs_backfill = has_orphan_todos or has_legacy_sc_rows or has_legacy_state

    legacy_user_id = None
    if needs_backfill:
        legacy_user_id = upsert_user(conn, _legacy_user_email())

    if needs_sc_rebuild:
        conn.execute("""
            CREATE TABLE source_connections_new (
                user_id       TEXT NOT NULL,
                source        TEXT NOT NULL,
                auth_type     TEXT NOT NULL
                                  CHECK (auth_type IN ('api_key','oauth2')),
                credentials   TEXT NOT NULL,
                connected_at  TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (user_id, source)
            )
        """)
        if legacy_user_id and has_legacy_sc_rows:
            conn.execute(
                "INSERT INTO source_connections_new "
                "(user_id, source, auth_type, credentials, connected_at, updated_at) "
                "SELECT ?, source, auth_type, credentials, connected_at, updated_at "
                "FROM source_connections",
                (legacy_user_id,),
            )
        conn.execute("DROP TABLE source_connections")
        conn.execute("ALTER TABLE source_connections_new RENAME TO source_connections")

    if has_orphan_todos and legacy_user_id:
        conn.execute(
            "UPDATE todos SET user_id = ? WHERE user_id IS NULL",
            (legacy_user_id,),
        )

    if has_legacy_state and legacy_user_id:
        for key in _LEGACY_USER_STATE_KEYS:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
            if not row:
                continue
            conn.execute(
                "INSERT INTO user_state (user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (legacy_user_id, key, row[0]),
            )
            conn.execute("DELETE FROM state WHERE key = ?", (key,))

    conn.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedup
            ON todos(user_id, source, dedup_key) WHERE dedup_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_todos_user_status_created
            ON todos(user_id, status, created_at DESC);
    """)
    # Drop the older single-user dedup index if it survived from a previous
    # schema, since multi-user dedup must include user_id.
    conn.execute("DROP INDEX IF EXISTS idx_todos_source_status_created")
    conn.execute("DROP INDEX IF EXISTS idx_todos_status_created")
    # If a legacy unique dedup index without user_id exists, drop it. The
    # earlier CREATE UNIQUE INDEX IF NOT EXISTS only no-ops on exact-name
    # match, so older builds may still have a same-named non-user-scoped
    # variant. Force-rebuild only if the index def doesn't include user_id.
    legacy_dedup = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_todos_dedup'"
    ).fetchone()
    if legacy_dedup and "user_id" not in (legacy_dedup[0] or ""):
        conn.execute("DROP INDEX idx_todos_dedup")
        conn.execute(
            "CREATE UNIQUE INDEX idx_todos_dedup "
            "ON todos(user_id, source, dedup_key) WHERE dedup_key IS NOT NULL"
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_user(
    conn: sqlite3.Connection,
    email: str,
    name: str | None = None,
    picture_url: str | None = None,
) -> str:
    """Insert or update a user by email. Returns the user's UUID.

    Existing users get name/picture/last_login_at refreshed if values are
    provided; passing None for those fields leaves the existing value alone.
    """
    now = _now()
    row = conn.execute(
        "SELECT user_id FROM users WHERE email = ?", (email,)
    ).fetchone()
    if row:
        user_id = row[0]
        sets = ["last_login_at = ?"]
        vals: list = [now]
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if picture_url is not None:
            sets.append("picture_url = ?")
            vals.append(picture_url)
        vals.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?", vals)
        conn.commit()
        return user_id

    user_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO users (user_id, email, name, picture_url, created_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, email, name, picture_url, now, now),
    )
    conn.commit()
    return user_id


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT user_id, email, name, picture_url, created_at, last_login_at "
        "FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "email": row[1],
        "name": row[2],
        "picture_url": row[3],
        "created_at": row[4],
        "last_login_at": row[5],
    }


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute(
        "SELECT user_id, email, name, picture_url, created_at, last_login_at "
        "FROM users WHERE email = ?",
        (email,),
    ).fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "email": row[1],
        "name": row[2],
        "picture_url": row[3],
        "created_at": row[4],
        "last_login_at": row[5],
    }


def list_active_users(conn: sqlite3.Connection) -> list[dict]:
    """Users that have at least one connected source. Used by the poller."""
    rows = conn.execute(
        "SELECT u.user_id, u.email, u.name, u.picture_url "
        "FROM users u "
        "WHERE EXISTS (SELECT 1 FROM source_connections sc WHERE sc.user_id = u.user_id)"
    ).fetchall()
    return [
        {"user_id": r[0], "email": r[1], "name": r[2], "picture_url": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Per-user state
# ---------------------------------------------------------------------------


def get_user_state(conn: sqlite3.Connection, user_id: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM user_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    return row[0] if row else None


def set_user_state(conn: sqlite3.Connection, user_id: str, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO user_state (user_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
        (user_id, key, value),
    )
    conn.commit()


def clear_user_state(conn: sqlite3.Connection, user_id: str, key: str) -> None:
    conn.execute(
        "DELETE FROM user_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Source connections
# ---------------------------------------------------------------------------


def get_source_connection(
    conn: sqlite3.Connection, user_id: str, source: str
) -> dict | None:
    row = conn.execute(
        "SELECT source, auth_type, credentials, connected_at, updated_at "
        "FROM source_connections WHERE user_id = ? AND source = ?",
        (user_id, source),
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
    conn: sqlite3.Connection,
    user_id: str,
    source: str,
    auth_type: str,
    credentials: dict,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO source_connections
            (user_id, source, auth_type, credentials, connected_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, source) DO UPDATE SET
            auth_type    = excluded.auth_type,
            credentials  = excluded.credentials,
            updated_at   = excluded.updated_at
        """,
        (user_id, source, auth_type, json.dumps(credentials), now, now),
    )
    conn.commit()


def clear_source_connection(
    conn: sqlite3.Connection, user_id: str, source: str
) -> None:
    conn.execute(
        "DELETE FROM source_connections WHERE user_id = ? AND source = ?",
        (user_id, source),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Per-user poll cursors (thin wrappers around user_state)
# ---------------------------------------------------------------------------


def get_last_history_id(conn: sqlite3.Connection, user_id: str) -> str | None:
    return get_user_state(conn, user_id, "history_id")


def set_last_history_id(conn: sqlite3.Connection, user_id: str, history_id: str) -> None:
    set_user_state(conn, user_id, "history_id", history_id)


def get_fathom_last_polled_at(conn: sqlite3.Connection, user_id: str) -> str | None:
    return get_user_state(conn, user_id, "fathom_last_polled_at")


def set_fathom_last_polled_at(conn: sqlite3.Connection, user_id: str, ts: str) -> None:
    set_user_state(conn, user_id, "fathom_last_polled_at", ts)


def get_browser_history_last_polled_at(
    conn: sqlite3.Connection, user_id: str
) -> str | None:
    return get_user_state(conn, user_id, "browser_history_last_polled_at")


def set_browser_history_last_polled_at(
    conn: sqlite3.Connection, user_id: str, ts: str
) -> None:
    set_user_state(conn, user_id, "browser_history_last_polled_at", ts)


def get_system_last_polled_at(conn: sqlite3.Connection, user_id: str) -> str | None:
    return get_user_state(conn, user_id, "system_last_polled_at")


def set_system_last_polled_at(conn: sqlite3.Connection, user_id: str, ts: str) -> None:
    set_user_state(conn, user_id, "system_last_polled_at", ts)


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------

_VALID_URGENCY = {"low", "medium", "high"}


def _save_todo(
    conn: sqlite3.Connection,
    *,
    user_id: str,
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
            todo_id, user_id, source, dedup_key, title, suggested_action, urgency,
            estimated_time_minutes, due_date, relevant_link, reasoning,
            status, source_meta, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            todo_id, user_id, source, dedup_key, title, suggested_action, urgency,
            estimated_time_minutes, due_date, relevant_link, reasoning or "",
            json.dumps(source_meta) if source_meta else None, now, now,
        ),
    )
    conn.commit()
    return conn.total_changes > before


def get_open_browser_history_titles(
    conn: sqlite3.Connection, user_id: str, limit: int = 40
) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM todos "
        "WHERE user_id = ? AND source = 'browser_history' AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def save_browser_history_todo(
    conn: sqlite3.Connection, user_id: str, todo: dict
) -> bool:
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    norm = _normalize_url(todo.get("relevant_link"))
    if not norm:
        return False
    # Fuzzy-match guard against any browser_history todo (any status) in last 30d
    # for THIS user.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos "
        "WHERE user_id = ? AND source = 'browser_history' AND created_at >= ?",
        (user_id, cutoff),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.80:
            return False
    dedup = hashlib.sha1(norm.encode()).hexdigest()[:12]
    return _save_todo(
        conn,
        user_id=user_id,
        todo_id=f"todo_browser_history_{user_id[:8]}_{dedup}",
        source="browser_history",
        dedup_key=dedup,
        title=title,
        suggested_action=todo.get("suggested_action", ""),
        urgency=todo.get("urgency", "medium"),
        relevant_link=todo.get("relevant_link", ""),
        reasoning=todo.get("reasoning", ""),
        source_meta={"normalized_url": norm, "raw_url": todo.get("relevant_link", "")},
    )


def save_fathom_todo(
    conn: sqlite3.Connection, user_id: str, meeting: dict, idx: int, item: dict
) -> None:
    recording_id = str(meeting.get("recording_id", ""))
    dedup = f"{recording_id}_{idx}"
    meeting_title = meeting.get("meeting_title") or meeting.get("title", "")
    assignee = item.get("assignee") or {}
    reasoning = f"Action item from Fathom meeting: {meeting_title}"
    if assignee.get("name"):
        reasoning += f" — assigned to {assignee['name']}"
    _save_todo(
        conn,
        user_id=user_id,
        todo_id=f"todo_fathom_{user_id[:8]}_{dedup}",
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
    user_id: str,
    gmail_email: str = "",
) -> bool:
    todo = result.get("todo") or {}
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos "
        "WHERE user_id = ? AND source = 'gmail' AND created_at >= ?",
        (user_id, cutoff),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.80:
            return False
    authuser = f"?authuser={gmail_email}" if gmail_email else ""
    relevant_link = (
        todo.get("relevant_link")
        or f"https://mail.google.com/mail/u/0/{authuser}#all/{thread_id}"
    )
    return _save_todo(
        conn,
        user_id=user_id,
        todo_id=f"todo_{user_id[:8]}_{message_id}",
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
    user_id: str,
    title: str,
    urgency: str = "medium",
    due_date: str | None = None,
    suggested_action: str = "",
) -> str:
    todo_id = f"todo_user_{uuid.uuid4().hex[:12]}"
    _save_todo(
        conn,
        user_id=user_id,
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


def get_open_system_todos(
    conn: sqlite3.Connection, user_id: str, limit: int = 20
) -> list[str]:
    rows = conn.execute(
        "SELECT title FROM todos "
        "WHERE user_id = ? AND source = 'system' AND status = 'open' "
        "ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def save_system_todo(conn: sqlite3.Connection, user_id: str, todo: dict) -> bool:
    title = (todo.get("title") or "").strip()
    if not title:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    existing = conn.execute(
        "SELECT title FROM todos "
        "WHERE user_id = ? AND source = 'system' AND created_at >= ?",
        (user_id, cutoff),
    ).fetchall()
    for (et,) in existing:
        if et and difflib.SequenceMatcher(None, title.lower(), et.lower()).ratio() > 0.60:
            return False
    uid = uuid.uuid4().hex[:12]
    return _save_todo(
        conn,
        user_id=user_id,
        todo_id=f"todo_system_{user_id[:8]}_{uid}",
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
