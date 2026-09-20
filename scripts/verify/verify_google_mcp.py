"""Verifies the Google Workspace MCP server without Hermes, Google, or the network.

The server is what Hermes talks to; these checks cover the contract around it:
no binding means no tools; the binding picks the account; a missing scope is an
error string before any API call; and each tool turns API failures into a plain
`Error:` line instead of raising. Google clients are replaced with `Fake`, which
records the fluent call chain and returns a canned response.

Usage: python scripts/verify/verify_google_mcp.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "mcp.db")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

from googleapiclient.errors import HttpError

import google_scopes
from db import init_db, set_source_credentials, upsert_user
from agent.google_mcp import services as S
from agent.google_mcp.__main__ import make_server


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


READONLY = "https://www.googleapis.com/auth/gmail.readonly"
FULL = set(google_scopes.ALL_SCOPES)


class Fake:
    """Stands in for a googleapiclient resource. Any attribute is a method that
    records (name, kwargs) and returns the same Fake; execute() returns the
    canned response for the *last* recorded method name, or raises."""

    def __init__(self, responses=None, raise_exc=None):
        self.responses = responses or {}
        self.raise_exc = raise_exc
        self.calls = []

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, kwargs))
            return self
        return method

    def execute(self, **kwargs):
        if self.raise_exc:
            raise self.raise_exc
        last = self.calls[-1][0] if self.calls else None
        return self.responses.get(last, self.responses.get("*", {}))

    def last(self, name):
        for n, kw in reversed(self.calls):
            if n == name:
                return kw
        return None


class FakeResp:
    def __init__(self, status): self.status = status; self.reason = "boom"


def http_error(status=403):
    return HttpError(FakeResp(status), b'{"error": {"message": "denied"}}')


def http_error_multiline(status=500):
    return HttpError(FakeResp(status), b'{"error": {"message": "Internal error.\\nPlease try again."}}')


def make_binding(account="a@example.com"):
    return S.Binding(user_id="u1", account_id=account, db_path=os.environ["DB_PATH"])


def make_services(grants: dict, clients: dict | None = None):
    """grants: account -> set of scopes. clients: (kind, account) -> Fake."""
    clients = clients or {}
    order = list(grants)

    def creds_provider(conn, user_id, account_id):
        acct = account_id or order[0]
        if acct not in grants:
            raise RuntimeError(f"Gmail not connected ({acct}).")
        return object(), set(grants[acct]), acct

    def builder(kind, account, creds):
        return clients.setdefault((kind, account), Fake())

    svc = S.Services(make_binding(order[0]), creds_provider=creds_provider, builder=builder,
                     account_lister=lambda: order)
    return svc, clients


def check_binding() -> None:
    print("\n-- binding --")
    check("no env → None", S.binding_from_env({}) is None)
    check("blank → None", S.binding_from_env({"AIB_USER_ID": " "}) is None)
    check("unexpanded ${AIB_USER_ID} → None",
          S.binding_from_env({"AIB_USER_ID": "${AIB_USER_ID}", "AIB_DB_PATH": "x"}) is None)
    b = S.binding_from_env({"AIB_USER_ID": "u1", "AIB_ACCOUNT_ID": "${AIB_ACCOUNT_ID}",
                            "AIB_DB_PATH": "/tmp/x.db"})
    check("bound with an unexpanded account → empty account", b == S.Binding("u1", "", "/tmp/x.db"))
    b = S.binding_from_env({"AIB_USER_ID": "u1", "AIB_ACCOUNT_ID": "A@Example.com"})
    check("account lowercased, db path defaults to DB_PATH env",
          b.account_id == "a@example.com" and b.db_path == os.environ["DB_PATH"])


def check_server_shape() -> None:
    print("\n-- server --")
    tools = asyncio.run(make_server(None).list_tools())
    check("no binding → zero tools", tools == [])
    server = make_server(make_binding())
    names = {t.name for t in asyncio.run(server.list_tools())}
    check("bound → google_accounts registered", "google_accounts" in names)


def check_accounts_and_gating() -> None:
    print("\n-- account resolution and scope gating --")
    svc, clients = make_services({"a@example.com": {READONLY}, "b@example.com": FULL})
    check("explicit account wins", svc.resolve_account("b@example.com") == "b@example.com")
    check("default is the bound account", svc.resolve_account(None) == "a@example.com")
    check("explicit account is case-insensitive", svc.resolve_account("B@Example.com") == "b@example.com")
    try:
        svc.resolve_account("zzz@example.com")
        check("unknown account is a ToolError", False)
    except S.ToolError as exc:
        check("unknown account is a ToolError", "not one of the connected" in str(exc))

    check("gmail_read accepts readonly", svc.require("gmail_read", "a@example.com") == "a@example.com")
    try:
        svc.require("gmail", "a@example.com")
        check("gmail send on a readonly account is gated", False)
    except S.ToolError as exc:
        check("gmail send on a readonly account is gated",
              "a@example.com has not granted Gmail access" in str(exc)
              and "Grant agent access" in str(exc))
    check("drive granted on full account", svc.require("drive", "b@example.com") == "b@example.com")

    svc_empty, _ = make_services({"a@example.com": {READONLY}})
    svc_empty.binding = S.Binding("u1", "", os.environ["DB_PATH"])
    check("empty bound account falls back to first connected",
          svc_empty.resolve_account(None) == "a@example.com")

    tools = {t.__name__: t for t in __import__("agent.google_mcp.tools", fromlist=["x"]).build_tools(svc)}
    out = json.loads(tools["google_accounts"]())
    check("google_accounts lists both with access flags",
          [a["email"] for a in out] == ["a@example.com", "b@example.com"]
          and out[0]["agent_access"] is False and out[1]["agent_access"] is True
          and "Gmail (read)" in out[0]["services"] and "Drive" in out[1]["services"])
    check("google_accounts marks the default", out[0]["default"] is True and out[1]["default"] is False)


def tools_for(svc):
    from agent.google_mcp.tools import build_tools
    return {t.__name__: t for t in build_tools(svc)}


def check_gmail() -> None:
    print("\n-- gmail --")
    import base64
    from email import message_from_bytes

    thread_full = {"messages": [{
        "id": "m1", "internalDate": "1700000000000", "snippet": "hi",
        "payload": {"mimeType": "text/plain",
                    "headers": [{"name": "From", "value": "Alice <alice@example.com>"},
                                {"name": "To", "value": "b@example.com"},
                                {"name": "Subject", "value": "Booth deposit"},
                                {"name": "Date", "value": "Tue, 14 Nov 2023 22:13:20 +0000"},
                                {"name": "Message-ID", "value": "<abc@mail>"}],
                    "body": {"data": base64.urlsafe_b64encode(b"Please pay by Friday").decode()}}}]}
    gmail = Fake(responses={
        "list": {"threads": [{"id": "t1", "snippet": "hi"}]},
        "get": thread_full,
        "send": {"id": "sent1", "threadId": "t1"},
        "create": {"id": "d1", "message": {"id": "dm1", "threadId": "t1"}},
    })
    svc, _ = make_services({"b@example.com": FULL}, {("gmail", "b@example.com"): gmail})
    t = tools_for(svc)

    out = json.loads(t["gmail_search_threads"]("from:alice"))
    check("search returns thread id, subject, from, snippet",
          out[0]["thread_id"] == "t1" and out[0]["subject"] == "Booth deposit"
          and out[0]["from"] == "Alice <alice@example.com>" and out[0]["snippet"] == "hi")
    check("search passed the query and cap", gmail.last("list")["q"] == "from:alice"
          and gmail.last("list")["maxResults"] == 10)

    out = t["gmail_read_thread"]("t1")
    check("read_thread renders from/to/date/subject/body",
          "Alice <alice@example.com>" in out and "b@example.com" in out
          and "Booth deposit" in out and "Please pay by Friday" in out)

    out = json.loads(t["gmail_send"]("alice@example.com", "", "On it.", reply_to_thread_id="t1"))
    raw = gmail.last("send")["body"]
    msg = message_from_bytes(base64.urlsafe_b64decode(raw["raw"] + "=="))
    check("reply threads onto the Gmail thread", raw["threadId"] == "t1")
    check("reply sets In-Reply-To and References",
          msg["In-Reply-To"] == "<abc@mail>" and msg["References"] == "<abc@mail>")
    check("reply subject defaults to Re: original", msg["Subject"] == "Re: Booth deposit")
    check("send reports ids", out["message_id"] == "sent1" and out["thread_id"] == "t1")

    out = json.loads(t["gmail_create_draft"]("x@example.com", "Hello", "Body", cc="c@example.com"))
    draft = gmail.last("create")["body"]
    msg = message_from_bytes(base64.urlsafe_b64decode(draft["message"]["raw"] + "=="))
    check("draft is a fresh message with cc", "threadId" not in draft["message"]
          and msg["Cc"] == "c@example.com" and msg["Subject"] == "Hello")
    check("draft reports id and url", out["draft_id"] == "d1" and "mail.google.com" in out["url"])

    ro, _ = make_services({"r@example.com": {READONLY}})
    tr = tools_for(ro)
    check("read on readonly account works", not tr["gmail_search_threads"]("x").startswith("Error"))
    check("send on readonly account is gated",
          tr["gmail_send"]("x@example.com", "s", "b").startswith("Error: r@example.com has not granted Gmail access"))

    broken = Fake(raise_exc=http_error(403))
    svc2, _ = make_services({"b@example.com": FULL}, {("gmail", "b@example.com"): broken})
    out = tools_for(svc2)["gmail_search_threads"]("x")
    check("HttpError becomes an Error line", out.startswith("Error: Google API returned 403"))

    multiline = Fake(raise_exc=http_error_multiline(500))
    svc3, _ = make_services({"b@example.com": FULL}, {("gmail", "b@example.com"): multiline})
    out = tools_for(svc3)["gmail_search_threads"]("x")
    check("HttpError with a multi-line message still yields a single-line Error",
          out.startswith("Error: Google API returned 500") and "\n" not in out)


if __name__ == "__main__":
    check_binding()
    check_server_shape()
    check_accounts_and_gating()
    check_gmail()
    print("\nAll checks passed.")
