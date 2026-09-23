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

# app.py's load_dotenv(override=True) has run by now; the developer's .env may
# opt into the macOS sources, so pin the default set for the "extra" checks.
os.environ["ENABLED_SOURCES"] = "gmail,fathom,morning_digest"
from db import init_db, upsert_user, set_source_credentials, save_todo, is_source_enabled


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

    page = client.get("/settings")
    check("settings is a page", page.status_code == 200
          and 'id="settings-view"' in page.get_data(as_text=True))
    check("settings page opens on the settings view",
          'window.__INITIAL_VIEW = "settings"' in page.get_data(as_text=True))
    check("inbox renders the settings view hidden",
          'id="settings-view" aria-labelledby="settings-heading" hidden' in client.get("/").get_data(as_text=True))

    settings = client.get("/settings.json").get_json()
    accounts = settings["sources"]["gmail"]["accounts"]
    check("sources start enabled",
          settings["sources"]["gmail"]["enabled"] is True and settings["sources"]["fathom"]["enabled"] is True)
    check("opt-in sources absent by default", settings["sources"]["extra"] == [])

    resp = client.post("/settings/sources/gmail/enabled", json={"enabled": False})
    check("pausing gmail succeeds", resp.get_json() == {"ok": True, "source": "gmail", "enabled": False})
    check("pause persisted",
          client.get("/settings.json").get_json()["sources"]["gmail"]["enabled"] is False)
    check("pause leaves accounts connected",
          len(client.get("/settings.json").get_json()["sources"]["gmail"]["accounts"]) == 2)
    check("poller-side helper agrees",
          is_source_enabled(sqlite3.connect(os.environ["DB_PATH"]), user_id, "gmail") is False)
    check("resume works",
          client.post("/settings/sources/gmail/enabled", json={"enabled": True}).get_json()["enabled"] is True)
    check("unknown source rejected",
          client.post("/settings/sources/morning_digest/enabled", json={"enabled": False}).status_code == 400)
    check("non-boolean rejected",
          client.post("/settings/sources/fathom/enabled", json={"enabled": "no"}).status_code == 400)

    os.environ["ENABLED_SOURCES"] = "gmail,fathom,browser_history"
    extra = client.get("/settings.json").get_json()["sources"]["extra"]
    check("server-enabled opt-in source is listed",
          [e["name"] for e in extra] == ["browser_history"] and extra[0]["enabled"] is True)
    os.environ["ENABLED_SOURCES"] = "gmail,fathom,morning_digest"
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

    after = client.get("/settings.json").get_json()["sources"]["gmail"]["accounts"]
    check("disconnect persisted", [a["email"] for a in after] == ["bob@example.com"])

    html2 = client.get("/").get_data(as_text=True)
    check("alice's todo survives her disconnect",
          "Reply about the Alice contract" in html2)

    print("\n-- todo routes share the db helpers --")
    created = client.post("/todos", json={"title": "Typed in the UI", "importance": "high"})
    body = created.get_json()
    check("create returns the row through get_todo",
          created.status_code == 201 and body["todo"]["title"] == "Typed in the UI"
          and body["todo"]["source_meta"] == {} and body["todo"]["has_ai_thread"] == 0)
    tid = body["todo_id"]
    check("PATCH rejects an enum outside the CHECK set with 400",
          client.patch(f"/todos/{tid}", json={"status": "done"}).status_code == 400)
    check("PATCH with no editable field is 400",
          client.patch(f"/todos/{tid}", json={"ai_thread": "x"}).status_code == 400)
    check("PATCH of an unknown id is 404",
          client.patch("/todos/nope", json={"status": "closed"}).status_code == 404)
    check("PATCH closes the todo",
          client.patch(f"/todos/{tid}", json={"status": "closed"}).get_json().get("ok") is True
          and 'Typed in the UI' in client.get("/").get_data(as_text=True))

    print("\nAll web-surface checks passed.")


if __name__ == "__main__":
    main()
