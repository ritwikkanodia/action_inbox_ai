"""Verifies the Google scope set and the general credential helper.

Checks that both OAuth flows request the same Workspace-wide scope list, that
`has_agent_access` is the exact test Settings uses, and — the one that matters —
that `get_google_credentials` refreshes with the scopes *stored on the token*,
not the list the app now requests. google-auth raises on refresh when the
requested set exceeds the granted one, so getting this wrong would break polling
for every account connected before the scope change.

No network: `Credentials.refresh` is monkeypatched.

Usage: python scripts/verify/verify_google_scopes.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "scopes.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only-not-a-real-key")

import sqlite3

from google.oauth2.credentials import Credentials

import google_scopes
from db import init_db, set_source_credentials, upsert_user
import pollers.gmail.auth as gauth
import auth as login_auth


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def stored_creds(scopes: list[str], expired: bool) -> dict:
    expiry = datetime.now(timezone.utc) + timedelta(hours=-1 if expired else 1)
    return {
        "token": "old-token",
        "refresh_token": "refresh-token",
        "client_id": "verify-client-id",
        "client_secret": "verify-client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes,
        "expiry": expiry.replace(tzinfo=None).isoformat() + "Z",
    }


def check_scopes() -> None:
    print("\n-- scope module --")
    check("POLL_SCOPES is exactly gmail.readonly", google_scopes.POLL_SCOPES == [READONLY])
    for needle in ("gmail.modify", "auth/drive", "auth/documents", "auth/spreadsheets",
                   "calendar.events", "contacts.readonly", "contacts.other.readonly"):
        check(f"AGENT_SCOPES contains {needle}",
              any(needle in s for s in google_scopes.AGENT_SCOPES))
    check("ALL_SCOPES is poll + agent, no duplicates",
          google_scopes.ALL_SCOPES == google_scopes.POLL_SCOPES + google_scopes.AGENT_SCOPES
          and len(set(google_scopes.ALL_SCOPES)) == len(google_scopes.ALL_SCOPES))
    check("has_agent_access true for the full set",
          google_scopes.has_agent_access(google_scopes.ALL_SCOPES))
    check("has_agent_access false for readonly only",
          not google_scopes.has_agent_access([READONLY]))
    check("has_agent_access false when one agent scope is missing",
          not google_scopes.has_agent_access(google_scopes.ALL_SCOPES[:-1]))
    check("Gmail reconnect flow requests ALL_SCOPES", gauth.SCOPES == google_scopes.ALL_SCOPES)
    check("GMAIL_CONNECT_SCOPES accepts either gmail scope",
          {READONLY, "https://www.googleapis.com/auth/gmail.modify"}
          <= login_auth.GMAIL_CONNECT_SCOPES)
    check("Sign-in flow requests openid + ALL_SCOPES",
          login_auth.LOGIN_SCOPES[:3] == ["openid",
                                          "https://www.googleapis.com/auth/userinfo.email",
                                          "https://www.googleapis.com/auth/userinfo.profile"]
          and login_auth.LOGIN_SCOPES[3:] == google_scopes.ALL_SCOPES)
    check("OAUTHLIB_RELAX_TOKEN_SCOPE set by the scope module",
          os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE") == "1")


def check_credentials() -> None:
    print("\n-- get_google_credentials --")
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    init_db(conn)
    user_id, _ = upsert_user(conn, email="verify@example.com", name="V", picture_url=None)

    # An account connected before the scope change: readonly only, token expired.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds([READONLY], expired=True), account_id="old@example.com")
    # A second account with the full grant, token still fresh.
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds(google_scopes.ALL_SCOPES, expired=False),
                           account_id="new@example.com")

    seen_scopes: list = []

    def fake_refresh(self, request):
        seen_scopes.append(list(self.scopes or []))
        self.token = "fresh-token"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    real_refresh = Credentials.refresh
    Credentials.refresh = fake_refresh
    try:
        creds, granted, account = gauth.get_google_credentials(conn, user_id, "old@example.com")
        check("refresh ran for the expired account", seen_scopes == [[READONLY]])
        check("refresh used the STORED scopes, not the requested list",
              seen_scopes[0] == [READONLY])
        check("granted set reflects the stored scopes", granted == {READONLY})
        check("resolved account echoed back", account == "old@example.com")
        row = conn.execute(
            "SELECT credentials FROM source_connections WHERE account_id='old@example.com'"
        ).fetchone()
        check("refreshed token persisted", json.loads(row[0])["token"] == "fresh-token")

        creds, granted, account = gauth.get_google_credentials(conn, user_id, "new@example.com")
        check("fresh token not refreshed", len(seen_scopes) == 1)
        check("full grant reported", granted == set(google_scopes.ALL_SCOPES))

        _, _, account = gauth.get_google_credentials(conn, user_id, None)
        check("account_id=None resolves to the first connected account",
              account == "old@example.com")

        try:
            gauth.get_google_credentials(conn, user_id, "nobody@example.com")
            check("unknown account raises RuntimeError", False)
        except RuntimeError as exc:
            check("unknown account raises RuntimeError", "not connected" in str(exc))

        svc = gauth.get_gmail_service(conn, user_id, "new@example.com")
        check("get_gmail_service still builds a client", hasattr(svc, "users"))
    finally:
        Credentials.refresh = real_refresh
    conn.close()


def check_settings() -> None:
    print("\n-- /settings --")
    import app as app_module
    from db import init_db as _init
    client = app_module.app.test_client()
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    _init(conn)
    user_id, _ = upsert_user(conn, email="settings@example.com", name="S", picture_url=None)
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds([READONLY], expired=False), account_id="ro@example.com")
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           stored_creds(google_scopes.ALL_SCOPES, expired=False),
                           account_id="full@example.com")
    conn.close()

    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "settings@example.com"
    payload = client.get("/settings").get_json()
    by_email = {a["email"]: a for a in payload["sources"]["gmail"]["accounts"]}
    check("readonly account reports agent_access False", by_email["ro@example.com"]["agent_access"] is False)
    check("full account reports agent_access True", by_email["full@example.com"]["agent_access"] is True)
    check("grant_url_template ends with login_hint=",
          payload["sources"]["gmail"]["grant_url_template"].endswith("login_hint="))

    resp = client.get("/settings/sources/gmail/auth?login_hint=ro%40example.com")
    check("gmail auth redirects to Google", resp.status_code == 302 and "accounts.google.com" in resp.headers["Location"])
    check("login_hint forwarded to Google", "login_hint=ro%40example.com" in resp.headers["Location"])
    check("include_granted_scopes requested", "include_granted_scopes=true" in resp.headers["Location"])
    check("consent still requested", "prompt=select_account" in resp.headers["Location"])


if __name__ == "__main__":
    check_scopes()
    check_credentials()
    check_settings()
    print("\nAll checks passed.")
