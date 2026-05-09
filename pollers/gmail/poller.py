import base64
import sqlite3
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError as GApiHttpError

from pollers.gmail.events import Actor, Actors, Content, GmailEvent, Metadata
from db import (
    get_last_history_id,
    set_last_history_id,
    save_event,
    get_user_state,
    set_user_state,
    clear_user_state,
)

BACKFILL_PENDING_KEY = "gmail_backfill_pending"
BACKFILLED_EMAIL_KEY = "gmail_backfilled_email"

HISTORY_TYPES = ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]


def get_current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


def _parse_address(header_value: str) -> Actor:
    """Parse 'Name <email>' or bare 'email' into Actor."""
    if "<" in header_value:
        name, _, rest = header_value.partition("<")
        return Actor(name=name.strip(), email=rest.rstrip(">").strip())
    return Actor(name="", email=header_value.strip())


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _attachment_names(payload: dict) -> list[str]:
    names = []
    for part in payload.get("parts", []):
        if part.get("filename"):
            names.append(part["filename"])
    return names


def _extract_body(payload: dict) -> str:
    """Recursively find and decode the text/plain body from a MIME payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        result = _extract_body(part)
        if result:
            return result

    return ""


def _trim_raw(msg: dict) -> dict:
    """Strip large/redundant fields from raw — headers (parsed) and body data (extracted)."""
    import copy
    raw = copy.deepcopy(msg)

    def strip_payload(payload: dict) -> None:
        payload.pop("headers", None)
        if "body" in payload:
            payload["body"].pop("data", None)
        for part in payload.get("parts", []):
            strip_payload(part)

    if "payload" in raw:
        strip_payload(raw["payload"])

    return raw


def fetch_full_message(service, message_id: str) -> dict:
    return service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()


def _build_email_received(msg: dict, user_id: str, history_id: str) -> GmailEvent:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    from_header = _header(headers, "From")
    to_header = _header(headers, "To")
    cc_header = _header(headers, "Cc")
    subject = _header(headers, "Subject")
    in_reply_to = _header(headers, "In-Reply-To") or None

    from_actor = _parse_address(from_header) if from_header else None
    to_list = [a.strip() for a in to_header.split(",")] if to_header else []
    cc_list = [a.strip() for a in cc_header.split(",")] if cc_header else []

    labels = msg.get("labelIds", [])
    is_reply = bool(in_reply_to)
    attachments = _attachment_names(payload)

    timestamp = datetime.now(timezone.utc).isoformat()

    return GmailEvent(
        event_id=f"evt_{msg['id']}_messagesAdded",
        user_id=user_id,
        source="gmail",
        type="messagesAdded",
        timestamp=timestamp,
        actors=Actors(from_=from_actor, to=to_list, cc=cc_list),
        content=Content(
            subject=subject,
            body_text=_extract_body(payload) or msg.get("snippet", ""),
            thread_id=msg.get("threadId", ""),
            message_id=msg["id"],
            in_reply_to=in_reply_to,
        ),
        metadata=Metadata(labels=labels, is_reply=is_reply, attachments=attachments),
        raw=_trim_raw(msg),
    )


def _build_simple_event(
    event_type: str, msg: dict, labels: list[str], user_id: str
) -> GmailEvent:
    timestamp = datetime.now(timezone.utc).isoformat()
    return GmailEvent(
        event_id=f"evt_{msg['id']}_{event_type}",
        user_id=user_id,
        source="gmail",
        type=event_type,
        timestamp=timestamp,
        actors=Actors(from_=None, to=[], cc=[]),
        content=Content(
            subject="",
            body_text="",
            thread_id=msg.get("threadId", ""),
            message_id=msg["id"],
            in_reply_to=None,
        ),
        metadata=Metadata(labels=labels, is_reply=False, attachments=[]),
        raw=msg,
    )


def _backfill_messages(
    service, conn: sqlite3.Connection, user_id: str, days: int = 3
) -> list[GmailEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y/%m/%d")

    events: list[GmailEvent] = []
    page_token = None
    page_num = 0

    while True:
        kwargs: dict = {
            "userId": "me",
            "q": f"after:{cutoff_str} -in:sent -in:drafts",
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        page_num += 1
        stubs = response.get("messages", [])
        print(f"[gmail]   backfill: page {page_num} — fetching {len(stubs)} messages…")

        for msg_stub in stubs:
            try:
                full_msg = fetch_full_message(service, msg_stub["id"])
            except GApiHttpError as e:
                if e.resp.status == 404:
                    continue
                raise

            if set(full_msg.get("labelIds", [])) & {"SENT", "DRAFT"}:
                continue

            evt = _build_email_received(full_msg, user_id, "backfill")
            events.append(evt)
            save_event(conn, evt)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    print(f"[gmail]   backfill: fetched {len(events)} inbound messages — generating todos…")
    return events


def poll(service, conn: sqlite3.Connection, user_id: str) -> list[GmailEvent]:
    pending_email = get_user_state(conn, user_id, BACKFILL_PENDING_KEY)
    if pending_email:
        # Capture baseline before fetching so incremental polls pick up anything
        # that arrives during backfill (dedup handles overlap).
        current_id = get_current_history_id(service)
        print(f"[gmail] {pending_email}: backfill (3d window)")
        events = _backfill_messages(service, conn, user_id, days=3)
        set_last_history_id(conn, user_id, current_id)
        set_user_state(conn, user_id, BACKFILLED_EMAIL_KEY, pending_email)
        clear_user_state(conn, user_id, BACKFILL_PENDING_KEY)
        return events

    last_id = get_last_history_id(conn, user_id)

    if last_id is None:
        current_id = get_current_history_id(service)
        set_last_history_id(conn, user_id, current_id)
        return []

    events: list[GmailEvent] = []
    page_token = None
    max_history_id = last_id

    while True:
        kwargs = {
            "userId": "me",
            "startHistoryId": last_id,
            "historyTypes": HISTORY_TYPES,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().history().list(**kwargs).execute()
        history_records = response.get("history", [])

        for record in history_records:
            record_history_id = record["id"]
            if int(record_history_id) > int(max_history_id):
                max_history_id = record_history_id

            for item in record.get("messagesAdded", []):
                msg_stub = item["message"]

                # TODO: handle SENT and DRAFT events (follow-up tracking, unsent drafts)
                stub_labels = set(msg_stub.get("labelIds", []))
                if stub_labels & {"SENT", "DRAFT"}:
                    continue

                try:
                    full_msg = fetch_full_message(service, msg_stub["id"])
                except GApiHttpError as e:
                    if e.resp.status == 404:
                        continue
                    raise

                e = _build_email_received(full_msg, user_id, record_history_id)
                events.append(e)
                save_event(conn, e)

            for item in record.get("messagesDeleted", []):
                msg = item["message"]
                e = _build_simple_event("messagesDeleted", msg, msg.get("labelIds", []), user_id)
                events.append(e)
                save_event(conn, e)

            for item in record.get("labelsAdded", []):
                msg = item["message"]
                e = _build_simple_event("labelsAdded", msg, item.get("labelIds", []), user_id)
                events.append(e)
                save_event(conn, e)

            for item in record.get("labelsRemoved", []):
                msg = item["message"]
                e = _build_simple_event("labelsRemoved", msg, item.get("labelIds", []), user_id)
                events.append(e)
                save_event(conn, e)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if max_history_id != last_id:
        set_last_history_id(conn, user_id, max_history_id)

    return events
