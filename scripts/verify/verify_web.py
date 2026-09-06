"""Verifies the multi-account web surface: settings API, per-account disconnect,
and the rendered todo list.

Runs against a throwaway database with synthetic connections, so it covers
everything except the Google OAuth round trip itself (which needs a real
browser sign-in). Usage: python scripts/verify/verify_web.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "web.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

import app as app_module
from db import init_db, upsert_user, set_source_credentials, save_todo


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    for addr, token in (("alice@example.com", "a"), ("bob@example.com", "b")):
        set_source_credentials(
            conn, user_id, "gmail", "oauth2", {"token": token}, account_id=addr
        )

    save_todo(conn, "e1", "m1", "th1",
              {"should_generate_todo": True, "reasoning": "r",
               "todo": {"title": "Reply about the Alice contract", "importance": "high"}},
              user_id, "alice@example.com")
    save_todo(conn, "e2", "m2", "th2",
              {"should_generate_todo": True, "reasoning": "r",
               "todo": {"title": "Send Bob the quarterly numbers", "importance": "low"}},
              user_id, "bob@example.com")
    # A todo predating per-account provenance.
    conn.execute(
        "INSERT INTO todos (todo_id, user_id, source, account_id, dedup_key, title, "
        "status, created_at, updated_at) VALUES "
        "('legacy','" + user_id + "','gmail',NULL,'m0','Legacy todo with no account',"
        "'open','2026-01-01','2026-01-01')"
    )
    conn.commit()
    conn.close()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"

    settings = client.get("/settings").get_json()
    accounts = settings["sources"]["gmail"]["accounts"]
    check("settings lists both accounts", len(accounts) == 2)
    check("settings carries addresses",
          {a["email"] for a in accounts} == {"alice@example.com", "bob@example.com"})

    html = client.get("/").get_data(as_text=True)
    check("alice badge rendered", 'title="alice@example.com"' in html)
    check("bob badge rendered", 'title="bob@example.com"' in html)
    check("badge shows local part only", ">alice<" in html and ">bob<" in html)
    check("alice link targets her mailbox",
          "https://mail.google.com/mail/u/alice@example.com/#all/th1" in html)
    check("bob link targets his mailbox",
          "https://mail.google.com/mail/u/bob@example.com/#all/th2" in html)
    check("no link pins profile index 0 for account todos",
          html.count("mail/u/0/") == 0)
    check("legacy todo renders without a badge",
          "Legacy todo with no account" in html)

    resp = client.post("/settings/sources/gmail",
                       json={"disconnect": True, "account_id": "alice@example.com"})
    body = resp.get_json()
    check("disconnect succeeds", body.get("ok") is True)
    check("only bob remains after scoped disconnect",
          [a["email"] for a in body["accounts"]] == ["bob@example.com"])

    after = client.get("/settings").get_json()["sources"]["gmail"]["accounts"]
    check("disconnect persisted", [a["email"] for a in after] == ["bob@example.com"])

    html2 = client.get("/").get_data(as_text=True)
    check("alice's todo survives her disconnect",
          "Reply about the Alice contract" in html2)

    print("\nAll web-surface checks passed.")


if __name__ == "__main__":
    main()
