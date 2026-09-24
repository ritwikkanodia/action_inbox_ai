import base64
import mimetypes

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


# What replaces the todo when the user opened a direct chat instead. Short on
# purpose: the instructions above it already say how to operate, and the only
# thing that changes is where the task comes from.
CHAT_CONTEXT = """\
There is no todo this time. The user opened a direct chat with you; their message is the task. \
Everything in your instructions still applies to it — use your tools before asking, never invent \
a fact about the user, stop before an irreversible act whose inputs you inferred, ask with an \
ask_user block when only they know — and produce the finished artifact. No email thread is \
attached; fetch whatever you need with the tools. The `todos_*` tools are their Action Inbox \
list; with no current todo, `todos_update` needs an explicit id from `todos_list`."""


def _format_todo(todo: dict) -> str:
    fields = [
        ("Title", todo.get("title")),
        ("Suggested action", todo.get("suggested_action")),
        ("Why this todo exists", todo.get("reasoning")),
        ("Importance", todo.get("importance")),
        ("Due", todo.get("due_date")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def user_turn(text: str, images: list[str] | None = None) -> dict:
    """A user input item. Plain string content unless `images` (local paths)
    are given, in which case the content is a list: the text as an
    `input_text` part, then one base64 data-URL `input_image` part per file.
    Files that cannot be read are skipped rather than failing the turn."""
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = [{"type": "input_text", "text": text}]
    for path in images:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        parts.append({"type": "input_image", "detail": "auto",
                      "image_url": f"data:{mime};base64,{encoded}"})
    return {"role": "user", "content": parts}


def strip_image_parts(items: list) -> list:
    """The persisted thread keeps the text of a user turn, never the image
    bytes: a base64 image re-sent on every later turn would dominate the
    context and the row. Returns a copy with list-shaped user content
    reduced to its `input_text` parts joined."""
    out = []
    for item in items:
        if (isinstance(item, dict) and item.get("role") == "user"
                and isinstance(item.get("content"), list)):
            text = "\n".join(
                p.get("text", "") for p in item["content"]
                if isinstance(p, dict) and p.get("type") == "input_text"
            )
            item = {**item, "content": text}
        out.append(item)
    return out


def build_initial_inputs(todo: dict, user_message: str, user_id: str) -> list[dict]:
    """Return the initial input items for a fresh agent thread.

    The first item is a context-only user turn marked with HIDDEN_CONTEXT_SENTINEL
    so the UI can filter it out. The user's own message, if any, is a separate
    visible turn.
    """
    context_parts = []
    if todo.get("source") == "chat":
        context_parts.append(CHAT_CONTEXT)
    else:
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
