import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(override=True)

from agent import runs
from agent.executor import ExecutorCancelled, ExecutorError, current_executor, resolve

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
               estimated_time_minutes, due_date, relevant_link, reasoning, status, source, account_id, decision, created_at, source_meta,
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
    gmail_accounts = list_gmail_accounts(db, user_id)
    gmail_connected = bool(gmail_accounts)
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
    """Filter an SDK input list down to renderable {role, content} bubbles.

    Assistant bubbles may also carry `questions`: a clarifying question the
    agent asked as a fenced block inside its reply, lifted out here so the
    frontend can render it as choices instead of making the user type.

    Splitting on read rather than on write is deliberate — the stored reply
    keeps the block, so the chips are re-derived every time the todo is opened
    and survive a reload without a new column to migrate.
    """
    from agent.clarify import split_questions
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

        bubble = {"role": role, "content": text}
        if role == "assistant":
            bubble["content"], questions = split_questions(text)
            if questions:
                bubble["questions"] = questions
            elif not bubble["content"]:
                continue
        out.append(bubble)
    return out


def _load_thread(raw) -> list:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _own_todo(db, todo_id: str, user_id: str):
    return db.execute(
        "SELECT title, suggested_action, reasoning, importance, due_date, source, "
        "account_id, ai_thread, executor_state, action_options, source_meta "
        "FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()


def _persist_resolution(todo_id: str, user_id: str, thread: list, state) -> None:
    """Write a completed run's result from the background thread.

    Opens its own connection: `get_db` caches on Flask's `g`, which belongs to
    the request that started the run and is long gone by the time this runs.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute(
            "UPDATE todos SET ai_thread = ?, executor_state = ?, updated_at = ? "
            "WHERE todo_id = ? AND user_id = ?",
            (
                json.dumps(thread),
                state,
                datetime.now(timezone.utc).isoformat(),
                todo_id,
                user_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _resolution_work(todo: dict, thread: list, user_message: str, user_id: str, state,
                     from_suggestion: bool = False):
    """Build the callable `agent.runs.start` will execute on its own thread.

    Renders its own failures into the thread rather than raising: the frontend
    reads `thread` without checking the status code, so anything not in there is
    invisible to the user.
    """
    todo_id = todo["todo_id"]

    def notice(text: str) -> list:
        shown = list(thread)
        if user_message:
            shown.append({"role": "user", "content": user_message})
        shown.append({"role": "assistant", "content": text})
        return shown

    def work(cancel, progress):
        try:
            final, new_state = resolve(
                todo, thread, user_message, user_id, state,
                cancel=cancel, progress=progress, from_suggestion=from_suggestion,
            )
        except ExecutorCancelled:
            # Not persisted. The agent may already have sent mail or submitted a
            # form before the stop landed, so neither the log nor the session id
            # should advance as if the turn had completed.
            #
            # Logged because a discarded turn is otherwise invisible: nothing
            # reaches the database, and the thread on screen stays continuous,
            # so a user reporting "it lost my conversation" leaves no evidence
            # behind to check. `state` is the session the *next* turn will
            # resume — unchanged by this one.
            app.logger.warning(
                "Resolution stopped: todo=%s user=%s executor=%s state=%s "
                "(turn discarded; state unchanged)",
                todo_id, user_id, current_executor(), state,
            )
            return notice("⏹ Stopped. Anything already done before the stop stands."), runs.CANCELLED
        except ExecutorError as exc:
            app.logger.warning(
                "Resolution failed: todo=%s user=%s executor=%s state=%s: %s "
                "(turn discarded; state unchanged)",
                todo_id, user_id, current_executor(), state, exc,
            )
            return notice(f"⚠️ Resolution failed: {exc}"), runs.ERROR

        _persist_resolution(todo_id, user_id, final, new_state)
        return final, runs.DONE

    return work


@app.route("/todos/<todo_id>/actions", methods=["GET"])
@login_required
def todo_action_options(todo_id):
    """The three ways this todo could be closed.

    Generated once and cached in `todos.action_options`, so reopening a todo
    costs nothing; `?refresh=1` re-runs the inference.
    """
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = _own_todo(db, todo_id, user_id)
    if row is None:
        return jsonify({"error": "not found"}), 404

    if row["action_options"] and request.args.get("refresh") != "1":
        try:
            return jsonify({"actions": json.loads(row["action_options"])})
        except ValueError:
            pass  # Corrupt cache — fall through and regenerate.

    # Imported lazily so the OpenAI client is only built by workers that use it.
    from agent.action_options import generate_action_options

    todo = dict(row)
    todo["todo_id"] = todo_id
    try:
        actions = generate_action_options(todo, user_id)
    except Exception as exc:
        app.logger.exception("Failed to generate action options")
        # 200 with an error field: the pane renders this inline next to a retry,
        # which is more useful than a silent empty section.
        return jsonify({"actions": [], "error": str(exc)})

    db.execute(
        "UPDATE todos SET action_options = ?, updated_at = ? "
        "WHERE todo_id = ? AND user_id = ?",
        (json.dumps(actions), datetime.now(timezone.utc).isoformat(), todo_id, user_id),
    )
    db.commit()
    return jsonify({"actions": actions})


@app.route("/todos/<todo_id>/ask-ai", methods=["POST"])
@login_required
def ask_ai(todo_id):
    """Start a resolution turn, or hand back what is already there.

    Returns as soon as the run is registered rather than when it finishes, so
    the caller keeps a connection free to poll `/run` and to stop it.
    """
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = _own_todo(db, todo_id, user_id)
    if row is None:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    user_message = (data.get("message") or "").strip()
    # Set when the message is a suggested action the user clicked rather than
    # something they typed. The executor needs the difference: they consented
    # to a short label, not to the generated sentence behind it.
    from_suggestion = bool(data.get("from_suggestion"))

    active = runs.get(user_id, todo_id)
    if active is not None and active.status == runs.RUNNING:
        # Don't start a second agent on the same todo behind the user's back.
        return jsonify(
            {
                "status": runs.RUNNING,
                "run_id": active.run_id,
                "thread": _thread_for_client(active.thread),
                "activity": active.activity_snapshot(),
            }
        )

    thread = _load_thread(row["ai_thread"])

    # No message means "show me what's there" — the detail pane asks this every
    # time it opens a todo with an existing thread, and it must never spend a
    # turn or launch an agent on an empty prompt.
    if not user_message:
        return jsonify({"status": "idle", "thread": _thread_for_client(thread)})

    todo = dict(row)
    todo["todo_id"] = todo_id
    seeded = thread + [{"role": "user", "content": user_message}]
    run = runs.start(
        user_id,
        todo_id,
        seeded,
        _resolution_work(todo, thread, user_message, user_id, row["executor_state"],
                         from_suggestion),
    )
    return jsonify(
        {
            "status": run.status,
            "run_id": run.run_id,
            "thread": _thread_for_client(run.thread),
            "activity": run.activity_snapshot(),
        }
    )


@app.route("/todos/<todo_id>/run", methods=["GET"])
@login_required
def todo_run(todo_id):
    """Poll a resolution run. `idle` means this process has no run for the todo."""
    user_id = current_user_id()
    assert user_id
    run = runs.get(user_id, todo_id)
    if run is None:
        return jsonify({"status": "idle"})
    return jsonify(
        {
            "status": run.status,
            "run_id": run.run_id,
            "thread": _thread_for_client(run.thread),
            "activity": run.activity_snapshot(),
        }
    )


@app.route("/todos/<todo_id>/run/stop", methods=["POST"])
@login_required
def stop_todo_run(todo_id):
    """Stop the running resolution — for Hermes, by killing the CLI process."""
    user_id = current_user_id()
    assert user_id
    return jsonify({"stopped": runs.stop(user_id, todo_id)})


@app.route("/todos/<todo_id>/context", methods=["GET"])
@login_required
def todo_context(todo_id):
    db = get_db()
    user_id = current_user_id()
    assert user_id
    row = db.execute(
        "SELECT source, account_id, source_meta, relevant_link FROM todos WHERE todo_id = ? AND user_id = ?",
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
        account_id = row["account_id"]
        try:
            # account_id is None for todos created before per-account provenance;
            # get_gmail_service falls back to the user's first connected account.
            service = get_gmail_service(db, user_id, account_id)
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
                "account": account_id,
                "thread_url": row["relevant_link"] or gmail_thread_url(thread_id, account_id),
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
    # Drop the executor's own state too: clearing only the display log would
    # leave an executor like Hermes still remembering the old conversation.
    # Any run in flight goes with it, for the same reason.
    runs.discard(user_id, todo_id)
    db.execute(
        "UPDATE todos SET ai_thread = NULL, executor_state = NULL, updated_at = ? "
        "WHERE todo_id = ? AND user_id = ?",
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
    accounts = list_gmail_accounts(db, user_id)
    return jsonify({
        "sources": {
            "fathom": {
                "connected": bool(fathom),
                "api_key_preview": f"...{fathom_key[-6:]}" if fathom_key else None,
            },
            "gmail": {
                "accounts": [
                    {"email": a["account_id"], "connected_at": a["connected_at"]}
                    for a in accounts
                ],
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
            # An explicit account_id disconnects one mailbox; omitting it
            # disconnects every Gmail account for this user.
            account_id = (data.get("account_id") or "").strip().lower() or None
            clear_source_connection(db, user_id, "gmail", account_id)
            return jsonify({
                "ok": True,
                "accounts": [
                    {"email": a["account_id"], "connected_at": a["connected_at"]}
                    for a in list_gmail_accounts(db, user_id)
                ],
            })
        return jsonify({"error": "use /settings/sources/gmail/auth to connect"}), 400
    return jsonify({"error": "unhandled"}), 500


@app.route("/stats")
def stats():
    user_id = request.args.get("user_id") or None
    return jsonify(get_user_view_summary(get_db(), user_id))


if __name__ == "__main__":
    app.run(debug=True)
