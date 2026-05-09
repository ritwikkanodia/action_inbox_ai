import json
import logging
from typing import Any

from agents import function_tool

from agent.db import open_db
from pollers.gmail.auth import get_gmail_service
from pollers.gmail.thread_context import build_thread_context, fetch_thread_messages

logger = logging.getLogger(__name__)


def _gmail_service(user_id: str):
    conn = open_db()
    try:
        return get_gmail_service(conn, user_id)
    finally:
        conn.close()


def fetch_gmail_thread_context(source_meta_json, user_id: str) -> str | None:
    """Used by the input builder to inject the linked thread on turn 1."""
    try:
        meta = json.loads(source_meta_json) if isinstance(source_meta_json, str) else source_meta_json
        thread_id = (meta or {}).get("thread_id")
        if not thread_id:
            return None
        service = _gmail_service(user_id)
        user_email = service.users().getProfile(userId="me").execute()["emailAddress"]
        messages = fetch_thread_messages(service, thread_id)
        return build_thread_context(messages, user_email)
    except Exception:
        logger.exception("Failed to fetch Gmail thread context")
        return None


def gmail_tools(user_id: str) -> list[Any]:
    @function_tool
    def search_email_threads(query: str) -> list[dict]:
        """Search the user's Gmail using a Gmail query string and return matching threads.

        The query supports the full Gmail search syntax, e.g.
        'from:alice@example.com newer_than:7d', 'subject:invoice', 'has:attachment'.
        Returns up to 10 threads, each with thread_id, subject, snippet, and from.
        Use fetch_email_thread(thread_id) to read the full conversation.
        """
        service = _gmail_service(user_id)
        resp = (
            service.users()
            .threads()
            .list(userId="me", q=query, maxResults=10)
            .execute()
        )
        results = []
        for t in resp.get("threads", []):
            tid = t["id"]
            try:
                meta = (
                    service.users()
                    .threads()
                    .get(userId="me", id=tid, format="metadata",
                         metadataHeaders=["Subject", "From"])
                    .execute()
                )
                first = (meta.get("messages") or [{}])[0]
                headers = first.get("payload", {}).get("headers", [])
                subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "")
                from_h = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
                results.append({
                    "thread_id": tid,
                    "subject": subject,
                    "from": from_h,
                    "snippet": t.get("snippet", ""),
                })
            except Exception:
                logger.exception("Failed to fetch metadata for thread %s", tid)
                results.append({"thread_id": tid, "snippet": t.get("snippet", "")})
        return results

    @function_tool
    def fetch_email_thread(thread_id: str) -> str:
        """Fetch the full formatted context of a Gmail thread by its thread_id.

        Returns the last few messages with sender, timestamp, and body, plus a note
        about whether the user has replied recently. Use this after search_email_threads
        to read the contents of a specific thread.
        """
        service = _gmail_service(user_id)
        user_email = service.users().getProfile(userId="me").execute()["emailAddress"]
        messages = fetch_thread_messages(service, thread_id)
        return build_thread_context(messages, user_email)

    return [search_email_threads, fetch_email_thread]
