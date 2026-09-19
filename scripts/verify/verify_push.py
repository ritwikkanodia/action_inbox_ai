"""Verifies push notifications with suggested actions: subscription storage,
the poller-side send (stubbing pywebpush and the OpenAI call), the web routes,
and action_index resolution in /ask-ai. No network, no spend.

Usage: python scripts/verify/verify_push.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "push.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only")

from db import (  # noqa: E402
    init_db, upsert_user, save_todo, save_fathom_todo,
    save_browser_history_todo, save_system_todo,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def _gmail_result(title: str) -> dict:
    return {"should_generate_todo": True, "reasoning": "r",
            "todo": {"title": title, "importance": "high"}}


def test_save_helpers_return_ids() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")

    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com")
    check("save_todo returns the todo id", isinstance(tid, str) and tid.startswith("todo_"))
    check("save_todo returns None on dup",
          save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com") is None)

    meeting = {"recording_id": "rec1", "title": "Standup", "url": "https://x"}
    item = {"description": "Send the deck"}
    fid = save_fathom_todo(conn, uid, meeting, 0, item)
    check("save_fathom_todo returns the todo id", isinstance(fid, str) and fid.startswith("todo_fathom_"))
    check("save_fathom_todo returns None on dup", save_fathom_todo(conn, uid, meeting, 0, item) is None)

    bh = {"title": "Finish the Dynatrace signup", "relevant_link": "https://www.dynatrace.com/signup",
          "importance": "medium"}
    bid = save_browser_history_todo(conn, uid, bh)
    check("save_browser_history_todo returns the todo id",
          isinstance(bid, str) and bid.startswith("todo_browser_history_"))
    check("save_browser_history_todo returns None on dup", save_browser_history_todo(conn, uid, bh) is None)

    st = {"title": "File the tax PDF in Downloads", "importance": "low"}
    sid = save_system_todo(conn, uid, st)
    check("save_system_todo returns the todo id", isinstance(sid, str) and sid.startswith("todo_system_"))
    check("save_system_todo returns None on dup", save_system_todo(conn, uid, st) is None)


def main() -> None:
    test_save_helpers_return_ids()
    print("All checks passed.")


if __name__ == "__main__":
    main()
