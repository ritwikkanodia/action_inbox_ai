"""The tool functions, as plain Python closures over a `Services`.

`build_tools(svc)` returns callables whose names, signatures and docstrings are
what the MCP layer exposes — no MCP types here, so another executor can wrap
the same functions. Every tool returns a string; `_safe` turns any failure into
a one-line `Error: …` result so Hermes always sees an ordinary tool reply.
"""

import functools
import json
import logging
from typing import Callable

import google_scopes
from agent.google_mcp.services import SERVICE_SCOPES, Services, ToolError

logger = logging.getLogger(__name__)


def _safe(fn: Callable) -> Callable:
    """Never raise across the MCP boundary: every failure is a tool result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # HttpError, RefreshError, anything from the SDK
            logger.warning("%s failed: %s", fn.__name__, exc)
            return f"Error: {_describe(exc)}"
    return wrapper


def _describe(exc: Exception) -> str:
    from googleapiclient.errors import HttpError
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", "?")
        try:
            body = json.loads(exc.content.decode("utf-8", errors="replace"))
            message = body.get("error", {}).get("message") or str(exc)
        except Exception:
            message = str(exc)
        return f"Google API returned {status}: {message}"
    return f"{type(exc).__name__}: {exc}"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_tools(svc: Services) -> list[Callable]:
    tools: list[Callable] = []

    def register(fn):
        tools.append(_safe(fn))
        return fn

    @register
    def google_accounts() -> str:
        """List the user's connected Google accounts, which one this todo came from
        (default), and which services each has granted. Pass `account` to any other
        tool to use a non-default account."""
        default = svc.resolve_account(None)
        out = []
        for email in svc.accounts():
            granted = svc.granted(email)
            services = [label for key, (accepted, label) in SERVICE_SCOPES.items()
                        if any(s in granted for s in accepted)]
            out.append({
                "email": email,
                "default": email == default,
                "agent_access": google_scopes.has_agent_access(granted),
                "services": services,
            })
        return _dumps(out)

    # Tasks 4-6 add the Gmail, Drive/Docs, Sheets/Calendar/Contacts tools here.

    return tools
