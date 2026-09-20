"""Per-run binding, account resolution, scope gating, and Google API clients.

`Binding` is who this server process acts for. `Services` turns an optional
`account` argument into a concrete connected address, checks the stored token
carries the scope a tool needs, and hands back a cached API client. Everything
user-facing goes through `ToolError`, whose message the tool layer returns as an
`Error:` line — nothing here raises across the MCP boundary.
"""

import logging
import os
import sqlite3
from typing import Callable, Mapping, NamedTuple

logger = logging.getLogger(__name__)

_ENV_USER = "AIB_USER_ID"
_ENV_ACCOUNT = "AIB_ACCOUNT_ID"
_ENV_DB = "AIB_DB_PATH"


class Binding(NamedTuple):
    user_id: str
    account_id: str   # '' when the todo predates multi-account
    db_path: str


def _clean(value: str | None) -> str:
    """An env value as the runner meant it: stripped, and empty when Hermes left a
    `${VAR}` reference unexpanded because the variable was never set."""
    value = (value or "").strip()
    if value.startswith("${") and value.endswith("}"):
        return ""
    return value


def binding_from_env(environ: Mapping[str, str]) -> Binding | None:
    """The per-run binding, or None when this is not an Action Inbox turn."""
    user_id = _clean(environ.get(_ENV_USER))
    if not user_id:
        return None
    account = _clean(environ.get(_ENV_ACCOUNT)).lower()
    db_path = _clean(environ.get(_ENV_DB)) or os.environ.get("DB_PATH", "gmail_events.db")
    return Binding(user_id=user_id, account_id=account, db_path=db_path)


class ToolError(Exception):
    """A failure the agent should read as a tool result, worded for the user."""


_GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"
_GMAIL_RW = "https://www.googleapis.com/auth/gmail.modify"

# service key -> (any of these scopes satisfies it, label used in messages)
SERVICE_SCOPES: dict[str, tuple[tuple[str, ...], str]] = {
    "gmail_read": ((_GMAIL_RO, _GMAIL_RW), "Gmail (read)"),
    "gmail": ((_GMAIL_RW,), "Gmail"),
    "drive": (("https://www.googleapis.com/auth/drive",), "Drive"),
    "docs": (("https://www.googleapis.com/auth/documents",), "Docs"),
    "sheets": (("https://www.googleapis.com/auth/spreadsheets",), "Sheets"),
    "calendar": (("https://www.googleapis.com/auth/calendar.events",), "Calendar"),
    "contacts": (("https://www.googleapis.com/auth/contacts.readonly",), "Contacts"),
}

# service key -> (discovery name, version)
_CLIENTS = {
    "gmail": ("gmail", "v1"),
    "drive": ("drive", "v3"),
    "docs": ("docs", "v1"),
    "sheets": ("sheets", "v4"),
    "calendar": ("calendar", "v3"),
    "people": ("people", "v1"),
}


def _default_creds_provider(conn, user_id, account_id):
    from pollers.gmail.auth import get_google_credentials
    return get_google_credentials(conn, user_id, account_id)


def _default_builder(kind: str, account: str, creds):
    from googleapiclient.discovery import build
    name, version = _CLIENTS[kind]
    return build(name, version, credentials=creds, cache_discovery=False)


class Services:
    def __init__(
        self,
        binding: Binding,
        creds_provider: Callable | None = None,
        builder: Callable | None = None,
        account_lister: Callable[[], list[str]] | None = None,
    ):
        self.binding = binding
        self._creds_provider = creds_provider or _default_creds_provider
        self._builder = builder or _default_builder
        self._account_lister = account_lister or self._list_accounts_from_db
        self._granted: dict[str, set[str]] = {}
        self._creds: dict[str, object] = {}
        self._clients: dict[tuple[str, str], object] = {}

    # -- database -----------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.binding.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _list_accounts_from_db(self) -> list[str]:
        from db import list_gmail_accounts
        conn = self._open()
        try:
            return [a["account_id"] for a in list_gmail_accounts(conn, self.binding.user_id)]
        finally:
            conn.close()

    # -- accounts -----------------------------------------------------------

    def accounts(self) -> list[str]:
        return self._account_lister()

    def resolve_account(self, account: str | None) -> str:
        """Explicit argument, else the bound account, else the first connected."""
        connected = self.accounts()
        if not connected:
            raise ToolError("No Gmail account is connected. The user can connect one in Settings.")
        if account and account.strip():
            wanted = account.strip().lower()
            if wanted not in connected:
                raise ToolError(
                    f"{wanted} is not one of the connected accounts ({', '.join(connected)})."
                )
            return wanted
        if self.binding.account_id and self.binding.account_id in connected:
            return self.binding.account_id
        return connected[0]

    def _load(self, account: str):
        if account in self._granted:
            return
        conn = self._open()
        try:
            creds, granted, resolved = self._creds_provider(conn, self.binding.user_id, account)
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        finally:
            conn.close()
        self._creds[account] = creds
        self._granted[account] = set(granted)

    def granted(self, account: str) -> set[str]:
        self._load(account)
        return self._granted[account]

    def require(self, service: str, account: str | None) -> str:
        """Resolve the account and check it granted `service`. Returns the account."""
        resolved = self.resolve_account(account)
        accepted, label = SERVICE_SCOPES[service]
        if not any(s in self.granted(resolved) for s in accepted):
            raise ToolError(
                f"{resolved} has not granted {label} access. The user can grant it from "
                "Settings → Gmail → Grant agent access."
            )
        return resolved

    # -- clients ------------------------------------------------------------

    def _client(self, kind: str, account: str):
        key = (kind, account)
        if key not in self._clients:
            self._load(account)
            self._clients[key] = self._builder(kind, account, self._creds[account])
        return self._clients[key]

    def gmail(self, account: str): return self._client("gmail", account)
    def drive(self, account: str): return self._client("drive", account)
    def docs(self, account: str): return self._client("docs", account)
    def sheets(self, account: str): return self._client("sheets", account)
    def calendar(self, account: str): return self._client("calendar", account)
    def people(self, account: str): return self._client("people", account)
