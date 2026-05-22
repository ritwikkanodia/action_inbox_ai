import sqlite3
from datetime import datetime, timezone

import requests

from pollers.gmail.events import Actor, Actors, Content, GmailEvent, Metadata
from db import get_user_state, set_user_state, save_event

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DELTA_STATE_KEY = "outlook_delta_link"

_SELECT = ",".join([
    "id", "subject", "from", "toRecipients", "ccRecipients",
    "body", "bodyPreview", "receivedDateTime", "conversationId",
    "categories", "hasAttachments", "isDraft",
])


def _graph_get(token: str, url: str, params: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Prefer": 'outlook.body-content-type="text"',
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _build_event(msg: dict, user_id: str) -> GmailEvent:
    sender_addr = msg.get("from", {}).get("emailAddress", {})
    to_list = [r["emailAddress"]["address"] for r in msg.get("toRecipients", [])]
    cc_list = [r["emailAddress"]["address"] for r in msg.get("ccRecipients", [])]

    body_content = msg.get("body", {}).get("content", "") or msg.get("bodyPreview", "")

    return GmailEvent(
        event_id=f"evt_outlook_{msg['id']}",
        user_id=user_id,
        source="outlook",
        type="messagesAdded",
        timestamp=msg.get("receivedDateTime", datetime.now(timezone.utc).isoformat()),
        actors=Actors(
            from_=Actor(
                name=sender_addr.get("name", ""),
                email=sender_addr.get("address", ""),
            ) if sender_addr else None,
            to=to_list,
            cc=cc_list,
        ),
        content=Content(
            subject=msg.get("subject", ""),
            body_text=body_content,
            thread_id=msg.get("conversationId", ""),
            message_id=msg["id"],
            in_reply_to=None,
        ),
        metadata=Metadata(
            labels=msg.get("categories", []),
            is_reply=False,
            attachments=[],
        ),
        raw={"id": msg["id"], "conversationId": msg.get("conversationId")},
    )


def _bootstrap_delta_link(token: str) -> str:
    """Page through the inbox delta to get the current delta link without yielding messages."""
    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta"
    params: dict | None = {"$select": "id", "$top": 500}
    while True:
        data = _graph_get(token, url, params=params)
        delta_link = data.get("@odata.deltaLink")
        if delta_link:
            return delta_link
        url = data["@odata.nextLink"]
        params = None


def poll(token: str, conn: sqlite3.Connection, user_id: str) -> list[GmailEvent]:
    delta_link = get_user_state(conn, user_id, DELTA_STATE_KEY)

    if delta_link is None:
        current_delta = _bootstrap_delta_link(token)
        set_user_state(conn, user_id, DELTA_STATE_KEY, current_delta)
        return []

    events: list[GmailEvent] = []
    url: str = delta_link
    params: dict | None = {"$select": _SELECT}

    while True:
        data = _graph_get(token, url, params=params)

        for msg in data.get("value", []):
            if msg.get("isDraft") or msg.get("@removed"):
                continue
            evt = _build_event(msg, user_id)
            events.append(evt)
            save_event(conn, evt)

        next_link = data.get("@odata.nextLink")
        new_delta = data.get("@odata.deltaLink")

        if new_delta:
            set_user_state(conn, user_id, DELTA_STATE_KEY, new_delta)
            break
        url = next_link
        params = None

    return events


def fetch_conversation_messages(token: str, conversation_id: str) -> list[dict]:
    """Fetch all messages in a conversation, newest last, for the context panel."""
    url = f"{GRAPH_BASE}/me/messages"
    params = {
        "$filter": f"conversationId eq '{conversation_id}'",
        "$select": "id,subject,from,body,bodyPreview,receivedDateTime",
        "$orderby": "receivedDateTime asc",
        "$top": 20,
    }
    data = _graph_get(token, url, params=params)
    messages = []
    for msg in data.get("value", []):
        sender = msg.get("from", {}).get("emailAddress", {})
        body_content = msg.get("body", {}).get("content", "") or msg.get("bodyPreview", "")
        messages.append({
            "message_id": msg["id"],
            "from_name": sender.get("name", ""),
            "from_email": sender.get("address", ""),
            "body_text": body_content,
            "received_at": msg.get("receivedDateTime", ""),
        })
    return messages
