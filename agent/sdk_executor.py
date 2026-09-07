"""The in-process OpenAI Agents SDK executor, behind the standard interface.

This is the original resolver. It stays wired because it is the only executor
that runs where there is no local CLI — notably the deployed container, which
has no `hermes` binary. Set TODO_EXECUTOR=agents_sdk there.

It keeps no out-of-band state: the persisted thread is round-tripped straight
back into the agent, so `state` is ignored on the way in and None on the way out.

`cancel` is accepted and ignored: Runner.run_sync owns the loop and there is no
subprocess to signal, so a stop can only detach the UI from a run that keeps
going. Only `hermes` can actually be interrupted.
"""

from agent.resolver import resolve_todo


def resolve(
    todo: dict,
    thread: list,
    user_message: str,
    user_id: str,
    state: str | None,
    cancel=None,
) -> tuple[list, None]:
    return resolve_todo(todo, thread, user_message, user_id), None
