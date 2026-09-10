from agent.tools.email import fetch_gmail_thread_context

# Sentinel prefix on bootstrap user turns that should NOT be shown in the UI.
# Kept inside the user-role content because the Agents SDK roundtrip drops
# unknown top-level keys, so a content-level marker is the most reliable way
# to identify these synthetic turns on read-back.
HIDDEN_CONTEXT_SENTINEL = "<<HIDDEN_CONTEXT>>\n"

# How a clicked suggestion is introduced, instead of "the user says".
#
# The user consented to a four-word label; the sentence underneath it was
# written by a model and can assert things about them they never said — that an
# experience was positive, that they liked something, that they want a
# particular outcome. Presented as their own words it defeats the rule against
# inventing facts about the user, because a fabricated claim now arrives
# wearing their voice. Framed as a route they picked, the claims inside it go
# back to being guesses that have to be checked.
#
# Lives here rather than in either prompt module because both executors need it
# and neither should import the other's.
SUGGESTED_ROUTE_LEAD = """\
The user picked this route from a list of suggestions. They clicked its heading; the wording \
below was drafted for them by another model, and they may never have read it.

The chosen route:
"""

# Deliberately placed *after* the instruction rather than before it. A caution
# that precedes a concrete directive loses to it — measured, not assumed: with
# this text in front, an instruction ending "one sentence that reflects a
# positive experience" still produced an invented 5-star review and no question.
# The last thing in the prompt is what gets obeyed, so the constraint goes last.
SUGGESTED_ROUTE_TAIL = """

Before acting on that: it is a route, not a statement. Strike from it every claim about the \
user's opinion, experience, satisfaction, rating or preference — "a positive experience", \
"you loved it", "how helpful it was" — and treat what remains as the instruction. Those claims \
were guessed by the model that drafted this text; the user did not make them, and rule 3 does \
not license acting on them. If removing them leaves a gap you need in order to finish, that \
gap is exactly what to ask about. Do the task, not the assumption."""


def _format_todo(todo: dict) -> str:
    fields = [
        ("Title", todo.get("title")),
        ("Suggested action", todo.get("suggested_action")),
        ("Why this todo exists", todo.get("reasoning")),
        ("Importance", todo.get("importance")),
        ("Due", todo.get("due_date")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def build_initial_inputs(todo: dict, user_message: str, user_id: str) -> list[dict]:
    """Return the initial input items for a fresh agent thread.

    The first item is a context-only user turn marked with HIDDEN_CONTEXT_SENTINEL
    so the UI can filter it out. The user's own message, if any, is a separate
    visible turn.
    """
    context_parts = []
    if todo.get("source") == "gmail" and todo.get("source_meta"):
        email_context = fetch_gmail_thread_context(
            todo["source_meta"], user_id, todo.get("account_id")
        )
        if email_context:
            context_parts.append(f"Email thread:\n{email_context}")
    context_parts.append(_format_todo(todo))

    items: list[dict] = [
        {"role": "user", "content": HIDDEN_CONTEXT_SENTINEL + "\n".join(context_parts)}
    ]
    if user_message:
        items.append({"role": "user", "content": user_message})
    return items
