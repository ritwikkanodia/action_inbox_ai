from agent.tools.email import fetch_gmail_thread_context


def _format_todo(todo: dict) -> str:
    fields = [
        ("Title", todo.get("title")),
        ("Suggested action", todo.get("suggested_action")),
        ("Why this todo exists", todo.get("reasoning")),
        ("Urgency", todo.get("urgency")),
        ("Due", todo.get("due_date")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def build_initial_input(todo: dict, user_message: str, user_id: str) -> str:
    parts = []
    if todo.get("source") == "gmail" and todo.get("source_meta"):
        email_context = fetch_gmail_thread_context(todo["source_meta"], user_id)
        if email_context:
            parts.append(f"Email thread:\n{email_context}")
    parts.append(_format_todo(todo))
    if user_message:
        parts.append(f"User: {user_message}")
    return "\n".join(parts)
