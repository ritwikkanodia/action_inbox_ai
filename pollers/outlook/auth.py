import json
import os
import sqlite3

import msal

from db import clear_source_connection, get_source_connection, set_source_credentials

SCOPES = ["Mail.Read"]
_TENANT = "common"


def _make_app(cache: msal.SerializableTokenCache) -> msal.ConfidentialClientApplication:
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    tenant = os.environ.get("AZURE_TENANT_ID", _TENANT)
    if not client_id or not client_secret:
        raise RuntimeError("AZURE_CLIENT_ID and AZURE_CLIENT_SECRET must be set.")
    return msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )


def get_auth_flow(redirect_uri: str) -> dict:
    """Start OAuth flow. Returns the flow dict to store in the session."""
    cache = msal.SerializableTokenCache()
    app = _make_app(cache)
    return app.initiate_auth_code_flow(SCOPES, redirect_uri=redirect_uri)


def complete_auth_flow(auth_flow: dict, auth_response: dict) -> dict:
    """Exchange the authorization code. Returns credentials dict to persist."""
    cache = msal.SerializableTokenCache()
    app = _make_app(cache)
    result = app.acquire_token_by_auth_code_flow(auth_flow, auth_response)
    if "error" in result:
        raise RuntimeError(
            f"Outlook auth error: {result.get('error_description', result['error'])}"
        )
    account = result.get("account") or {}
    return {
        "token_cache": cache.serialize(),
        "account_home_id": account.get("home_account_id"),
        "connected_email": account.get("username"),
    }


def get_access_token(conn: sqlite3.Connection, user_id: str) -> str:
    """Return a valid access token, refreshing via MSAL if needed."""
    row = get_source_connection(conn, user_id, "outlook")
    if not row:
        raise RuntimeError("Outlook not connected. Visit settings to authorize.")

    cache = msal.SerializableTokenCache()
    cache.deserialize(row["credentials"]["token_cache"])
    app = _make_app(cache)

    accounts = app.get_accounts()
    if not accounts:
        clear_source_connection(conn, user_id, "outlook")
        raise RuntimeError("Outlook session expired. Reconnect in settings.")

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        clear_source_connection(conn, user_id, "outlook")
        raise RuntimeError("Outlook token refresh failed. Reconnect in settings.")

    if cache.has_state_changed:
        creds = dict(row["credentials"])
        creds["token_cache"] = cache.serialize()
        set_source_credentials(conn, user_id, "outlook", "oauth2", creds)

    return result["access_token"]
