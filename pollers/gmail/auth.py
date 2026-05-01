import json
import sqlite3

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from db import get_source_connection, set_source_credentials

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = "credentials.json"


def get_auth_flow(
    redirect_uri: str,
    *,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    return Flow.from_client_secrets_file(
        CREDENTIALS_FILE,
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def get_gmail_service(conn: sqlite3.Connection):
    row = get_source_connection(conn, "gmail")
    if not row:
        raise RuntimeError(
            "Gmail not connected. Visit the settings page to authorize."
        )

    creds = Credentials.from_authorized_user_info(row["credentials"], SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            set_source_credentials(conn, "gmail", "oauth2", json.loads(creds.to_json()))
        else:
            raise RuntimeError(
                "Gmail credentials expired. Re-authorize via the settings page."
            )

    return build("gmail", "v1", credentials=creds)
