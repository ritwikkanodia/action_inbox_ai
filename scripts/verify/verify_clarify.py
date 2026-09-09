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

    # A live run produced exactly this: two rating questions with options plus
    # "paste the sentence you want posted", which has no plausible menu. It must
    # survive, or the UI shows fewer questions than the agent asked and Send
    # returns an incomplete answer.
    free_text = "Prose.\n" + block({"questions": [
        {"question": "Paste the sentence you want posted", "header": "Wording"},
        {"question": "Which rating?", "options": ["5 stars", "4 stars"]},
    ]})
    prose, questions = split_questions(free_text)
    check("an option-less question is kept, not dropped", len(questions) == 2)
    check("it is kept with empty options for the frontend to render as a field",
          questions[0]["options"] == [])
    check("the question alongside it still gets its options",
          len(questions[1]["options"]) == 2)
    check("the block is still stripped", prose == "Prose.")

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


def check_suggested_route_framing() -> None:
    """A clicked suggestion must not reach the agent as the user's own words.

    The instruction behind an option is model-written and can assert things the
    user never said. Presented as their statement it defeats the rule against
    inventing facts about them, since the fabrication arrives in their voice —
    which is exactly how a "reflects a positive experience" instruction got a
    glowing review written on a live Trustpilot form.
    """
    print("\n-- suggested-route framing --")

    from agent.hermes_prompt import build_followup_prompt, build_prompt

    todo = {"todo_id": "t1", "title": "Leave a review", "source": "user"}
    msg = "submit a review, ideally one that reflects a positive experience"

    typed = build_prompt(todo, msg, "u", False)
    clicked = build_prompt(todo, msg, "u", True)
    check("a typed message is still presented as the user's words",
          "## The user says" in typed and "chosen route" not in typed)
    check("a clicked suggestion is presented as a route, not a statement",
          "## The chosen route" in clicked and "## The user says" not in clicked)
    check("the framing says its claims are not the user's to act on",
          "the user did not make them" in clicked)
    check("the instruction itself still reaches the agent", msg in clicked)
    # Order matters: a caution placed before a concrete directive loses to it.
    # Measured — the earlier prefix-only version still produced an invented
    # 5-star review. The constraint has to be the last thing read.
    check("the caution comes after the instruction, not before",
          clicked.index("Strike from it every claim") > clicked.index(msg))

    check("a typed follow-up keeps the plain framing",
          "The user says:" in build_followup_prompt(msg, False))
    followup = build_followup_prompt(msg, True)
    check("a clicked follow-up is reframed too",
          "picked this route from a list" in followup)
    check("the follow-up caution also comes last",
          followup.index("Strike from it every claim") > followup.index(msg))

    # The flag has to survive the route, not just exist in the prompt builder.
    conn = sqlite3.connect(os.environ["DB_PATH"])
    seen = {}

    def stub_resolve(todo, thread, user_message, user_id, state,
                     cancel=None, progress=None, from_suggestion=False):
        seen["flag"] = from_suggestion
        return list(thread) + [{"role": "assistant", "content": "ok"}], state

    original = app_module.resolve
    app_module.resolve = stub_resolve
    try:
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = conn.execute(
                "SELECT user_id FROM todos WHERE todo_id = 't1'").fetchone()[0]

        client.post("/todos/t1/ask-ai", json={"message": "hi"})
        _wait_for(seen)
        check("a typed message reaches the executor unflagged", seen.get("flag") is False)

        seen.clear()
        client.post("/todos/t1/ask-ai",
                    json={"message": "hi", "from_suggestion": True})
        _wait_for(seen)
        check("a clicked suggestion reaches the executor flagged",
              seen.get("flag") is True)
    finally:
        app_module.resolve = original
        conn.close()


def _wait_for(seen: dict, timeout: float = 5.0) -> None:
    """Runs execute on a background thread; give it a moment to land."""
    import time

    deadline = time.time() + timeout
    while "flag" not in seen and time.time() < deadline:
        time.sleep(0.02)


def main() -> None:
    check_parser()
    check_route()
    check_suggested_route_framing()
    print("\nAll clarifying-question checks passed.")


if __name__ == "__main__":
    main()
