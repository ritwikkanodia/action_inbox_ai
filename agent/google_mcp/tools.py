"""The tool functions, as plain Python closures over a `Services`.

`build_tools(svc)` returns callables whose names, signatures and docstrings are
what the MCP layer exposes — no MCP types here, so another executor can wrap
the same functions. Every tool returns a string; `_safe` turns any failure into
a one-line `Error: …` result so Hermes always sees an ordinary tool reply.
"""

import base64
import functools
import json
import logging
import re
from email.message import EmailMessage
from typing import Callable

import google_scopes
from agent.google_mcp.services import SERVICE_SCOPES, Services, ToolError
from pollers.gmail.thread_context import _extract_body, _header

logger = logging.getLogger(__name__)


def _one_line(text: str) -> str:
    """Collapse every run of whitespace, including newlines, into a single space —
    keeps an `Error: ...` result to the one-line contract regardless of what the
    underlying message looked like."""
    return re.sub(r"\s+", " ", text).strip()


def _safe(fn: Callable) -> Callable:
    """Never raise across the MCP boundary: every failure is a tool result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError as exc:
            return f"Error: {_one_line(str(exc))}"
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
        return _one_line(f"Google API returned {status}: {message}")
    return _one_line(f"{type(exc).__name__}: {exc}")


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

    # -- Gmail --------------------------------------------------------------

    def _thread_headers(gmail, thread_id: str) -> tuple[str, str, str]:
        """(subject, last Message-ID, References) from a thread's last message."""
        thread = gmail.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["Subject", "Message-ID", "References"],
        ).execute()
        messages = thread.get("messages") or []
        if not messages:
            raise ToolError(f"Thread {thread_id} has no messages.")
        headers = messages[-1].get("payload", {}).get("headers", [])
        subject = _header(headers, "Subject")
        message_id = _header(headers, "Message-ID")
        references = (_header(headers, "References") + " " + message_id).strip()
        return subject, message_id, references

    def _build_message(gmail, to, subject, body, cc, reply_to_thread_id) -> dict:
        """The Gmail API `message` resource for a send or a draft."""
        msg = EmailMessage()
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        payload: dict = {}
        if reply_to_thread_id:
            orig_subject, message_id, references = _thread_headers(gmail, reply_to_thread_id)
            if not subject:
                subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
            if message_id:
                msg["In-Reply-To"] = message_id
                msg["References"] = references
            payload["threadId"] = reply_to_thread_id
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body)
        payload["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return payload

    @register
    def gmail_search_threads(query: str, max_results: int = 10, account: str | None = None) -> str:
        """Search the user's Gmail with Gmail query syntax (e.g. 'from:alice newer_than:7d',
        'subject:invoice', 'has:attachment'). Returns threads with thread_id, subject,
        from, date and snippet. Use gmail_read_thread to read one in full."""
        acct = svc.require("gmail_read", account)
        gmail = svc.gmail(acct)
        resp = gmail.users().threads().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for t in resp.get("threads", []):
            meta = gmail.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()
            messages = meta.get("messages") or [{}]
            headers = messages[-1].get("payload", {}).get("headers", [])
            out.append({
                "thread_id": t["id"],
                "subject": _header(headers, "Subject"),
                "from": _header(headers, "From"),
                "date": _header(headers, "Date"),
                "snippet": t.get("snippet", ""),
            })
        return _dumps(out)

    @register
    def gmail_read_thread(thread_id: str, account: str | None = None) -> str:
        """Read every message in a Gmail thread: from, to, date, subject and body text."""
        acct = svc.require("gmail_read", account)
        thread = svc.gmail(acct).users().threads().get(
            userId="me", id=thread_id, format="full").execute()
        parts = []
        for m in thread.get("messages", []):
            payload = m.get("payload", {})
            headers = payload.get("headers", [])
            body = _extract_body(payload) or m.get("snippet", "")
            parts.append(
                f"From: {_header(headers, 'From')}\nTo: {_header(headers, 'To')}\n"
                f"Date: {_header(headers, 'Date')}\nSubject: {_header(headers, 'Subject')}\n\n{body}"
            )
        return "\n\n---\n\n".join(parts) or f"Thread {thread_id} has no messages."

    @register
    def gmail_create_draft(to: str, subject: str, body: str, cc: str | None = None,
                           reply_to_thread_id: str | None = None,
                           account: str | None = None) -> str:
        """Create a Gmail draft (not sent). With reply_to_thread_id it is threaded as a
        reply and the subject defaults to 'Re: <original>'. Use this when the user asked
        for a draft or when any part of the content is inferred rather than confirmed."""
        acct = svc.require("gmail", account)
        gmail = svc.gmail(acct)
        message = _build_message(gmail, to, subject, body, cc, reply_to_thread_id)
        draft = gmail.users().drafts().create(userId="me", body={"message": message}).execute()
        return _dumps({"draft_id": draft["id"],
                       "url": f"https://mail.google.com/mail/u/0/#drafts?compose={draft['id']}"})

    @register
    def gmail_send(to: str, subject: str, body: str, cc: str | None = None,
                   reply_to_thread_id: str | None = None,
                   account: str | None = None) -> str:
        """Send an email from the user's account. Irreversible: only when the user asked
        for this message to this recipient and every input is confirmed. With
        reply_to_thread_id it is sent as a threaded reply."""
        acct = svc.require("gmail", account)
        gmail = svc.gmail(acct)
        message = _build_message(gmail, to, subject, body, cc, reply_to_thread_id)
        sent = gmail.users().messages().send(userId="me", body=message).execute()
        return _dumps({"message_id": sent["id"], "thread_id": sent.get("threadId")})

    # Tasks 5-6 add the Drive/Docs and Sheets/Calendar/Contacts tools here.

    return tools
