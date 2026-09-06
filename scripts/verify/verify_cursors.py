"""Verifies per-account cursor key namespacing."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import (
    init_db, upsert_user, gmail_state_key,
    get_gmail_history_id, set_gmail_history_id,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    check("key is namespaced by account",
          gmail_state_key("a@x.com", "history_id") == "gmail:a@x.com:history_id")

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")

    set_gmail_history_id(conn, uid, "a@x.com", "100")
    set_gmail_history_id(conn, uid, "b@x.com", "200")

    check("account a cursor isolated", get_gmail_history_id(conn, uid, "a@x.com") == "100")
    check("account b cursor isolated", get_gmail_history_id(conn, uid, "b@x.com") == "200")
    check("unknown account has no cursor",
          get_gmail_history_id(conn, uid, "c@x.com") is None)

    set_gmail_history_id(conn, uid, "a@x.com", "150")
    check("advancing a leaves b alone",
          (get_gmail_history_id(conn, uid, "a@x.com"),
           get_gmail_history_id(conn, uid, "b@x.com")) == ("150", "200"))

    print("\nAll cursor checks passed.")


if __name__ == "__main__":
    main()
