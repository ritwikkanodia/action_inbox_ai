import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, render_template, request, jsonify

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
               estimated_time_minutes, due_date, relevant_link, reasoning, status, created_at
        FROM todos
        ORDER BY
            CASE status WHEN 'closed' THEN 1 ELSE 0 END,
            CASE urgency WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
            created_at DESC
        """
    ).fetchall()
    return render_template("index.html", todos=todos)


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
