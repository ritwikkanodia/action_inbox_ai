import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

from agent import resolve_todo

from flask import (
    Flask,
    g,
    render_template,
    request,
    jsonify,
    redirect,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from db import (
    init_db,
    save_user_todo,
    get_source_connection,
    set_source_credentials,
    clear_source_connection,
    get_user_state,
    set_user_state,
    clear_user_state,
    get_user_by_id,
    record_page_view,
    get_user_view_summary,
    list_gmail_accounts,
    gmail_state_key,
    gmail_thread_url,
)
from pollers.gmail.poller import BACKFILL_PENDING_SUFFIX, BACKFILLED_SUFFIX
from pollers.digest import poller as digest_poller
from auth import (
    complete_login,
    current_user,
    current_user_id,
    login_required,
    start_login,
)
from googleapiclient.discovery import build as google_build
from pollers.gmail.auth import get_auth_flow, get_gmail_service
from pollers.gmail.thread_context import fetch_thread_messages

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")

# Allow OAuth over http only for local development.
if BASE_URL.startswith("http://localhost") or BASE_URL.startswith("http://127.0.0.1"):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
# Keep users signed in across browser restarts. Sessions are marked permanent
# at login (see auth.complete_login); this caps their lifetime.
app.permanent_session_lifetime = timedelta(days=30)

# Redirect URIs are computed per-request to match the hostname the browser
# used. This avoids cookie / PKCE state mismatches when the host differs
# (e.g. 127.0.0.1 vs localhost).


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


def public_request_url() -> str:
    # Use the actual incoming request URL so callback handling matches
    # the hostname the browser used (avoids cookie / state mismatch).
    return request.url


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


_TRACKED_PATHS = {"/", "/settings"}


@app.before_request
def track_page_view():
    if request.method != "GET":
        return
    if request.path not in _TRACKED_PATHS:
        return
    user_id = current_user_id()
    try:
        record_page_view(get_db(), user_id, request.path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PWA (installable web app)
# ---------------------------------------------------------------------------
#
# The manifest and service worker are public — no @login_required. Chrome
# fetches both before the user has a session, and a redirect to /login would
# make the app non-installable.


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(
        app.static_folder,
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@app.route("/sw.js")
def service_worker():
    # Served from the root so the worker's scope covers the whole app; a
    # worker under /static/ could only control /static/.
    response = send_from_directory(
        os.path.join(app.static_folder, "js"),
        "sw.js",
        mimetype="text/javascript",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.route("/offline")
def offline():
    return render_template("offline.html")


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
    redirect_uri = request.url_root.rstrip("/") + "/oauth/login/callback"
    return start_login(redirect_uri)


@app.route("/oauth/login/callback")
def login_callback():
    redirect_uri = request.url_root.rstrip("/") + "/oauth/login/callback"
    user_id, error = complete_login(redirect_uri, get_db(), request.url)
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
    rows = db.execute(
        """
        SELECT todo_id, title, suggested_action, importance,
               estimated_time_minutes, due_date, relevant_link, reasoning, status, source, decision, created_at, source_meta,
               (ai_thread IS NOT NULL AND ai_thread != '' AND ai_thread != '[]') AS has_ai_thread
        FROM todos
        WHERE user_id = ? AND title IS NOT NULL AND title != ''
        ORDER BY
            CASE status WHEN 'closed' THEN 1 ELSE 0 END,
            CASE importance WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            created_at DESC
        """,
        (user_id,),
    ).fetchall()
    todos = [dict(r) for r in rows]
    for t in todos:
        meta_raw = t.get("source_meta")
        if meta_raw:
            try:
                t["source_meta"] = json.loads(meta_raw)
            except Exception:
                t["source_meta"] = {}
        else:
            t["source_meta"] = {}
    gmail_connected = bool(get_source_connection(db, user_id, "gmail"))
    fresh_signup = bool(session.pop("fresh_signup", False))
    return render_template(
        "index.html",
        todos=todos,
        todos_json=json.dumps(todos).replace("</", "<\\/"),
        user=current_user(),
        gmail_connected=gmail_connected,
        gmail_auth_url=url_for("gmail_auth"),
        fresh_signup=fresh_signup,
    )


@app.route("/todos", methods=["POST"])
@login_required
def create_todo():
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    importance = data.get("importance", "medium")
    if importance not in ("low", "medium", "high"):
        importance = "medium"
    due_date = data.get("due_date") or None
    suggested_action = (data.get("suggested_action") or "").strip()
    db = get_db()
    user_id = current_user_id()
    assert user_id
    todo_id = save_user_todo(db, user_id, title, importance, due_date, suggested_action)
    row = db.execute(
        """
        SELECT todo_id, title, suggested_action, importance,
               estimated_time_minutes, due_date, relevant_link, reasoning, status,
               source, decision, created_at,
               (ai_thread IS NOT NULL AND ai_thread != '' AND ai_thread != '[]') AS has_ai_thread
        FROM todos WHERE todo_id = ? AND user_id = ?
        """,
        (todo_id, user_id),
    ).fetchone()
    return jsonify({"ok": True, "todo_id": todo_id, "todo": dict(row) if row else None}), 201


def _extract_text(content) -> str:
    """Flatten an SDK message 'content' field to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                # output_text, input_text, refusal, etc.
                text = item.get("text") or item.get("refusal") or ""
                if text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


def _thread_for_client(thread):
    """Filter an SDK input list down to renderable {role, content} bubbles."""
    from agent.input_builder import HIDDEN_CONTEXT_SENTINEL

    out = []
    for item in thread or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(item.get("content"))
        if not text:
            continue
        if role == "user" and text.startswith(HIDDEN_CONTEXT_SENTINEL):
            continue
        out.append({"role": role, "content": text})
    return out


@app.route("/todos/<todo_id>/ask-ai", methods=["POST"])
@login_required
def ask_ai(todo_id):
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = db.execute(
        "SELECT title, suggested_action, reasoning, importance, due_date, source, ai_thread, source_meta "
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
        return jsonify({"thread": _thread_for_client(thread)})

    thread = resolve_todo(dict(row), thread, user_message, user_id)

    db.execute(
        "UPDATE todos SET ai_thread = ?, updated_at = ? WHERE todo_id = ? AND user_id = ?",
        (json.dumps(thread), datetime.now(timezone.utc).isoformat(), todo_id, user_id),
    )
    db.commit()

    return jsonify({"thread": _thread_for_client(thread)})


@app.route("/todos/<todo_id>/context", methods=["GET"])
@login_required
def todo_context(todo_id):
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = db.execute(
        "SELECT source, source_meta, relevant_link FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404

    source = row["source"]
    try:
        meta = json.loads(row["source_meta"]) if row["source_meta"] else {}
    except Exception:
        meta = {}

    if source == "gmail":
        thread_id = meta.get("thread_id")
        if not thread_id:
            return jsonify({"source": source, "error": "No thread linked to this todo."})
        try:
            service = get_gmail_service(db, user_id)
            user_email = service.users().getProfile(userId="me").execute().get("emailAddress", "")
            messages = fetch_thread_messages(service, thread_id)
            formatted = [
                {
                    "from_name": m["from_name"],
                    "from_email": m["from_email"],
                    "received_at": m["received_at"],
                    "body_text": m["body_text"],
                    "is_user": bool(user_email) and user_email.lower() in (m["from_email"] or "").lower(),
                }
                for m in messages
            ]
            return jsonify({
                "source": "gmail",
                "thread": formatted,
                "thread_url": row["relevant_link"] or f"https://mail.google.com/mail/u/0/#all/{thread_id}",
            })
        except Exception as exc:
            return jsonify({"source": "gmail", "error": f"Couldn't load thread: {exc}"})

    if source == "fathom":
        return jsonify({
            "source": "fathom",
            "meeting_title": meta.get("meeting_title"),
            "assignee": meta.get("assignee"),
            "recording_url": row["relevant_link"],
        })

    if source == "browser_history":
        return jsonify({
            "source": "browser_history",
            "page_url": meta.get("page_url") or row["relevant_link"],
            "page_title": meta.get("page_title"),
        })

    return jsonify({"source": source})


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
    ALLOWED = {"due_date", "importance", "status", "decision", "title"}
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
# Digest preview (renders the same email body without sending)
# ---------------------------------------------------------------------------


@app.route("/digest/preview", methods=["GET"])
def digest_preview():
    db = get_db()
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    user = get_user_by_id(db, user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404
    now_local = digest_poller._now_local()
    buckets = digest_poller._fetch_buckets(db, user["user_id"], now_local)
    subject, html, text = digest_poller._render(user, buckets, BASE_URL)
    if request.args.get("format") == "json":
        return jsonify({
            "subject": subject,
            "buckets": buckets,
            "html": html,
            "text": text,
        })
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


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
    redirect_uri = request.url_root.rstrip("/") + "/oauth/gmail/callback"
    flow = get_auth_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        # `select_account` is required for multi-account: with `consent` alone
        # Google silently reuses the already signed-in account, making it
        # impossible to add a second mailbox from the browser.
        prompt="select_account consent",
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

    redirect_uri = request.url_root.rstrip("/") + "/oauth/gmail/callback"
    flow = get_auth_flow(
        redirect_uri,
        state=oauth_state,
        code_verifier=code_verifier,
    )
    flow.fetch_token(
        authorization_response=request.url,
    )
    creds = flow.credentials
    db = get_db()
    creds_dict = json.loads(creds.to_json())

    # The address is mandatory now: it is the connection's primary key, the
    # value the deep link needs, and the namespace for the poll cursor. A row
    # without one cannot be keyed, linked, or polled, so fail loudly rather
    # than storing an unusable connection.
    try:
        gmail_svc = google_build("gmail", "v1", credentials=creds)
        profile = gmail_svc.users().getProfile(userId="me").execute()
        connected_email = (profile.get("emailAddress") or "").strip().lower()
    except Exception:
        connected_email = ""
    if not connected_email:
        session.pop("gmail_oauth_state", None)
        session.pop("gmail_oauth_code_verifier", None)
        return (
            "Couldn't read the Gmail address for that account, so it wasn't "
            "connected. This is usually temporary — please try again.",
            502,
        )

    creds_dict["connected_email"] = connected_email
    user_id = current_user_id()
    assert user_id
    set_source_credentials(
        db, user_id, "gmail", "oauth2", creds_dict, account_id=connected_email
    )

    backfilled_key = gmail_state_key(connected_email, BACKFILLED_SUFFIX)
    if get_user_state(db, user_id, backfilled_key):
        print(f"[gmail] reconnect of {connected_email} — skipping backfill")
    else:
        print(f"[gmail] new account {connected_email} — scheduling backfill")
        set_user_state(
            db, user_id,
            gmail_state_key(connected_email, BACKFILL_PENDING_SUFFIX),
            connected_email,
        )
        clear_user_state(db, user_id, gmail_state_key(connected_email, "history_id"))

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


@app.route("/stats")
def stats():
    user_id = request.args.get("user_id") or None
    return jsonify(get_user_view_summary(get_db(), user_id))


if __name__ == "__main__":
    app.run(debug=True)
