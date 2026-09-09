"""Verifies the clarifying-question protocol: the parser and its route wiring.

Two things are being checked. First, `agent.clarify.split_questions` — that a
well-formed block becomes structured options and that every malformed shape
degrades to the untouched reply rather than losing it. Second, that a reply
stored in `todos.ai_thread` comes back off `POST /todos/<id>/ask-ai` already
split, which is what makes the chips survive a reload without any new column.

No OpenAI call and no executor run, so this costs nothing.

Usage: python scripts/verify/verify_clarify.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "clarify.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only-not-a-real-key")

import sqlite3

import app as app_module
from agent.clarify import split_questions
from db import init_db, upsert_user


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def block(payload: dict) -> str:
    return "```ask_user\n" + json.dumps(payload) + "\n```"


GOOD = {
    "questions": [
        {
            "question": "How many stars should the review be?",
            "header": "Rating",
            "options": [
                {"label": "5 - excellent", "detail": "Nothing to complain about"},
                {"label": "4 - good", "detail": "Minor gripes"},
                {"label": "3 - mixed", "detail": "Would not recommend strongly"},
            ],
        }
    ]
}


def check_parser() -> None:
    print("\n-- parser --")

    prose, questions = split_questions("I need one thing first.\n\n" + block(GOOD))
    check("the block is lifted out of the prose", prose == "I need one thing first.")
    check("one question is parsed", len(questions) == 1)
    check("the question text survives",
          questions[0]["question"] == "How many stars should the review be?")
    check("all three options survive", len(questions[0]["options"]) == 3)
    check("option detail survives",
          questions[0]["options"][0]["detail"] == "Nothing to complain about")
    check("multiSelect defaults to False", questions[0]["multiSelect"] is False)

    # ---- a reply with no block is the overwhelmingly common case -----------
    plain = "Here is the finished reply, ready to send."
    prose, questions = split_questions(plain)
    check("a reply with no block is untouched", prose == plain and questions == [])

    # ---- every malformed shape must keep the reply ------------------------
    broken = "Some prose.\n```ask_user\n{not json at all}\n```"
    prose, questions = split_questions(broken)
    check("invalid JSON leaves the reply intact", prose == broken and questions == [])

    no_options = "Prose.\n" + block({"questions": [{"question": "Which one?"}]})
    prose, questions = split_questions(no_options)
    check("a question with no options is left as prose",
          prose == no_options and questions == [])

    no_text = "Prose.\n" + block({"questions": [{"options": [{"label": "A"}]}]})
    _, questions = split_questions(no_text)
    check("a question with no text is dropped", questions == [])

    # ---- lenient shapes an agent plausibly writes --------------------------
    _, questions = split_questions(
        block({"question": "Pick one", "options": ["Yes", "No"]})
    )
    check("a bare question object is accepted", len(questions) == 1)
    check("bare string options are accepted",
          [o["label"] for o in questions[0]["options"]] == ["Yes", "No"])

    _, questions = split_questions(
        block({"questions": [{"question": "Q", "options": ["A", "a", "B"]}]})
    )
    check("duplicate option labels are collapsed",
          [o["label"] for o in questions[0]["options"]] == ["A", "B"])

    _, questions = split_questions(
        block({"questions": [{"question": "Q",
                              "options": ["1", "2", "3", "4", "5", "6"]}]})
    )
    check("options are capped at 4", len(questions[0]["options"]) == 4)

    _, questions = split_questions(
        block({"questions": [{"question": f"Q{i}", "options": ["A", "B"]}
                             for i in range(6)]})
    )
    check("questions are capped at 3", len(questions) == 3)

    # ---- the block need not be last ---------------------------------------
    trailing = block(GOOD) + "\n\nOnce you answer I'll submit it."
    prose, questions = split_questions(trailing)
    check("prose after the block is preserved",
          prose == "Once you answer I'll submit it." and len(questions) == 1)


def check_route() -> None:
    print("\n-- route --")

    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    # A completed turn as the executor would have stored it: the raw reply,
    # block included. Nothing strips it on the way in.
    stored = [
        {"role": "user", "content": "Leave the review"},
        {"role": "assistant",
         "content": "I need your rating before I can post anything.\n\n" + block(GOOD)},
    ]
    conn.execute(
        "INSERT INTO todos (todo_id, user_id, source, dedup_key, title, status, "
        "created_at, updated_at, ai_thread) VALUES (?, ?, 'gmail', 'k1', "
        "'Leave a review', 'open', '2026-01-01', '2026-01-01', ?)",
        ("t1", user_id, json.dumps(stored)),
    )
    conn.commit()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id

    # Posting with no message must not run the executor — it just reads back.
    resp = client.post("/todos/t1/ask-ai", json={})
    check("reading an existing thread returns 200", resp.status_code == 200)
    data = resp.get_json()
    check("the thread has both bubbles", len(data["thread"]) == 2)

    bubble = data["thread"][-1]
    check("the block is not shown as text", "ask_user" not in bubble["content"])
    check("the prose is shown",
          bubble["content"] == "I need your rating before I can post anything.")
    check("the question rides on the bubble", len(bubble.get("questions") or []) == 1)
    check("the options reach the client",
          len(bubble["questions"][0]["options"]) == 3)

    # The point of parsing on read: nothing was rewritten in the database, so a
    # second read — a reload — produces the same chips.
    again = client.post("/todos/t1/ask-ai", json={}).get_json()
    check("a reload yields the same question",
          again["thread"][-1]["questions"] == bubble["questions"])
    check("the stored row still holds the raw block",
          "ask_user" in conn.execute(
              "SELECT ai_thread FROM todos WHERE todo_id = 't1'"
          ).fetchone()[0])

    # A user bubble must never carry chips, however it is worded.
    check("user bubbles carry no questions", not data["thread"][0].get("questions"))

    conn.close()


def main() -> None:
    check_parser()
    check_route()
    print("\nAll clarifying-question checks passed.")


if __name__ == "__main__":
    main()
