"""The routes the agent's tools call instead of opening the database.

A cloud turn runs Hermes as an unprivileged OS user that cannot read
`gmail_events.db`; its MCP server (`agent/google_mcp`) holds only a bearer
token minted for that turn (`db.mint_run_token`) and reaches this user's data
through these routes. Every route resolves the token first and scopes every
query to the token's user, so a valid token can never name anyone else. The
credentials route hands out an access token alone — refreshed here when it is
close to expiry — so the refresh token and the Google client secret never
leave the app process.

Registered on the app by `app.py` with `create_blueprint(get_db)`; none of it
is behind `login_required`, since no browser session is involved.
"""

from functools import wraps

from flask import Blueprint, g, jsonify, request

from db import (
    TODO_EDITABLE_FIELDS,
    get_todo,
    list_gmail_accounts,
    list_todos,
    resolve_run_token,
    save_user_todo,
    update_todo_fields,
)

# A turn can run for HERMES_TIMEOUT_SECONDS (600 by default); an access token
# with less than this left is refreshed before it is handed out, so the agent
# never watches its only credential die mid-turn.
CREDS_MIN_VALID_SECONDS = 15 * 60

_IMPORTANCE = ("low", "medium", "high")


def create_blueprint(get_db) -> Blueprint:
    bp = Blueprint("internal", __name__, url_prefix="/internal")

    def _auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            token = header[7:].strip() if header.startswith("Bearer ") else ""
            who = resolve_run_token(get_db(), token) if token else None
            if not who:
                return jsonify({"error": "unauthorized"}), 401
            g.run_user_id = who["user_id"]
            g.run_todo_id = who["todo_id"]
            return view(*args, **kwargs)
        return wrapped

    @bp.get("/accounts")
    @_auth
    def accounts():
        rows = list_gmail_accounts(get_db(), g.run_user_id)
        return jsonify({"accounts": [r["account_id"] for r in rows]})

    @bp.get("/credentials")
    @_auth
    def credentials():
        # Imported here, not at module top: pulling google-auth in costs the
        # web process nothing it did not already pay, but keeps this module
        # importable by the verify script before the app is built.
        from pollers.gmail.auth import get_google_credentials
        account = (request.args.get("account") or "").strip().lower() or None
        try:
            creds, granted, resolved = get_google_credentials(
                get_db(), g.run_user_id, account,
                min_valid_seconds=CREDS_MIN_VALID_SECONDS,
            )
        except RuntimeError as exc:
            # Not connected, revoked, or an unknown account: the tool shows
            # the message as an Error line, which is what the agent needs.
            return jsonify({"error": str(exc)}), 409
        return jsonify({
            "account": resolved,
            "token": creds.token,
            "expiry": creds.expiry.isoformat() + "Z" if creds.expiry else None,
            "scopes": sorted(granted),
        })

    @bp.get("/todos")
    @_auth
    def todos_list():
        status = (request.args.get("status") or "all").strip().lower()
        try:
            limit = max(1, int(request.args.get("limit") or 500))
        except ValueError:
            limit = 500
        rows = list_todos(get_db(), g.run_user_id)
        if status != "all":
            rows = [t for t in rows if t.get("status") == status]
        return jsonify(rows[:limit])

    @bp.get("/todos/<todo_id>")
    @_auth
    def todos_get(todo_id):
        todo = get_todo(get_db(), g.run_user_id, todo_id)
        if todo is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(todo)

    @bp.post("/todos")
    @_auth
    def todos_create():
        data = request.get_json(force=True, silent=True) or {}
        title = (data.get("title") or "").strip()
        importance = (data.get("importance") or "medium").strip().lower()
        if not title:
            return jsonify({"error": "title is required"}), 400
        if importance not in _IMPORTANCE:
            return jsonify({"error": f"importance must be one of {', '.join(_IMPORTANCE)}"}), 400
        db = get_db()
        todo_id = save_user_todo(
            db, g.run_user_id, title, importance,
            (data.get("due_date") or "").strip() or None,
            (data.get("suggested_action") or "").strip(),
        )
        return jsonify(get_todo(db, g.run_user_id, todo_id)), 201

    @bp.patch("/todos/<todo_id>")
    @_auth
    def todos_update(todo_id):
        # `update_todo_fields` owns the editable set and the enum checks — the
        # same rule the user-facing PATCH and the in-process tools run.
        data = request.get_json(force=True, silent=True) or {}
        data = {k: v for k, v in data.items() if k in TODO_EDITABLE_FIELDS}
        db = get_db()
        try:
            changed = update_todo_fields(db, g.run_user_id, todo_id, data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if not changed:
            return jsonify({"error": "not found"}), 404
        return jsonify(get_todo(db, g.run_user_id, todo_id))

    return bp
