"""The tool functions, as plain Python closures over a `Services`.

`build_tools(svc)` returns callables whose names, signatures and docstrings are
what the MCP layer exposes — no MCP types here, so another executor can wrap
the same functions. Every tool returns a string; `_safe` turns any failure into
a one-line `Error: …` result so Hermes always sees an ordinary tool reply.
"""

import base64
import functools
import io
import json
import logging
import re
from email.message import EmailMessage
from typing import Callable

import google_scopes
from agent.google_mcp.services import SERVICE_SCOPES, Services, ToolError
from pollers.gmail.thread_context import _extract_body, _header

logger = logging.getLogger(__name__)

READ_CAP = 100_000

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


def _cap(text: str) -> str:
    if len(text) <= READ_CAP:
        return text
    return text[:READ_CAP] + f"\n\n[truncated: {len(text) - READ_CAP} more characters not shown]"


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


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

    # -- Drive --------------------------------------------------------------

    @register
    def drive_search(query: str, max_results: int = 10, account: str | None = None) -> str:
        """Search the user's Google Drive by words in the name or content. Returns id,
        name, mimeType, modifiedTime and webViewLink. Read one with drive_read_file."""
        acct = svc.require("drive", account)
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        resp = svc.drive(acct).files().list(
            q=f"(fullText contains '{safe}' or name contains '{safe}') and trashed = false",
            pageSize=max_results, orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,modifiedTime,webViewLink)",
        ).execute()
        return _dumps(resp.get("files", []))

    @register
    def drive_read_file(file_id: str, account: str | None = None) -> str:
        """Read a Drive file as text: Google Docs and Slides as plain text, Sheets as
        CSV, PDFs and text files as their text. Long files are truncated at 100k chars."""
        acct = svc.require("drive", account)
        drive = svc.drive(acct)
        meta = drive.files().get(fileId=file_id, fields="id,name,mimeType").execute()
        mime = meta.get("mimeType", "")
        if mime in _EXPORT:
            data = drive.files().export(fileId=file_id, mimeType=_EXPORT[mime]).execute()
            return _cap(data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data))
        if mime == "application/pdf" or mime.startswith("text/"):
            data = drive.files().get_media(fileId=file_id).execute()
            if isinstance(data, str):
                data = data.encode("utf-8")
            text = _pdf_text(data) if mime == "application/pdf" else data.decode("utf-8", errors="replace")
            return _cap(text)
        return (f"Cannot read {meta.get('name')!r}: type {mime} is not exportable as text. "
                "Open it in the browser if its contents are needed.")

    @register
    def drive_upload_file(name: str, content: str, mime_type: str = "text/plain",
                          folder_id: str | None = None, account: str | None = None) -> str:
        """Create a file in the user's Drive from text content. Returns id and webViewLink."""
        acct = svc.require("drive", account)
        from googleapiclient.http import MediaInMemoryUpload
        body: dict = {"name": name}
        if folder_id:
            body["parents"] = [folder_id]
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type)
        created = svc.drive(acct).files().create(
            body=body, media_body=media, fields="id,webViewLink").execute()
        return _dumps({"id": created["id"], "webViewLink": created.get("webViewLink")})

    # -- Docs ---------------------------------------------------------------

    def _doc_url(document_id: str) -> str:
        return f"https://docs.google.com/document/d/{document_id}/edit"

    @register
    def docs_create(title: str, body_text: str = "", account: str | None = None) -> str:
        """Create a Google Doc with a title and optional body text. Returns id and URL."""
        acct = svc.require("docs", account)
        docs = svc.docs(acct)
        doc = docs.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]
        if body_text:
            docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
                {"insertText": {"location": {"index": 1}, "text": body_text}}]}).execute()
        return _dumps({"document_id": doc_id, "url": _doc_url(doc_id)})

    @register
    def docs_append(document_id: str, text: str, account: str | None = None) -> str:
        """Append text at the end of an existing Google Doc."""
        acct = svc.require("docs", account)
        docs = svc.docs(acct)
        doc = docs.documents().get(documentId=document_id, fields="body.content.endIndex").execute()
        content = doc.get("body", {}).get("content", [])
        # The body always ends with a newline the API won't let you write after.
        end = max((c.get("endIndex", 1) for c in content), default=1) - 1
        docs.documents().batchUpdate(documentId=document_id, body={"requests": [
            {"insertText": {"location": {"index": max(end, 1)}, "text": text}}]}).execute()
        return _dumps({"document_id": document_id, "url": _doc_url(document_id), "appended": len(text)})

    @register
    def docs_replace_text(document_id: str, find: str, replace: str, account: str | None = None) -> str:
        """Replace every case-sensitive occurrence of `find` in a Google Doc. Returns the count."""
        acct = svc.require("docs", account)
        resp = svc.docs(acct).documents().batchUpdate(documentId=document_id, body={"requests": [
            {"replaceAllText": {"containsText": {"text": find, "matchCase": True},
                                "replaceText": replace}}]}).execute()
        replies = resp.get("replies") or [{}]
        count = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        return f"Replaced {count} occurrence(s) in {_doc_url(document_id)}"

    # Task 6 adds the Sheets/Calendar/Contacts tools here.

    return tools
