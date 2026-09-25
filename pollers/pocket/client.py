"""The one place that talks to Pocket.

Pocket (heypocket.com) documents a REST API, but its recordings endpoints do
not return action items — the only documented action-item shape is inside
webhook payloads. Its hosted MCP server does return them, with priority,
context, assignee and due date, and it takes the same `pk_…` API key, so the
poller uses that: one `search_pocket_actionitems` call per cycle over
streamable HTTP, through the `mcp` package the Google server already needs.

`search_action_items` is what the poller calls and what the verify script
stubs. `parse_search_result` is split out because it is where the wire shape
is interpreted, and it is the part worth pinning.
"""
import asyncio
import json
import os

POCKET_MCP_URL = os.environ.get("POCKET_MCP_URL", "https://public.heypocketai.com/mcp")
SEARCH_TOOL = "search_pocket_actionitems"
# Pocket's server answers a search in well under this; the bound only matters
# when it is wedged, and it has to be well inside the 30s poll cycle.
TIMEOUT_SECONDS = float(os.environ.get("POCKET_TIMEOUT_SECONDS", "20"))


def parse_search_result(result) -> list[dict]:
    """The `actions` list out of a CallToolResult.

    The server returns `{"success": true, "data": {"actions": [...]}}`, either
    as structured content or as JSON in a text block. A tool-level error
    raises so the poller logs it and leaves its cursor alone."""
    if getattr(result, "is_error", False):
        raise RuntimeError(f"Pocket search failed: {_text_of(result)}")
    data = getattr(result, "structured_content", None)
    if not data:
        text = _text_of(result)
        data = json.loads(text) if text else {}
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Pocket search failed: {data.get('error') or data}")
    actions = ((data or {}).get("data") or {}).get("actions") or []
    return [a for a in actions if isinstance(a, dict)]


def _text_of(result) -> str:
    return "".join(
        getattr(part, "text", "") for part in (getattr(result, "content", None) or [])
        if getattr(part, "type", "") == "text"
    )


def describe_error(exc: BaseException) -> str:
    """One line for the poll log.

    anyio wraps a failed handshake in nested `ExceptionGroup`s whose own
    message is "unhandled errors in a TaskGroup"; the leaf is what happened.
    A rejected key comes back as an `MCPError` carrying a JSON-RPC code and a
    generic message, so the code is kept — it is the only thing that varies."""
    if isinstance(exc, BaseExceptionGroup):
        leaves = [describe_error(sub) for sub in exc.exceptions]
        return "; ".join(dict.fromkeys(leaves)) or str(exc)
    error = getattr(exc, "error", None)
    if error is not None and hasattr(error, "code"):
        return f"{type(exc).__name__}: {error.message} (code {error.code})"
    return f"{type(exc).__name__}: {exc}"


def search_action_items(
    api_key: str, recording_date_from: str | None = None, status: str | None = None
) -> list[dict]:
    """Every action item Pocket has for this key, optionally from a recording
    date onward and in one status (`TODO`, `IN_PROGRESS`, `COMPLETED`,
    `CANCELLED`). Synchronous on purpose — the poller is. Any failure is one
    `RuntimeError` with a readable message; the poll loop logs it."""
    arguments: dict = {}
    if recording_date_from:
        arguments["recordingDateFrom"] = recording_date_from
    if status:
        arguments["status"] = status
    try:
        return asyncio.run(_call_tool(api_key, SEARCH_TOOL, arguments))
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Pocket request failed: {describe_error(exc)} — "
            "if this persists, re-check the API key in Settings"
        ) from exc


async def _call_tool(api_key: str, name: str, arguments: dict):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = httpx2.Timeout(TIMEOUT_SECONDS, read=TIMEOUT_SECONDS)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as http:
        async with streamable_http_client(POCKET_MCP_URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments, read_timeout_seconds=TIMEOUT_SECONDS)
                return parse_search_result(result)
