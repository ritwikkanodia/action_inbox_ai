"""Entrypoint: `python -m agent.google_mcp`, run by Hermes as a stdio MCP server.

Reads the per-run binding from the environment. With no binding it still serves
— with zero tools — so a Hermes invocation that is not an Action Inbox turn is
unaffected. Logging goes to stderr only; stdout is the protocol channel.
"""

import logging
import os
import sys

# The repo's .env supplies DB_PATH and the Google client id/secret the refresh
# needs. Hermes launches this with cwd set to the repo root by the install
# script, so a relative lookup finds it. Loaded before any app import, as the
# entrypoints do, since module-level code reads env vars.
from dotenv import load_dotenv
load_dotenv(override=False)

from mcp.server.mcpserver import MCPServer

from agent.google_mcp.services import Binding, Services, binding_from_env


INSTRUCTIONS = (
    "The user's own Google account over the API. Use these tools for anything in "
    "Gmail, Drive, Docs, Sheets, Calendar or Contacts instead of a browser. Tools act "
    "on the account the todo came from unless you pass `account`; call "
    "google_accounts to see the options. The todos_* tools are the user's Action "
    "Inbox list itself: read it, add a follow-up, or close the current todo."
)


def make_server(binding: Binding | None) -> MCPServer:
    server = MCPServer("action_inbox_google", instructions=INSTRUCTIONS)
    if binding is None:
        logging.getLogger(__name__).info("no AIB binding; serving zero tools")
        return server
    from agent.google_mcp.tools import build_tools
    from agent.todo_tools import HttpTodoBackend, build_todo_tools
    for fn in build_tools(Services(binding)):
        server.add_tool(fn)
    # Same process, same binding: the todo tools only need the user and the
    # database — or, in HTTP mode, the same token — which the Google binding
    # already names. A second MCP server would cost a second subprocess inside
    # Hermes' startup budget for nothing.
    if binding.http:
        from agent.internal_client import InternalClient
        backend = HttpTodoBackend(InternalClient(binding.api_url, binding.run_token))
    else:
        backend = binding.db_path
    for fn in build_todo_tools(backend, binding.user_id, binding.todo_id or None):
        server.add_tool(fn)
    return server


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="[google_mcp] %(levelname)s %(message)s")
    make_server(binding_from_env(os.environ)).run("stdio")


if __name__ == "__main__":
    main()
