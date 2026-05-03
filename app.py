import json
import os
import sqlite3
from datetime import datetime, timezone
from agent import resolve_todo

from flask import Flask, g, render_template, request, jsonify, redirect, session, url_for

from db import (
    init_db,
    save_user_todo,
    get_source_connection,
    set_source_credentials,
    clear_source_connection,
)
from auth import (
    complete_login,
    current_user,
    current_user_id,
    login_required,
    start_login,
)
from googleapiclient.discovery import build as google_build
from pollers.gmail.auth import get_auth_flow

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")

# Allow OAuth over http only for local development.
if BASE_URL.startswith("http://localhost") or BASE_URL.startswith("http://127.0.0.1"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

GMAIL_REDIRECT_URI = f"{BASE_URL}/oauth/gmail/callback"
LOGIN_REDIRECT_URI = f"{BASE_URL}/oauth/login/callback"


@app.template_filter("fmt_dt")
def fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    except ValueError:
        return value[:16]


DB_PATH = os.environ.get("DB_PATH", "gmail_events.db")


def _ensure_db_parent_dir() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_db():
    if "db" not in g:
        _ensure_db_parent_dir()
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        init_db(g.db)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ---------------------------------------------------------------------------
# Login routes
# ---------------------------------------------------------------------------


@app.route("/login")
def login_page():
    if current_user_id():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/oauth/login/start")
def login_start():
    return start_login(LOGIN_REDIRECT_URI)


@app.route("/oauth/login/callback")
def login_callback():
    user_id, error = complete_login(LOGIN_REDIRECT_URI, get_db(), request.url)
    if error:
        return f"Login failed: {error}", 400
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def index():
    db = get_db()
    user_id = current_user_id()
    todos = db.execute(
        """
        SELECT todo_id, title, suggested_action, urgency,
               estimated_time_minutes, due_date, relevant_link, reasoning, status, source, decision, created_at
        FROM todos
        WHERE user_id = ? AND title IS NOT NULL AND title != ''
        ORDER BY
            CASE status WHEN 'closed' THEN 1 ELSE 0 END,
            CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            created_at DESC
        """,
        (user_id,),
    ).fetchall()
    return render_template("index.html", todos=todos, user=current_user())


@app.route("/todos", methods=["POST"])
@login_required
def create_todo():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    urgency = data.get("urgency", "medium")
    if urgency not in ("low", "medium", "high"):
        urgency = "medium"
    due_date = data.get("due_date") or None
    suggested_action = (data.get("suggested_action") or "").strip()
    db = get_db()
    user_id = current_user_id()
    assert user_id
    todo_id = save_user_todo(db, user_id, title, urgency, due_date, suggested_action)
    return jsonify({"ok": True, "todo_id": todo_id}), 201


@app.route("/todos/<todo_id>/ask-ai", methods=["POST"])
@login_required
def ask_ai(todo_id):
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = db.execute(
        "SELECT title, suggested_action, reasoning, urgency, due_date, source, ai_thread, source_meta "
        "FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    user_message = (data.get("message") or "").strip()

    # Load persisted thread
    thread = []
    if row["ai_thread"]:
        try:
            thread = json.loads(row["ai_thread"])
        except Exception:
            thread = []

    # If thread already exists and no new message, return it without an LLM call
    if thread and not user_message:
        return jsonify({"thread": thread})

    thread = resolve_todo(dict(row), thread, user_message)

    db.execute(
        "UPDATE todos SET ai_thread = ?, updated_at = ? WHERE todo_id = ? AND user_id = ?",
        (json.dumps(thread), datetime.now(timezone.utc).isoformat(), todo_id, user_id),
    )
    db.commit()

    return jsonify({"thread": thread})


@app.route("/todos/<todo_id>/reset-thread", methods=["POST"])
@login_required
def reset_thread(todo_id):
    db = get_db()
    user_id = current_user_id()
    assert user_id
    db.execute(
        "UPDATE todos SET ai_thread = NULL, updated_at = ? WHERE todo_id = ? AND user_id = ?",
        (datetime.now(timezone.utc).isoformat(), todo_id, user_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/todos/<todo_id>", methods=["PATCH"])
@login_required
def update_todo(todo_id):
    ALLOWED = {"due_date", "urgency", "status", "decision", "title"}
    data = request.get_json(force=True)
    updates = {k: v for k, v in data.items() if k in ALLOWED}
    if not updates:
        return jsonify({"error": "no valid fields"}), 400
    sets = ", ".join(f"{k} = ?" for k in updates) + ", updated_at = ?"
    db = get_db()
    user_id = current_user_id()
    assert user_id
    db.execute(
        f"UPDATE todos SET {sets} WHERE todo_id = ? AND user_id = ?",
        (*updates.values(), datetime.now(timezone.utc).isoformat(), todo_id, user_id),
    )
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.route("/settings", methods=["GET"])
@login_required
def get_settings():
    db = get_db()
    user_id = current_user_id()
    assert user_id
    fathom = get_source_connection(db, user_id, "fathom")
    fathom_key = (fathom or {}).get("credentials", {}).get("api_key", "") if fathom else None
    gmail = get_source_connection(db, user_id, "gmail")
    gmail_email = (gmail or {}).get("credentials", {}).get("connected_email") if gmail else None
    return jsonify({
        "sources": {
            "fathom": {
                "connected": bool(fathom),
                "api_key_preview": f"...{fathom_key[-6:]}" if fathom_key else None,
            },
            "gmail": {
                "connected": bool(gmail),
                "email": gmail_email,
                "auth_url": url_for("gmail_auth"),
            },
        }
    })


@app.route("/settings/sources/gmail/auth")
@login_required
def gmail_auth():
    flow = get_auth_flow(GMAIL_REDIRECT_URI)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    session["gmail_oauth_state"] = state
    session["gmail_oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/oauth/gmail/callback")
@login_required
def gmail_callback():
    oauth_state = session.get("gmail_oauth_state")
    code_verifier = session.get("gmail_oauth_code_verifier")
    if not oauth_state or not code_verifier:
        return (
            "Gmail OAuth session expired. Start the Gmail connection flow again.",
            400,
        )

    flow = get_auth_flow(
        GMAIL_REDIRECT_URI,
        state=oauth_state,
        code_verifier=code_verifier,
    )
    flow.fetch_token(
        authorization_response=request.url,
    )
    creds = flow.credentials
    db = get_db()
    creds_dict = json.loads(creds.to_json())
    try:
        gmail_svc = google_build("gmail", "v1", credentials=creds)
        profile = gmail_svc.users().getProfile(userId="me").execute()
        creds_dict["connected_email"] = profile.get("emailAddress")
    except Exception:
        pass
    user_id = current_user_id()
    assert user_id
    set_source_credentials(db, user_id, "gmail", "oauth2", creds_dict)
    session.pop("gmail_oauth_state", None)
    session.pop("gmail_oauth_code_verifier", None)
    return redirect(url_for("index"))


@app.route("/settings/sources/<source>", methods=["POST"])
@login_required
def update_source_settings(source: str):
    ALLOWED_SOURCES = {"fathom", "gmail"}
    if source not in ALLOWED_SOURCES:
        return jsonify({"error": "unknown source"}), 400
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    user_id = current_user_id()
    assert user_id
    if source == "fathom":
        if data.get("disconnect"):
            clear_source_connection(db, user_id, "fathom")
            return jsonify({"ok": True, "connected": False})
        api_key = (data.get("api_key") or "").strip()
        if not api_key:
            return jsonify({"error": "api_key required"}), 400
        set_source_credentials(db, user_id, "fathom", "api_key", {"api_key": api_key})
        return jsonify({"ok": True, "connected": True, "api_key_preview": f"...{api_key[-6:]}"})
    if source == "gmail":
        if data.get("disconnect"):
            clear_source_connection(db, user_id, "gmail")
            return jsonify({"ok": True, "connected": False})
        return jsonify({"error": "use /settings/sources/gmail/auth to connect"}), 400
    return jsonify({"error": "unhandled"}), 500


if __name__ == "__main__":
    app.run(debug=True)
