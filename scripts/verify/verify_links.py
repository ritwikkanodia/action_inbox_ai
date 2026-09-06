"""Verifies Gmail deep-link construction and todo account provenance."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import init_db, upsert_user, gmail_thread_url, save_todo


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    url = gmail_thread_url("t123", "dev@example.com")
    check("link targets the account mailbox",
          url == "https://mail.google.com/mail/u/dev@example.com/#all/t123")
    check("link does not pin profile index 0", "/u/0/" not in url)
    check("link does not use the broken authuser form", "authuser" not in url)

    fallback = gmail_thread_url("t123", None)
    check("None account falls back to index 0",
          fallback == "https://mail.google.com/mail/u/0/#all/t123")
    check("empty account falls back to index 0",
          gmail_thread_url("t123", "") == fallback)

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    result = {
        "should_generate_todo": True,
        "reasoning": "needs a reply",
        "todo": {"title": "Reply to Acme", "importance": "high"},
    }
    saved = save_todo(conn, "evt1", "m1", "t1", result, user_id, "a@example.com")
    check("todo saved", saved)

    row = conn.execute(
        "SELECT account_id, relevant_link FROM todos WHERE dedup_key = 'm1'"
    ).fetchone()
    check("account_id persisted", row[0] == "a@example.com")
    check("relevant_link targets that account",
          row[1] == "https://mail.google.com/mail/u/a@example.com/#all/t1")

    result2 = {
        "should_generate_todo": True,
        "reasoning": "x",
        "todo": {"title": "Totally unrelated subject line here", "importance": "low"},
    }
    save_todo(conn, "evt2", "m2", "t2", result2, user_id, "")
    row2 = conn.execute(
        "SELECT account_id, relevant_link FROM todos WHERE dedup_key = 'm2'"
    ).fetchone()
    check("empty account stores NULL", row2[0] is None)
    check("empty account uses fallback link", "/u/0/" in row2[1])

    print("\nAll link and provenance checks passed.")


if __name__ == "__main__":
    main()
