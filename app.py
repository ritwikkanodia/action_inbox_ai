import json
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
               estimated_time_minutes, due_date, relevant_link, reasoning, status, source, decision, created_at
        FROM todos
        WHERE title IS NOT NULL AND title != ''
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


@app.route("/todos/<todo_id>/ask-ai", methods=["POST"])
def ask_ai(todo_id):
    db = get_db()
    row = db.execute(
        "SELECT title, suggested_action, reasoning, draft, urgency, due_date, source, ai_thread FROM todos WHERE todo_id = ?",
        (todo_id,),
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

    from todo_generator import _get_client

    # Build base todo context
    parts = [f"Title: {row['title']}"]
    if row["suggested_action"]:
        parts.append(f"Suggested action: {row['suggested_action']}")
    if row["reasoning"]:
        parts.append(f"Why this todo exists: {row['reasoning']}")
    if row["draft"]:
        parts.append(f"Draft reply: {row['draft']}")
    if row["urgency"]:
        parts.append(f"Urgency: {row['urgency']}")
    if row["due_date"]:
        parts.append(f"Due: {row['due_date']}")
    todo_context = "\n".join(parts)

    # Build full input — include conversation history for follow-ups
    if thread or user_message:
        input_parts = [todo_context]
        if thread:
            input_parts.append("\nConversation so far:")
            for msg in thread:
                label = "Assistant" if msg["role"] == "assistant" else "User"
                input_parts.append(f"{label}: {msg['content']}")
        if user_message:
            input_parts.append(f"User: {user_message}")
        input_text = "\n".join(input_parts)
    else:
        input_text = todo_context

    client = _get_client()
    resp = client.responses.create(
        model="gpt-5.2",
        instructions=(
            "You are a task resolution assistant. Your job is to produce the output that resolves the todo — "
            "not explain how to do it, not recommend steps. Just do it. "
            "If the task is to reply to someone: output the exact reply, ready to send. "
            "If the task is to write something: output the written content. "
            "If the task requires external action the user must take (e.g. a booking, a call): "
            "output the exact script or message they would use to complete it. "
            "Use web search proactively for anything that benefits from current information: "
            "prices, availability, contact details, recent events, deadlines, or factual lookups. "
            "No preamble. No 'here is a draft'. No meta-commentary. Just the output."
        ),
        input=input_text,
        tools=[{"type": "web_search"}],
    )
    search_count = sum(1 for item in resp.output if item.type == "web_search_call")
    if search_count:
        app.logger.info("Web searches fired: %d", search_count)

    ai_text = resp.output_text
    if user_message:
        thread.append({"role": "user", "content": user_message})
    thread.append({"role": "assistant", "content": ai_text})

    db.execute("UPDATE todos SET ai_thread = ? WHERE todo_id = ?", (json.dumps(thread), todo_id))
    db.commit()

    return jsonify({"thread": thread})


@app.route("/todos/<todo_id>", methods=["PATCH"])
def update_todo(todo_id):
    ALLOWED = {"due_date", "urgency", "status", "decision"}
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
