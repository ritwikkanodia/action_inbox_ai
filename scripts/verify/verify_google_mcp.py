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


def check_drive_docs() -> None:
    print("\n-- drive / docs --")
    drive = Fake(responses={
        "list": {"files": [{"id": "f1", "name": "Plan", "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-09-01T00:00:00Z", "webViewLink": "https://docs/f1"}]},
        "get": {"id": "f1", "name": "Plan", "mimeType": "application/vnd.google-apps.document"},
        "export": b"x" * 150_000,
        "create": {"id": "up1", "webViewLink": "https://drive/up1"},
    })
    docs = Fake(responses={
        "create": {"documentId": "doc1"},
        "get": {"body": {"content": [{"endIndex": 1}, {"endIndex": 42}]}},
        "batchUpdate": {"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]},
    })
    svc, _ = make_services({"b@example.com": FULL},
                           {("drive", "b@example.com"): drive, ("docs", "b@example.com"): docs})
    t = tools_for(svc)

    out = json.loads(t["drive_search"]("plan"))
    q = drive.last("list")["q"]
    check("drive_search uses fullText and name", "fullText contains 'plan'" in q and "name contains 'plan'" in q)
    check("drive_search returns id, name, type, link", out[0]["id"] == "f1" and out[0]["webViewLink"] == "https://docs/f1")

    out = t["drive_read_file"]("f1")
    check("Google Doc exported as text/plain", drive.last("export")["mimeType"] == "text/plain")
    check("read capped at 100k with a note", len(out) < 100_200 and "truncated" in out.lower())

    drive.responses["get"] = {"id": "s1", "name": "Sheet", "mimeType": "application/vnd.google-apps.spreadsheet"}
    drive.responses["export"] = b"a,b\n1,2"
    out = t["drive_read_file"]("s1")
    check("Google Sheet exported as CSV", drive.last("export")["mimeType"] == "text/csv" and out == "a,b\n1,2")

    drive.responses["get"] = {"id": "z1", "name": "img.png", "mimeType": "image/png"}
    out = t["drive_read_file"]("z1")
    check("unsupported type named, not dumped", "image/png" in out and "cannot" in out.lower())

    out = json.loads(t["drive_upload_file"]("notes.txt", "hello", folder_id="fold1"))
    body = drive.last("create")["body"]
    check("upload names the file and parent", body["name"] == "notes.txt" and body["parents"] == ["fold1"])
    check("upload reports link", out["webViewLink"] == "https://drive/up1")

    out = json.loads(t["docs_create"]("Title", "First line"))
    ins = docs.last("batchUpdate")["body"]["requests"][0]["insertText"]
    check("docs_create inserts body at index 1", ins["text"] == "First line" and ins["location"]["index"] == 1)
    check("docs_create reports url", out["document_id"] == "doc1" and out["url"].endswith("/doc1/edit"))

    t["docs_append"]("doc1", "More")
    ins = docs.last("batchUpdate")["body"]["requests"][0]["insertText"]
    check("docs_append inserts before the final newline", ins["location"]["index"] == 41)

    out = t["docs_replace_text"]("doc1", "old", "new")
    rep = docs.last("batchUpdate")["body"]["requests"][0]["replaceAllText"]
    check("replace is case-sensitive and counts", rep["containsText"]["matchCase"] is True and "3" in out)


def check_sheets_calendar_contacts() -> None:
    print("\n-- sheets / calendar / contacts --")
    sheets = Fake(responses={"get": {"values": [["a", "b"], ["1", "2"]]},
                             "update": {"updatedCells": 4},
                             "append": {"updates": {"updatedRows": 2}}})
    cal = Fake(responses={"list": {"items": [{"id": "e1", "summary": "Standup",
                                              "start": {"dateTime": "2026-09-21T09:00:00+05:30"},
                                              "end": {"dateTime": "2026-09-21T09:15:00+05:30"},
                                              "attendees": [{"email": "x@example.com"}],
                                              "htmlLink": "https://cal/e1"}]},
                          "insert": {"id": "e2", "htmlLink": "https://cal/e2"}})
    people = Fake(responses={
        "searchContacts": {"results": [{"person": {"names": [{"displayName": "Alice A"}],
                                                   "emailAddresses": [{"value": "alice@example.com"}]}}]},
        "search": {"results": [{"person": {"names": [{"displayName": "Bob"}],
                                           "emailAddresses": [{"value": "bob@example.com"}]}}]},
    })
    svc, _ = make_services({"b@example.com": FULL}, {("sheets", "b@example.com"): sheets,
                                                     ("calendar", "b@example.com"): cal,
                                                     ("people", "b@example.com"): people})
    t = tools_for(svc)

    out = json.loads(t["sheets_read_range"]("sid", "Sheet1!A1:B2"))
    check("sheets_read_range returns rows", out == [["a", "b"], ["1", "2"]])
    t["sheets_write_range"]("sid", "Sheet1!A1:B2", [["x", "y"], ["3", "4"]])
    kw = sheets.last("update")
    check("write uses USER_ENTERED", kw["valueInputOption"] == "USER_ENTERED" and kw["body"]["values"][0] == ["x", "y"])
    t["sheets_append_rows"]("sid", "Sheet1!A:B", [["5", "6"]])
    kw = sheets.last("append")
    check("append inserts rows", kw["insertDataOption"] == "INSERT_ROWS" and kw["valueInputOption"] == "USER_ENTERED")

    out = json.loads(t["calendar_list_events"]("2026-09-21T00:00:00Z", "2026-09-22T00:00:00Z", query="stand"))
    kw = cal.last("list")
    check("list_events bounds, query, single expanded events",
          kw["timeMin"].startswith("2026-09-21") and kw["q"] == "stand" and kw["singleEvents"] is True)
    check("list_events shape", out[0]["id"] == "e1" and out[0]["attendees"] == ["x@example.com"])

    out = json.loads(t["calendar_create_event"]("Coffee", "2026-09-22T10:00:00+05:30", "2026-09-22T10:30:00+05:30",
                                                attendees=["x@example.com"], location="Cafe"))
    kw = cal.last("insert")
    check("create_event body and invitations",
          kw["body"]["summary"] == "Coffee" and kw["body"]["attendees"] == [{"email": "x@example.com"}]
          and kw["sendUpdates"] == "all" and out["id"] == "e2")
    t["calendar_create_event"]("Solo", "2026-09-22", "2026-09-23")
    kw = cal.last("insert")
    check("all-day event uses date, no invitations",
          kw["body"]["start"] == {"date": "2026-09-22"} and kw["sendUpdates"] == "none")

    out = json.loads(t["contacts_search"]("ali"))
    check("contacts merges contacts and other contacts",
          {c["email"] for c in out} == {"alice@example.com", "bob@example.com"})
    check("contacts warmed the cache first", people.calls[0][0] == "people" and people.calls[1][0] == "searchContacts"
          and people.calls[1][1]["query"] == "")


if __name__ == "__main__":
    check_binding()
    check_server_shape()
    check_accounts_and_gating()
    check_gmail()
    check_drive_docs()
    check_sheets_calendar_contacts()
    print("\nAll checks passed.")
