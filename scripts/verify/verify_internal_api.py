"""Verifies the /internal/* routes the agent's tools call in the cloud.

Bearer-token only: no token, a bogus token and an expired token are all 401;
a good one scopes every route to its user. The credentials route hands out an
access token alone (never the refresh token), and refreshes first when the
stored token is within 15 minutes of expiry — the Google refresh is stubbed.
The todo routes mirror the user-facing ones: same ordering, same 400/404.
No network, no spend.

Usage: python scripts/verify/verify_internal_api.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "internal.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import app as app_module  # noqa: E402
import google_scopes  # noqa: E402
from db import (  # noqa: E402
    get_todo, init_db, mint_run_token, save_user_todo, set_source_credentials, upsert_user,
)
from pollers.gmail import auth as gmail_auth  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _creds(expires_in_minutes: int, scopes) -> dict:
    return {
        "token": "stored-access-token",
        "refresh_token": "the-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "verify-client-id",
        "client_secret": "verify-client-secret",
        "scopes": list(scopes),
        "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=expires_in_minutes)),
        "connected_email": "alice@example.com",
    }


refreshes: list[str] = []


def fake_refresh(self, request):
    refreshes.append(self.token)
    self.token = "fresh-access-token"
    self.expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    init_db(conn)
    alice, _ = upsert_user(conn, "alice@example.com")
    bob, _ = upsert_user(conn, "bob@example.com")
    set_source_credentials(conn, alice, "gmail", "oauth2",
                           _creds(5, google_scopes.ALL_SCOPES), account_id="alice@example.com")
    set_source_credentials(conn, alice, "gmail", "oauth2",
                           _creds(60, ["https://www.googleapis.com/auth/gmail.readonly"]),
                           account_id="old@example.com")
    set_source_credentials(conn, bob, "gmail", "oauth2",
                           _creds(60, google_scopes.ALL_SCOPES), account_id="bob@example.com")
    bobs_todo = save_user_todo(conn, bob, "bob's private todo", "high")
    token = mint_run_token(conn, alice, "todo_x", 60)
    expired = mint_run_token(conn, alice, None, -1)
    conn.close()

    gmail_auth.Credentials.refresh = fake_refresh
    client = app_module.app.test_client()

    def hdr(t=token):
        return {"Authorization": f"Bearer {t}"}

    print("-- auth --")
    check("no token is 401", client.get("/internal/todos").status_code == 401)
    check("bogus token is 401", client.get("/internal/todos", headers=hdr("nope")).status_code == 401)
    check("expired token is 401", client.get("/internal/todos", headers=hdr(expired)).status_code == 401)
    check("wrong scheme is 401",
          client.get("/internal/todos", headers={"Authorization": f"Basic {token}"}).status_code == 401)
    check("a browser session is not enough",
          client.get("/internal/accounts").status_code == 401)

    print("\n-- accounts --")
    r = client.get("/internal/accounts", headers=hdr())
    check("accounts lists the token's user's accounts only",
          r.status_code == 200 and r.get_json()["accounts"] == ["alice@example.com", "old@example.com"])

    print("\n-- credentials --")
    r = client.get("/internal/credentials?account=alice@example.com", headers=hdr())
    body = r.get_json()
    check("credentials is 200", r.status_code == 200)
    check("near-expiry token was refreshed first",
          refreshes == ["stored-access-token"] and body["token"] == "fresh-access-token")
    check("no refresh token or client secret in the response",
          "refresh_token" not in body and "client_secret" not in body)
    check("scopes and account come back",
          body["account"] == "alice@example.com" and set(body["scopes"]) == set(google_scopes.ALL_SCOPES))
    check("expiry is an ISO string", body["expiry"].endswith("Z"))
    conn = sqlite3.connect(os.environ["DB_PATH"]); conn.row_factory = sqlite3.Row
    from db import get_source_connection
    stored = get_source_connection(conn, alice, "gmail", "alice@example.com")["credentials"]
    check("the refreshed token was persisted", stored["token"] == "fresh-access-token")
    conn.close()
    r = client.get("/internal/credentials?account=old@example.com", headers=hdr())
    check("a token with plenty of life is not refreshed",
          r.status_code == 200 and r.get_json()["token"] == "stored-access-token" and len(refreshes) == 1)
    r = client.get("/internal/credentials", headers=hdr())
    check("no account resolves to the first connected", r.get_json()["account"] == "alice@example.com")
    r = client.get("/internal/credentials?account=bob@example.com", headers=hdr())
    check("another user's account is a 409 with a message",
          r.status_code == 409 and "not connected" in r.get_json()["error"].lower())

    print("\n-- todos --")
    r = client.post("/internal/todos", json={"title": "chase the deposit", "importance": "high",
                                             "due_date": "2026-10-02"}, headers=hdr())
    check("create is 201 with the row", r.status_code == 201 and r.get_json()["title"] == "chase the deposit")
    created = r.get_json()["todo_id"]
    check("create landed as a user todo",
          r.get_json()["source"] == "user" and r.get_json()["decision"] == "accepted")
    check("create with a blank title is 400",
          client.post("/internal/todos", json={"title": " "}, headers=hdr()).status_code == 400)
    check("create with a bad importance is 400",
          client.post("/internal/todos", json={"title": "x", "importance": "urgent"},
                      headers=hdr()).status_code == 400)
    r = client.get("/internal/todos", headers=hdr())
    ids = [t["todo_id"] for t in r.get_json()]
    check("list shows alice's todo and never bob's", created in ids and bobs_todo not in ids)
    check("list filters by status",
          client.get("/internal/todos?status=closed", headers=hdr()).get_json() == [])
    check("get returns the row",
          client.get(f"/internal/todos/{created}", headers=hdr()).get_json()["todo_id"] == created)
    check("get of bob's todo is 404",
          client.get(f"/internal/todos/{bobs_todo}", headers=hdr()).status_code == 404)
    check("patch with a bad enum is 400",
          client.patch(f"/internal/todos/{created}", json={"status": "done"},
                       headers=hdr()).status_code == 400)
    check("patch with only non-editable fields is 400",
          client.patch(f"/internal/todos/{created}", json={"ai_thread": "x"},
                       headers=hdr()).status_code == 400)
    r = client.patch(f"/internal/todos/{created}", json={"status": "closed", "ai_thread": "x"},
                     headers=hdr())
    check("patch returns the row after the change",
          r.status_code == 200 and r.get_json()["status"] == "closed")
    check("patch of bob's todo is 404 and leaves it alone",
          client.patch(f"/internal/todos/{bobs_todo}", json={"status": "closed"},
                       headers=hdr()).status_code == 404)
    conn = sqlite3.connect(os.environ["DB_PATH"]); conn.row_factory = sqlite3.Row
    check("bob's todo untouched", get_todo(conn, bob, bobs_todo)["status"] == "open")
    check("non-editable field was ignored",
          conn.execute("SELECT ai_thread FROM todos WHERE todo_id = ?", (created,)).fetchone()[0] is None)
    conn.close()

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
