"""Verifies GET /todos/<id>/actions — the three inferred ways to close a todo.

The OpenAI call is stubbed, so this covers the route's own behavior — caching,
refresh, ownership, malformed-response handling — without spending money. The
quality of the options themselves is a matter for the prompt, not this script.

Usage: python scripts/verify/verify_actions.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "actions.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only-not-a-real-key")

import sqlite3

import app as app_module
from agent import action_options
from db import init_db, upsert_user


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


generated: list[str] = []


def stub_generate(todo: dict, user_id: str) -> list[dict]:
    generated.append(todo.get("title") or "")
    n = len(generated)
    return [
        {"label": f"Option {i} (gen {n})", "detail": "d", "instruction": f"do {i}"}
        for i in (1, 2, 3)
    ]


def failing_generate(todo: dict, user_id: str) -> list[dict]:
    generated.append("fail")
    raise RuntimeError("openai is down")


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")
    other_id, _ = upsert_user(conn, "someone-else@example.com")
    for todo_id, owner in (("t1", user_id), ("t2", other_id)):
        conn.execute(
            "INSERT INTO todos (todo_id, user_id, source, dedup_key, title, status, "
            "created_at, updated_at) VALUES (?,?,'user',?,'Reply to Sarah re: Q3 budget',"
            "'open','2026-01-01','2026-01-01')",
            (todo_id, owner, todo_id),
        )
    conn.commit()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"

    action_options.generate_action_options = stub_generate

    # ---- schema ------------------------------------------------------------
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    check("todos has an action_options column", "action_options" in cols)

    # ---- first fetch generates and caches ----------------------------------
    data = client.get("/todos/t1/actions").get_json()
    check("three options returned", len(data["actions"]) == 3)
    check("options carry an executable instruction",
          all(a["instruction"] for a in data["actions"]))
    check("the todo went to the generator", generated == ["Reply to Sarah re: Q3 budget"])

    cached = conn.execute(
        "SELECT action_options FROM todos WHERE todo_id = 't1'"
    ).fetchone()[0]
    check("options persisted", len(json.loads(cached)) == 3)

    # ---- reopening is free -------------------------------------------------
    data = client.get("/todos/t1/actions").get_json()
    check("a second fetch does not call OpenAI again", len(generated) == 1)
    check("the cached options come back", data["actions"][0]["label"] == "Option 1 (gen 1)")

    # ---- refresh re-infers -------------------------------------------------
    data = client.get("/todos/t1/actions?refresh=1").get_json()
    check("refresh calls the generator again", len(generated) == 2)
    check("refresh returns the new options", data["actions"][0]["label"] == "Option 1 (gen 2)")
    check("refresh replaces the cache",
          json.loads(conn.execute(
              "SELECT action_options FROM todos WHERE todo_id = 't1'"
          ).fetchone()[0])[0]["label"] == "Option 1 (gen 2)")

    # ---- corrupt cache regenerates rather than 500s ------------------------
    conn.execute("UPDATE todos SET action_options = 'not json' WHERE todo_id = 't1'")
    conn.commit()
    data = client.get("/todos/t1/actions").get_json()
    check("a corrupt cache is regenerated", len(data["actions"]) == 3 and len(generated) == 3)

    # ---- failures surface inline, not as a 500 -----------------------------
    action_options.generate_action_options = failing_generate
    conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = 't1'")
    conn.commit()
    resp = client.get("/todos/t1/actions")
    check("a generator failure still returns 200", resp.status_code == 200)
    check("the failure is reported to the pane", "openai is down" in resp.get_json()["error"])
    check("a failed generation is not cached",
          conn.execute(
              "SELECT action_options FROM todos WHERE todo_id = 't1'"
          ).fetchone()[0] is None)

    # ---- another user's todo is not readable -------------------------------
    check("someone else's todo is a 404", client.get("/todos/t2/actions").status_code == 404)

    conn.close()
    print("\nAll action-option checks passed.")


if __name__ == "__main__":
    main()
