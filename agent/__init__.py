"""Per-todo resolution.

Intentionally empty. Executors are selected at runtime through
`agent.executor`, which imports them lazily; re-exporting
`resolver.resolve_todo` here would defeat that by pulling the Agents-SDK
executor in on every Flask boot regardless of TODO_EXECUTOR. Import from the
module you actually want.
"""
