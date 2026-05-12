import json
import os
import sqlite3

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from db import clear_source_connection, get_source_connection, set_source_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _client_config() -> dict:
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in the environment."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }
    }


def get_auth_flow(
    redirect_uri: str,
    *,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    return Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def get_gmail_service(conn: sqlite3.Connection, user_id: str):
    row = get_source_connection(conn, user_id, "gmail")
    if not row:
        raise RuntimeError(
            "Gmail not connected. Visit the settings page to authorize."
        )

    creds = Credentials.from_authorized_user_info(row["credentials"], SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Google has revoked the refresh token (Testing-mode 7-day expiry,
                # user revoked access, password change, etc.). Clear the stored
                # credentials so the UI flips to "not connected" and prompts re-auth.
                clear_source_connection(conn, user_id, "gmail")
                raise RuntimeError(
                    "Gmail access revoked by Google. Reconnect Gmail in settings."
                ) from e
            set_source_credentials(
                conn, user_id, "gmail", "oauth2", json.loads(creds.to_json())
            )
        else:
            clear_source_connection(conn, user_id, "gmail")
            raise RuntimeError(
                "Gmail credentials expired. Re-authorize via the settings page."
            )

    return build("gmail", "v1", credentials=creds)
