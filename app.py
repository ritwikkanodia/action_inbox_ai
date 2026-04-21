import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, render_template, request, jsonify

from db import init_db, save_user_todo

app = Flask(__name__)


@app.template_filter("fmt_dt")
def fmt_dt(value: str | None) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%-d %b %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm")
    except ValueError:
        return value[:16]
DB_PATH = "gmail_events.db"


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        init_db(g.db)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.route("/")
def index():
    db = get_db()
    todos = db.execute(
        """
        SELECT todo_id, title, suggested_action, draft, urgency,
               estimated_time_minutes, due_date, relevant_link, reasoning, status, source, created_at
        FROM todos
        ORDER BY
            CASE status WHEN 'closed' THEN 1 ELSE 0 END,
            CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            created_at DESC
        """
    ).fetchall()
    return render_template("index.html", todos=todos)


@app.route("/todos", methods=["POST"])
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
    todo_id = save_user_todo(db, title, urgency, due_date, suggested_action)
    return jsonify({"ok": True, "todo_id": todo_id}), 201


@app.route("/todos/<todo_id>", methods=["PATCH"])
def update_todo(todo_id):
    ALLOWED = {"due_date", "urgency", "status"}
    data = request.get_json(force=True)
    updates = {k: v for k, v in data.items() if k in ALLOWED}
    if not updates:
        return jsonify({"error": "no valid fields"}), 400
    sets = ", ".join(f"{k} = ?" for k in updates)
    db = get_db()
    db.execute(f"UPDATE todos SET {sets} WHERE todo_id = ?", (*updates.values(), todo_id))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True)
