"""Per-todo resolution.

Intentionally empty. The live executor is `agent.hermes_runner`; re-exporting
`resolver.resolve_todo` here would import the unused Agents-SDK resolver on
every Flask boot. Import from the module you actually want.

(The SDK itself still loads regardless, via `tools/email.py`, which decorates
its Gmail tools with `@function_tool` and also holds the thread-fetch helper
`hermes_prompt` uses.)
"""
