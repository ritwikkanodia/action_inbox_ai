from agent.tools.email import fetch_gmail_thread_context

# Sentinel prefix on bootstrap user turns that should NOT be shown in the UI.
# Kept inside the user-role content because the Agents SDK roundtrip drops
# unknown top-level keys, so a content-level marker is the most reliable way
# to identify these synthetic turns on read-back.
HIDDEN_CONTEXT_SENTINEL = "<<HIDDEN_CONTEXT>>\n"


def _format_todo(todo: dict) -> str:
    fields = [
        ("Title", todo.get("title")),
        ("Suggested action", todo.get("suggested_action")),
        ("Why this todo exists", todo.get("reasoning")),
        ("Urgency", todo.get("urgency")),
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
        email_context = fetch_gmail_thread_context(todo["source_meta"], user_id)
        if email_context:
            context_parts.append(f"Email thread:\n{email_context}")
    context_parts.append(_format_todo(todo))

    items: list[dict] = [
        {"role": "user", "content": HIDDEN_CONTEXT_SENTINEL + "\n".join(context_parts)}
    ]
    if user_message:
        items.append({"role": "user", "content": user_message})
    return items
