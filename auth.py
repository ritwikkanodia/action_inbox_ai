"""Google Sign-In login flow + Flask helpers.

This is intentionally kept separate from the per-source Gmail OAuth
authorization (`pollers/gmail/auth.py`). Login here only requests the
minimum scopes needed to identify the user; data-source authorization
happens later inside the Settings modal.
"""

import os
from functools import wraps
from typing import Callable

from flask import jsonify, redirect, request, session, url_for
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as id_token_lib
from google_auth_oauthlib.flow import Flow

from db import upsert_user

LOGIN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# Google sometimes echoes scopes back in a different order/form; the strict
# default makes oauthlib raise. Relaxing it is standard for openid logins.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


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


def _client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def _login_flow(
    redirect_uri: str,
    *,
    state: str | None = None,
    code_verifier: str | None = None,
) -> Flow:
    return Flow.from_client_config(
        _client_config(),
        scopes=LOGIN_SCOPES,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )


def start_login(redirect_uri: str):
    flow = _login_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="online",
        prompt="select_account",
    )
    session["login_oauth_state"] = state
    session["login_oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


def complete_login(
    redirect_uri: str,
    db,
    authorization_response_url: str,
) -> tuple[str | None, str | None]:
    """Exchange the OAuth callback for an ID token and upsert the user.

    Returns (user_id, error_message). Exactly one is non-None.
    """
    state = session.get("login_oauth_state")
    code_verifier = session.get("login_oauth_code_verifier")
    if not state or not code_verifier:
        return None, "Login session expired. Please start again."

    flow = _login_flow(redirect_uri, state=state, code_verifier=code_verifier)
    flow.fetch_token(authorization_response=authorization_response_url)

    raw_id_token = getattr(flow.credentials, "id_token", None)
    if not raw_id_token:
        # Fallback: pull it off the underlying oauth2 session token dict
        token = getattr(flow.oauth2session, "token", {}) or {}
        raw_id_token = token.get("id_token")
    if not raw_id_token:
        return None, "Google did not return an ID token."

    try:
        info = id_token_lib.verify_oauth2_token(
            raw_id_token, google_requests.Request(), _client_id()
        )
    except ValueError as exc:
        return None, f"Invalid ID token: {exc}"

    email = info.get("email")
    if not email:
        return None, "Google did not return an email address."

    user_id = upsert_user(
        db,
        email=email,
        name=info.get("name"),
        picture_url=info.get("picture"),
    )

    session.pop("login_oauth_state", None)
    session.pop("login_oauth_code_verifier", None)
    session["user_id"] = user_id
    session["user_email"] = email
    session["user_name"] = info.get("name")
    session["user_picture"] = info.get("picture")
    return user_id, None


def current_user_id() -> str | None:
    return session.get("user_id")


def current_user() -> dict | None:
    uid = session.get("user_id")
    if not uid:
        return None
    return {
        "user_id": uid,
        "email": session.get("user_email"),
        "name": session.get("user_name"),
        "picture_url": session.get("user_picture"),
    }


def _wants_json_response() -> bool:
    # Non-GET requests are always API-shaped (PATCH/POST from fetch()).
    if request.method != "GET":
        return True
    # GET endpoints called by fetch() in the SPA — currently only /settings.
    # /settings/sources/gmail/auth is a redirect flow, NOT JSON.
    if request.path == "/settings":
        return True
    return False


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("user_id"):
            return view(*args, **kwargs)
        if _wants_json_response():
            return jsonify({"error": "not authenticated"}), 401
        return redirect(url_for("login_page"))

    return wrapped
