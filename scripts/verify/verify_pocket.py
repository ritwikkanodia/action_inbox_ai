"""Verifies the Pocket source: the MCP result parsing, the todo mapping, the
poller's cursor and skip rules, the source registry, and the web surface.

Pocket (heypocket.com) exposes its action items through a hosted MCP server;
`pollers/pocket/client.py` is the only thing that talks to it, and this script
stubs that one function, so nothing here touches the network. Usage:
python scripts/verify/verify_pocket.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp, "pocket.db")
os.environ["DB_PATH"] = DB_PATH
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-not-a-real-key")

import app as app_module  # noqa: E402  (runs load_dotenv; pin sources after it)

os.environ["ENABLED_SOURCES"] = "gmail,fathom,pocket,morning_digest"

from db import (  # noqa: E402
    get_pocket_last_polled_at,
    get_source_connection,
    get_todo,
    init_db,
    list_todos,
    save_fathom_todo,
    save_pocket_todo,
    save_user_todo,
    set_source_credentials,
    upsert_user,
)
from pollers.pocket import client as pocket_client  # noqa: E402
from pollers.pocket import poller as pocket_poller  # noqa: E402
from sources import DEFAULT_ENABLED_SOURCES, DISCOVERY_SOURCE_NAMES, KNOWN_SOURCES  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


# The shape `search_pocket_actionitems` returned against a real account on
# 2026-09-25, trimmed to three items: a reminder with a due date, a drafted
# message with none, and one already completed.
REMINDER = {
    "actionItemId": "1cf6519d-dce6-4135-9fbf-517bc0856f36",
    "actionType": "create_reminder",
    "assignee": "me",
    "context": "Ritwik needs to update the roadmap document to include the client's request.",
    "dueDate": "2026-08-27T00:00:00.000Z",
    "label": "Update roadmap with EU channels",
    "payload": {"reminder": {"dueDateTime": "2026-08-27T23:59:59Z",
                             "title": "Update roadmap document and send to Cynthia"}},
    "priority": "high",
    "recordingDate": "2026-08-27T13:31:46.000Z",
    "recordingId": "10ed240c-2bed-4f38-8f9f-4f03353a95b2",
    "recordingTitle": "Phase One Pilot and Commercials Review",
    "status": "TODO",
}
MESSAGE = {
    "actionItemId": "42312a4e-5af0-4429-a8b8-b84723b61b57",
    "actionType": "send_message",
    "assignee": "Other",
    "context": "Cynthia needs to review the roadmap and provide feedback.",
    "dueDate": None,
    "label": "Review roadmap and phases",
    "payload": {"message": {"body": "Hi Cynthia, just following up on the roadmap document.",
                            "to": "null"}},
    "priority": "medium",
    "recordingDate": "2026-08-27T13:31:46.000Z",
    "recordingId": "10ed240c-2bed-4f38-8f9f-4f03353a95b2",
    "recordingTitle": "Phase One Pilot and Commercials Review",
    "status": "TODO",
}
EMAIL = {
    "actionItemId": "25abb218-0705-4cca-bb03-96ba7cb58b49",
    "actionType": "draft_email",
    "assignee": "me",
    "context": "Ritwik needs to formalise the information sharing process.",
    "dueDate": None,
    "label": "Draft NDA for healthcare client",
    "payload": {"email": {"body": "Please find the NDA attached.",
                          "subject": "NDA for Pilot Discussion"}},
    "priority": "CRITICAL",
    "recordingDate": "2026-09-14T04:46:39.000Z",
    "recordingId": "5bec5aff-eb96-4abb-8877-0e1be04b1259",
    "recordingTitle": "Hospital Automation and Patient Experience",
    "status": "TODO",
}
DONE = dict(REMINDER, actionItemId="done-1", label="Already done", status="COMPLETED")
CANCELLED = dict(REMINDER, actionItemId="cancelled-1", label="Dropped", status="CANCELLED")


class FakeContent:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResult:
    def __init__(self, payload, structured=None, is_error=False):
        self.content = [FakeContent(json.dumps(payload))]
        self.structured_content = structured
        self.is_error = is_error


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def check_parsing() -> None:
    print("\n-- MCP result parsing --")
    envelope = {"success": True, "data": {"actions": [REMINDER, MESSAGE], "limit": 50, "total": 2}}
    check("actions come out of the text content",
          pocket_client.parse_search_result(FakeResult(envelope)) == [REMINDER, MESSAGE])
    check("structured content wins when present",
          pocket_client.parse_search_result(FakeResult({}, structured=envelope)) == [REMINDER, MESSAGE])
    check("empty data is an empty list",
          pocket_client.parse_search_result(FakeResult({"success": True, "data": {"actions": []}})) == [])
    try:
        pocket_client.parse_search_result(FakeResult({"error": "bad key"}, is_error=True))
        check("a tool error raises", False)
    except RuntimeError as exc:
        check("a tool error raises with the server's text", "bad key" in str(exc))

    class FakeMCPError(Exception):
        def __init__(self):
            super().__init__("Server returned an error response")
            self.error = type("Err", (), {"code": -32001, "message": "Invalid API key", "data": None})()

    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [FakeMCPError()])])
    msg = pocket_client.describe_error(nested)
    check("a task-group failure is described by its leaf",
          "Invalid API key" in msg and "TaskGroup" not in msg and "outer" not in msg)
    check("a plain exception describes itself",
          pocket_client.describe_error(ValueError("nope")) == "ValueError: nope")


def check_mapping(conn, user_id) -> None:
    print("\n-- todo mapping --")
    tid = save_pocket_todo(conn, user_id, REMINDER)
    check("reminder saves", bool(tid))
    row = get_todo(conn, user_id, tid)
    check("title is the label with the recording for context",
          row["title"] == "Update roadmap with EU channels — Phase One Pilot and Commercials Review")
    untitled = dict(REMINDER, actionItemId="untitled-1", label="Item from an untitled recording",
                    recordingTitle="")
    urow = get_todo(conn, user_id, save_pocket_todo(conn, user_id, untitled))
    check("no recording title means no suffix", urow["title"] == "Item from an untitled recording")
    meeting = {"recording_id": "rec1", "title": "Standup", "url": "https://x"}
    fid = save_fathom_todo(conn, user_id, meeting, 0, {"description": "Send the deck"})
    check("Fathom titles carry the meeting too",
          get_todo(conn, user_id, fid)["title"] == "Send the deck — Standup")
    check("importance follows priority", row["importance"] == "high")
    check("due date passes through as ISO", row["due_date"] == "2026-08-27T00:00:00.000Z")
    check("suggested action is the reminder title",
          row["suggested_action"] == "Update roadmap document and send to Cynthia")
    check("reasoning names the recording",
          "Phase One Pilot and Commercials Review" in row["reasoning"])
    check("no recording link is invented", not row["relevant_link"])
    meta = row["source_meta"]
    check("source_meta keeps provenance",
          meta["action_item_id"] == REMINDER["actionItemId"]
          and meta["recording_id"] == REMINDER["recordingId"]
          and meta["recording_title"] == REMINDER["recordingTitle"]
          and meta["action_type"] == "create_reminder"
          and meta["assignee"] == "me"
          and meta["context"] == REMINDER["context"])
    check("dedup on the action item id", save_pocket_todo(conn, user_id, REMINDER) is None)

    mid = save_pocket_todo(conn, user_id, MESSAGE)
    mrow = get_todo(conn, user_id, mid)
    check("message suggested action carries the draft",
          mrow["suggested_action"] == "Send: Hi Cynthia, just following up on the roadmap document.")
    check("no due date stays NULL", mrow["due_date"] is None)
    check("assignee 'Other' still saves and is named in the reasoning",
          "assigned to Other" in mrow["reasoning"])

    eid = save_pocket_todo(conn, user_id, EMAIL)
    erow = get_todo(conn, user_id, eid)
    check("critical priority folds into high", erow["importance"] == "high")
    check("email suggested action carries subject and body",
          erow["suggested_action"] == "Email 'NDA for Pilot Discussion': Please find the NDA attached.")

    bare = dict(EMAIL, actionItemId="bare-1", priority=None, payload={}, label="  ")
    check("blank label is skipped", save_pocket_todo(conn, user_id, bare) is None)
    bare["label"] = "Bare item"
    brow = get_todo(conn, user_id, save_pocket_todo(conn, user_id, bare))
    check("missing priority defaults to medium", brow["importance"] == "medium")
    check("missing payload falls back to the label", brow["suggested_action"] == "Bare item")

    # Cross-source near-duplicates: the same task arriving as a Pocket item
    # and as the mail (or the typed todo) about it must not become two rows.
    save_user_todo(conn, user_id, "Send the NDA to the healthcare client")
    twin = dict(EMAIL, actionItemId="twin-1", label="Send NDA to the healthcare client")
    check("a near-duplicate of a recent todo from another source is skipped",
          save_pocket_todo(conn, user_id, twin) is None)
    fresh = dict(EMAIL, actionItemId="fresh-1", label="Book the venue for the offsite")
    check("a genuinely new title still saves", save_pocket_todo(conn, user_id, fresh) is not None)


def check_poller(conn, user_id) -> None:
    print("\n-- poller --")
    calls = []

    def fake_search(api_key, recording_date_from=None, status=None):
        calls.append((api_key, recording_date_from, status))
        if status == "TODO":
            return [REMINDER, MESSAGE, EMAIL]
        if status == "IN_PROGRESS":
            return [dict(EMAIL, actionItemId="wip-1", label="Half done", status="IN_PROGRESS")]
        return [REMINDER, MESSAGE, DONE, CANCELLED, EMAIL]

    pocket_poller.search_action_items = fake_search

    other, _ = upsert_user(conn, "nokey@example.com")
    check("no connection: returns 0 without calling Pocket",
          pocket_poller.poll(conn, other) == 0 and calls == [])

    # A user of its own: the mapping checks already saved these items for the
    # first one, and dedup would (correctly) hide the poller's work.
    user_id, _ = upsert_user(conn, "poller@example.com")
    set_source_credentials(conn, user_id, "pocket", "api_key", {"api_key": "pk_verify_123456"})
    before = datetime.now(timezone.utc)
    saved = pocket_poller.poll(conn, user_id)
    check("first poll is a backfill: one call per open status",
          [c[2] for c in calls] == ["TODO", "IN_PROGRESS"]
          and all(c[0] == "pk_verify_123456" for c in calls))
    backfill_from = datetime.fromisoformat(calls[0][1])
    check("backfill window is POCKET_BACKFILL_DAYS back",
          abs((before - backfill_from) - timedelta(days=pocket_poller.BACKFILL_DAYS)) < timedelta(seconds=2))
    check("backfill saves the open items from both calls", saved == 4)
    titles = {t["title"] for t in list_todos(conn, user_id)}
    check("in-progress item counted as open", any(t.startswith("Half done") for t in titles))
    cursor = get_pocket_last_polled_at(conn, user_id)
    check("cursor written after the poll",
          cursor is not None and datetime.fromisoformat(cursor) >= before.replace(microsecond=0))

    calls.clear()
    saved = pocket_poller.poll(conn, user_id)
    check("second poll is one unfiltered call", len(calls) == 1 and calls[0][2] is None)
    check("second poll re-fetches nothing new", saved == 0)
    titles = {t["title"] for t in list_todos(conn, user_id)}
    check("skipped items never reach the list",
          not any(t.startswith(("Already done", "Dropped")) for t in titles))
    _, lower, _ = calls[-1]
    lookback = datetime.fromisoformat(cursor) - datetime.fromisoformat(lower)
    check("second poll looks back 24h behind the cursor",
          abs(lookback - timedelta(hours=pocket_poller.LOOKBACK_HOURS)) < timedelta(seconds=2))

    def boom(api_key, recording_date_from=None, status=None):
        raise RuntimeError("Pocket is down")

    pocket_poller.search_action_items = boom
    stale = get_pocket_last_polled_at(conn, user_id)
    try:
        pocket_poller.poll(conn, user_id)
        check("a failed fetch raises to the poll loop", False)
    except RuntimeError:
        check("a failed fetch raises to the poll loop", True)
    check("a failed fetch leaves the cursor alone",
          get_pocket_last_polled_at(conn, user_id) == stale)


def check_migration() -> None:
    """A database created before Pocket carries a CHECK on `source` without
    it. SQLite can't loosen a CHECK in place, and INSERT OR IGNORE swallows
    the violation, so without a rebuild every Pocket save silently returns
    None — which is exactly how this surfaced against a real database."""
    print("\n-- migration --")
    path = os.path.join(_tmp, "pre_pocket.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)  # current schema...
    conn.executescript("""
        DROP TABLE todos;
        CREATE TABLE todos (
            todo_id TEXT PRIMARY KEY, user_id TEXT,
            source TEXT NOT NULL CHECK (source IN ('gmail','fathom','browser_history','system','user')),
            account_id TEXT, dedup_key TEXT, title TEXT, suggested_action TEXT,
            importance TEXT, estimated_time_minutes INTEGER, due_date TEXT, relevant_link TEXT,
            reasoning TEXT, status TEXT NOT NULL DEFAULT 'open', decision TEXT, ai_thread TEXT,
            executor_state TEXT, action_options TEXT, source_meta TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_todos_dedup ON todos(user_id, source, dedup_key) WHERE dedup_key IS NOT NULL;
        INSERT INTO todos (todo_id,user_id,source,dedup_key,title,status,ai_thread,created_at,updated_at)
        VALUES ('t1','u1','gmail','m1','Existing gmail todo','open','[{"role":"user","content":"hi"}]','2026-01-01','2026-01-01');
    """)  # ...then the todos table as a pre-Pocket database has it
    conn.commit()
    try:
        conn.execute("INSERT INTO todos (todo_id,user_id,source,status,created_at,updated_at) "
                     "VALUES ('p','u1','pocket','open','','')")
        check("fixture really rejects pocket", False)
    except sqlite3.IntegrityError:
        check("fixture really rejects pocket", True)
    conn.rollback()

    init_db(conn)
    check("save works after migration", save_pocket_todo(conn, "u1", REMINDER) is not None)
    row = conn.execute("SELECT title, ai_thread FROM todos WHERE todo_id = 't1'").fetchone()
    check("existing rows survive the rebuild with their data",
          row is not None and row["title"] == "Existing gmail todo" and "hi" in row["ai_thread"])
    check("dedup still enforced after the rebuild", save_pocket_todo(conn, "u1", REMINDER) is None)
    idx = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_todos_dedup'").fetchone()
    check("dedup index recreated with user_id", idx is not None and "user_id" in idx[0])
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'todos'").fetchone()[0]
    check("rebuilt table carries the current CHECK", "'pocket'" in ddl)
    init_db(conn)
    check("migration is idempotent",
          conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 2)

    # Rows saved before titles carried the recording: the context is in
    # source_meta, so init_db adds it. Once, not on every start.
    conn.execute(
        "INSERT INTO todos (todo_id,user_id,source,dedup_key,title,source_meta,status,created_at,updated_at) VALUES "
        "('p-old','u1','pocket','old-1','Friday follow-up call',"
        "'{\"recording_title\": \"Hospital Automation\"}','open','2026-09-01','2026-09-01'),"
        "('f-old','u1','fathom','old-2','Send the deck',"
        "'{\"meeting_title\": \"Standup\"}','open','2026-09-01','2026-09-01'),"
        "('p-none','u1','pocket','old-3','No recording known','{\"recording_title\": \"\"}','open','2026-09-01','2026-09-01')"
    )
    conn.commit()
    init_db(conn)
    titles = {r[0]: r[1] for r in conn.execute("SELECT todo_id, title FROM todos")}
    check("old Pocket titles gain the recording",
          titles["p-old"] == "Friday follow-up call — Hospital Automation")
    check("old Fathom titles gain the meeting", titles["f-old"] == "Send the deck — Standup")
    check("no known recording, title untouched", titles["p-none"] == "No recording known")
    init_db(conn)
    check("title backfill is idempotent",
          conn.execute("SELECT title FROM todos WHERE todo_id='p-old'").fetchone()[0]
          == "Friday follow-up call — Hospital Automation")
    conn.close()


def check_registry() -> None:
    print("\n-- registry --")
    if "POCKET_BACKFILL_DAYS" not in os.environ:
        check("backfill defaults to a week, the digest's age-out", pocket_poller.BACKFILL_DAYS == 7)
    check("pocket is a known source", "pocket" in KNOWN_SOURCES)
    check("pocket is on by default", "pocket" in DEFAULT_ENABLED_SOURCES)
    check("pocket has a Settings entry", "pocket" in DISCOVERY_SOURCE_NAMES)


def check_web(user_id) -> None:
    print("\n-- web --")
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"

    page = client.get("/settings").get_data(as_text=True)
    check("settings page renders a Pocket card", 'id="pocket-card"' in page)
    group, fathom, pocket, after = (page.find(m) for m in
                                    ('id="notetakers-card"', 'id="fathom-card"', 'id="pocket-card"',
                                     'id="extra-sources"'))
    check("Fathom and Pocket sit inside one AI notetakers card",
          -1 < group < fathom < pocket < after
          and page.count('class="source-card"', fathom, after) == 0)
    check("pocket is not rendered as a generic extra card",
          all(s["name"] != "pocket" for s in client.get("/settings.json").get_json()["sources"]["extra"]))

    settings = client.get("/settings.json").get_json()["sources"]["pocket"]
    check("settings.json reports the connection",
          settings["connected"] is True and settings["api_key_preview"] == "...123456"
          and settings["enabled"] is True)

    r = client.post("/settings/sources/pocket", json={"disconnect": True}).get_json()
    check("disconnect clears the key", r["ok"] and r["connected"] is False)
    check("disconnected in settings.json",
          client.get("/settings.json").get_json()["sources"]["pocket"]["connected"] is False)

    r = client.post("/settings/sources/pocket", json={"api_key": ""})
    check("empty key is a 400", r.status_code == 400)
    r = client.post("/settings/sources/pocket", json={"api_key": " pk_new_key_abcdef "}).get_json()
    check("connect stores the trimmed key and previews it",
          r["ok"] and r["api_key_preview"] == "...abcdef")
    conn = open_db()
    stored = get_source_connection(conn, user_id, "pocket")["credentials"]["api_key"]
    check("stored key is trimmed", stored == "pk_new_key_abcdef")

    r = client.post("/settings/sources/pocket/enabled", json={"enabled": False}).get_json()
    check("pocket can be paused", r["ok"] and r["enabled"] is False)

    todo = [t for t in list_todos(conn, user_id) if t["source"] == "pocket"][0]
    ctx = client.get(f"/todos/{todo['todo_id']}/context").get_json()
    check("context route serves the recording title and context",
          ctx["source"] == "pocket" and ctx["recording_title"] and ctx["context"]
          and "assignee" in ctx)
    inbox = client.get("/").get_data(as_text=True)
    check("inbox offers a Pocket filter", "data-source=\"pocket\"" in inbox or "'pocket'" in inbox)
    conn.close()


def main() -> None:
    conn = open_db()
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    check_parsing()
    check_mapping(conn, user_id)
    check_poller(conn, user_id)
    check_migration()
    check_registry()
    set_source_credentials(conn, user_id, "pocket", "api_key", {"api_key": "pk_verify_123456"})
    conn.close()
    check_web(user_id)
    print("\nAll Pocket checks passed.")


if __name__ == "__main__":
    main()
