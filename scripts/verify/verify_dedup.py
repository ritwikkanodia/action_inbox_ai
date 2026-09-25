"""Verifies the cross-source near-duplicate check the poller save helpers
run before inserting (`db.titles_similar`, `db.similar_recent_todo`).

The unique index only stops the *same* message or URL coming back; on real
data twelve tasks still produced 37 todos because a reminder mail, a second
account and the page the mail linked to each carry a different key. The
title pairs here are those real clusters, plus pairs that share most of
their words but are different tasks (two co-founder invites from different
people, two LinkedIn invites) and must not collapse. No LLM, no network.

Usage: python scripts/verify/verify_dedup.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp, "dedup.db")
os.environ["DB_PATH"] = DB_PATH
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-not-a-real-key")

from db import (
    init_db, list_todos, save_browser_history_todo, save_system_todo, save_todo,
    save_user_todo, similar_recent_todo, titles_similar, upsert_user,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


DUPLICATE_PAIRS = [
    ("Confirm Zoho account", "Confirm Zoho account before closure"),
    ("Upload Rentomojo KYC documents", "Upload KYC documents for Rentomojo order"),
    ("Upload verification documents to RentoMojo", "Upload missing KYC documents to Rentomojo"),
    ("Pay Jio Postpaid Mobile bill", "Pay Jio bill of ₹411.82"),
    ("Reply to Rohin re: co-founder matching invitation", "Respond to Rohin re: co-founder matching invitation"),
    ("Call Kotak Bank about possible unauthorized PhonePe link request", "Call Kotak Bank about PhonePe linking alert"),
    ("Send calendar invite for Aster AI discussion with Vinod", "Send calendar invite for Aster meeting"),
    ("Book Frontier AI Summit pass", "Book FRONTIER AI Summit Mumbai pass"),
    ("Investigate failed Cloudflare deployment for PR #3", "Investigate failed Cloudflare deployment for averentia-ai-website"),
    ("Pay Booth primary deferral deposit", "Pay the Booth deferred deposit"),
    ("Review QRFY plan and reactivate disabled QR code", "Review QRFY account upgrade for disabled QR code"),
    ("RSVP to Group Pitches - Fall'26 BA", "Confirm attendance for Group Pitches - Fall'26 BA"),
]

DISTINCT_PAIRS = [
    ("Reply to Madhav re: Aster pilot proposal and NDA", "Reply to Madhav about the call recording"),
    ("Pay Citi PremierMiles Card bill", "Pay Jio Postpaid Mobile bill"),
    ("Review and apply for Software Engineer (C++) role", "Review and apply for Outsystems Developer role at Optimum Solutions"),
    ("Submit Tuck Round 1 application", "Complete MIT Sloan MBA application"),
    ("Reply to Ishita about MemStore data migration", "Reply to Ishika re: FRONTIER sponsorship opportunity"),
    ("Clean up ~/Desktop", "Clean up ~/Downloads Screen Studio 3.7.5-4595 Apple Silicon.dmg"),
    ("RSVP to Group Pitches - Fall'26 BA", "RSVP to Fall26 EF Founder Socials"),
    ("Sign and return revised ABS NDA v1.1", "Review and sign NDA for WhatsApp Order Capture pilot"),
    ("Reply to Rohin re: co-founder matching invitation", "Reply to Akshit Gupta about co-founder matching"),
    ("Review LinkedIn invite from Siddharth Vaish", "Review LinkedIn invite from Hongqi Cai"),
    ("Attend EF Session AMA with Jonny Clifford", "Attend EF Founder Session on Pyramid Principle"),
    ("Reply to Piyush re: scheduling a connection", "Reply to Kanishk on LinkedIn"),
    ("Delete ~/Downloads Wireshark 4.6.7.dmg", "Delete ~/Downloads Postman for macOS (arm64).zip"),
    ("Investigate failed Cloudflare deployment for PR #3", "Investigate failed aster_agent deployment"),
]


def check_matcher() -> None:
    print("\n-- titles_similar --")
    missed = [p for p in DUPLICATE_PAIRS if not titles_similar(*p)]
    check(f"known duplicate clusters match (missed: {missed})", not missed)
    collapsed = [p for p in DISTINCT_PAIRS if titles_similar(*p)]
    check(f"distinct tasks stay distinct (collapsed: {collapsed})", not collapsed)
    check("symmetric", all(titles_similar(b, a) for a, b in DUPLICATE_PAIRS))
    check("empty never matches", not titles_similar("", "Pay Jio bill") and not titles_similar("x", ""))
    check("two shared words are not containment",
          not titles_similar("Reply to Madhav", "Reply to Madhav re: Aster pilot proposal and NDA"))


def _gmail_result(title: str, **todo) -> dict:
    return {"should_generate_todo": True, "reasoning": "r", "todo": {"title": title, **todo}}


def check_save_helpers() -> None:
    print("\n-- save helpers --")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    uid, _ = upsert_user(conn, "dedup@example.com", "Dedup")
    other, _ = upsert_user(conn, "other@example.com", "Other")

    first = save_todo(conn, "ev1", "msg1", "thr1", _gmail_result("Confirm Zoho account"), uid, "a@x.com")
    check("first gmail todo saves", first is not None)
    second = save_todo(conn, "ev2", "msg2", "thr2", _gmail_result("Confirm Zoho account before closure"), uid, "b@x.com")
    check("reminder from another account with a new message id is a duplicate", second is None)
    check("the same title for another user still saves",
          save_todo(conn, "ev3", "msg3", "thr3", _gmail_result("Confirm Zoho account"), other) is not None)

    browser = save_browser_history_todo(conn, uid, {
        "title": "Confirm Zoho account", "relevant_link": "https://accounts.zoho.com/confirm/abc"})
    check("browser-history todo for the task the mail was about is a duplicate", browser is None)
    browser = save_browser_history_todo(conn, uid, {
        "title": "Review Pull Request #25 changes", "relevant_link": "https://github.com/o/r/pull/25"})
    check("an unrelated browser todo saves", browser is not None)

    check("a user-typed todo is never suppressed",
          save_user_todo(conn, uid, "Confirm Zoho account") is not None)
    check("a generated todo matching what the user typed is suppressed",
          save_todo(conn, "ev4", "msg4", "thr4", _gmail_result("Confirm Zoho account today"), uid) is None)

    sys_id = save_system_todo(conn, uid, {"title": "Clean up ~/Downloads installers"})
    check("system todo saves", sys_id is not None)
    check("system source keeps its looser ratio",
          save_system_todo(conn, uid, {"title": "Clean up ~/Downloads installer files"}) is None)

    # A closed or rejected todo still blocks regeneration inside the window.
    conn.execute("UPDATE todos SET status = 'closed', decision = 'rejected' WHERE todo_id = ?", (first,))
    conn.commit()
    check("a closed, rejected todo still blocks a lookalike",
          similar_recent_todo(conn, uid, "Confirm Zoho account", days=14) is not None)

    # Outside the window it does not.
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute("UPDATE todos SET created_at = ? WHERE user_id = ?", (old, uid))
    conn.commit()
    check("gmail window is 14 days: a 20-day-old lookalike no longer blocks",
          similar_recent_todo(conn, uid, "Confirm Zoho account", days=14) is None)
    check("browser window is 30 days: the same row still blocks there",
          similar_recent_todo(conn, uid, "Confirm Zoho account", days=30) is not None)

    titles = sorted(t["title"] for t in list_todos(conn, uid))
    check("exactly the expected rows exist",
          titles == sorted(["Confirm Zoho account", "Review Pull Request #25 changes",
                            "Confirm Zoho account", "Clean up ~/Downloads installers"]))
    conn.close()


if __name__ == "__main__":
    check_matcher()
    check_save_helpers()
    print("\nall checks passed")
