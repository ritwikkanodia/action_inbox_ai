import json
import os
import sqlite3

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from db import clear_source_connection, get_source_connection, set_source_credentials
from google_scopes import ALL_SCOPES

SCOPES = ALL_SCOPES


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


def get_google_credentials(
    conn: sqlite3.Connection, user_id: str, account_id: str | None = None
) -> tuple[Credentials, set[str], str]:
    """Valid credentials for one connected Google account.

    Returns (credentials, granted scopes, resolved account id). account_id=None
    resolves to the user's first connected account, which is the fallback for
    todos created before per-account provenance existed.

    Refresh is done against the scopes *stored on the token*, not `SCOPES`:
    google-auth raises RefreshError when the requested set is not a subset of
    what Google returns, and every account connected before the Workspace scopes
    were added carries only gmail.readonly. The granted set is returned so callers
    can gate on it instead of guessing.
    """
    row = get_source_connection(conn, user_id, "gmail", account_id)
    if not row:
        label = account_id or "any account"
        raise RuntimeError(
            f"Gmail not connected ({label}). Visit the settings page to authorize."
        )

    # Resolve the concrete account so revocation clears only this row, never
    # every account the user has connected.
    resolved = row["account_id"]
    info = row["credentials"]
    stored_scopes = list(info.get("scopes") or [])
    creds = Credentials.from_authorized_user_info(info, stored_scopes)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Google has revoked the refresh token (Testing-mode 7-day expiry,
                # user revoked access, password change, etc.). Clear the stored
                # credentials for THIS account so the UI flips to "not connected"
                # and prompts re-auth, leaving the user's other accounts polling.
                clear_source_connection(conn, user_id, "gmail", resolved)
                raise RuntimeError(
                    f"Gmail access revoked by Google for {resolved or 'this account'}. "
                    "Reconnect it in settings."
                ) from e
            refreshed = json.loads(creds.to_json())
            # to_json drops keys it doesn't own; keep ours (connected_email).
            refreshed = {**info, **refreshed}
            set_source_credentials(
                conn, user_id, "gmail", "oauth2", refreshed, account_id=resolved,
            )
        else:
            clear_source_connection(conn, user_id, "gmail", resolved)
            raise RuntimeError(
                f"Gmail credentials expired for {resolved or 'this account'}. "
                "Re-authorize via the settings page."
            )

    return creds, set(stored_scopes), resolved


def get_gmail_service(
    conn: sqlite3.Connection, user_id: str, account_id: str | None = None
):
    """Build a Gmail client for one connected account (see get_google_credentials)."""
    creds, _, _ = get_google_credentials(conn, user_id, account_id)
    return build("gmail", "v1", credentials=creds)
