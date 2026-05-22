"""Morning email digest — re-engagement hook sent once per day at 10am local.

Intentionally minimal: counts + a few teased titles + a CTA back to the app.
The email's job is to pull the user back in, not to summarize their inbox.
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from html import escape

from db import get_user_state, set_user_state


log = logging.getLogger(__name__)

SEND_HOUR_LOCAL = 10
TEASER_LIMIT = 3
TITLE_MAX_CHARS = 60
LAST_SENT_KEY = "digest_last_sent_date"


def _base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")


def _truncate(s: str, n: int = TITLE_MAX_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_name(user: dict) -> str:
    name = (user.get("name") or "").strip()
    if name:
        return name.split()[0]
    email = (user.get("email") or "").strip()
    return email.split("@")[0] if email else "there"


def _fetch_buckets(conn: sqlite3.Connection, user_id: str, now_local: datetime) -> dict:
    today_end = now_local.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    yesterday_start = (now_local - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    awaiting_rows = conn.execute(
        """
        SELECT todo_id, title
        FROM todos
        WHERE user_id = ?
          AND status = 'open'
          AND decision IS NULL
          AND title IS NOT NULL AND title != ''
        ORDER BY
            CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
            due_date ASC,
            CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
            created_at DESC
        """,
        (user_id,),
    ).fetchall()

    urgent_rows = conn.execute(
        """
        SELECT todo_id, title
        FROM todos
        WHERE user_id = ?
          AND status IN ('open', 'ongoing')
          AND decision = 'accepted'
          AND title IS NOT NULL AND title != ''
          AND (urgency = 'high' OR (due_date IS NOT NULL AND due_date <= ?))
        ORDER BY
            CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
            due_date ASC,
            CASE urgency WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
            created_at DESC
        """,
        (user_id, today_end),
    ).fetchall()

    closed_yesterday_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM todos
        WHERE user_id = ?
          AND status = 'closed'
          AND updated_at >= ?
          AND updated_at < ?
        """,
        (user_id, yesterday_start, today_start),
    ).fetchone()[0]

    def shape(rows):
        return {
            "count": len(rows),
            "teasers": [
                {"id": r[0], "title": _truncate(r[1])} for r in rows[:TEASER_LIMIT]
            ],
        }

    return {
        "awaiting": shape(awaiting_rows),
        "urgent": shape(urgent_rows),
        "closed_yesterday": closed_yesterday_count,
    }


def _render(user: dict, buckets: dict, base_url: str) -> tuple[str, str, str]:
    """Return (subject, html, text)."""
    name = _first_name(user)
    inbox_url = f"{base_url}/"
    today_str = datetime.now().strftime("%a, %b %-d")

    awaiting = buckets["awaiting"]
    urgent = buckets["urgent"]
    closed_y = buckets["closed_yesterday"]

    inbox_zero = awaiting["count"] == 0 and urgent["count"] == 0

    # ---- subject ----
    if inbox_zero:
        subject = f"Inbox zero — {today_str}"
    elif awaiting["count"] > 0:
        n = awaiting["count"]
        subject = f"{n} suggestion{'s' if n != 1 else ''} waiting for you"
    else:
        n = urgent["count"]
        subject = f"{n} urgent todo{'s' if n != 1 else ''} today"

    # ---- HTML ----
    def section_html(label: str, bucket: dict, all_url: str) -> str:
        if bucket["count"] == 0:
            return ""
        items = "".join(
            f'<li style="margin:6px 0;"><a href="{base_url}/todos/{escape(t["id"])}" '
            f'style="color:#1a73e8;text-decoration:none;">{escape(t["title"])}</a></li>'
            for t in bucket["teasers"]
        )
        more = ""
        if bucket["count"] > len(bucket["teasers"]):
            extra = bucket["count"] - len(bucket["teasers"])
            more = (
                f'<p style="margin:8px 0 0 0;font-size:13px;">'
                f'<a href="{all_url}" style="color:#666;">+{extra} more — review them all →</a>'
                f"</p>"
            )
        return f"""
        <div style="margin:28px 0;">
          <h2 style="font-size:14px;text-transform:uppercase;letter-spacing:.5px;color:#666;margin:0 0 10px 0;">
            {escape(label)} · {bucket["count"]}
          </h2>
          <ul style="list-style:none;padding:0;margin:0;font-size:15px;line-height:1.5;">{items}</ul>
          {more}
        </div>
        """

    if inbox_zero:
        hero = (
            f'<p style="font-size:18px;margin:0 0 24px 0;">'
            f"Inbox zero. Nice work, {escape(name)}."
            f"</p>"
        )
        body = ""
    else:
        if awaiting["count"] > 0:
            n = awaiting["count"]
            lede = (
                f"You have <strong>{n}</strong> suggestion{'s' if n != 1 else ''} "
                f"waiting for your call."
            )
        else:
            n = urgent["count"]
            lede = (
                f"<strong>{n}</strong> urgent todo{'s' if n != 1 else ''} on your plate today."
            )
        hero = (
            f'<p style="font-size:18px;margin:0 0 16px 0;">'
            f"Morning, {escape(name)}. {lede}"
            f"</p>"
        )
        body = (
            section_html("Awaiting your decision", awaiting, inbox_url)
            + section_html("Urgent today", urgent, inbox_url)
        )

    footer = ""
    if closed_y > 0:
        plural = "s" if closed_y != 1 else ""
        footer = (
            '<div style="margin:36px 0 0 0;padding:18px 20px;background:#ecfdf5;'
            'border-left:4px solid #10b981;border-radius:6px;">'
            '<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.6px;color:#059669;margin-bottom:6px;">'
            "🎉 Yesterday&rsquo;s win"
            "</div>"
            '<div style="font-size:17px;color:#064e3b;line-height:1.45;">'
            f'You closed <strong style="font-size:22px;color:#047857;">{closed_y}</strong> '
            f"todo{plural} yesterday. Keep the streak going!"
            "</div>"
            "</div>"
        )

    cta = (
        f'<p style="margin:24px 0;">'
        f'<a href="{inbox_url}" style="display:inline-block;background:#1a73e8;color:#fff;'
        f'padding:12px 22px;border-radius:6px;text-decoration:none;font-weight:600;">'
        f"Open your inbox</a></p>"
    )

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:560px;margin:0 auto;padding:32px 24px;color:#222;">
  {hero}
  {cta}
  {body}
  {footer}
</body></html>"""

    # ---- plain text ----
    lines = []
    if inbox_zero:
        lines.append(f"Inbox zero. Nice work, {name}.")
    else:
        lines.append(f"Morning, {name}.")
        if awaiting["count"]:
            lines.append("")
            lines.append(f"Awaiting your decision ({awaiting['count']}):")
            for t in awaiting["teasers"]:
                lines.append(f"  • {t['title']}")
                lines.append(f"    {base_url}/todos/{t['id']}")
        if urgent["count"]:
            lines.append("")
            lines.append(f"Urgent today ({urgent['count']}):")
            for t in urgent["teasers"]:
                lines.append(f"  • {t['title']}")
                lines.append(f"    {base_url}/todos/{t['id']}")
    lines.append("")
    lines.append(f"Open your inbox: {inbox_url}")
    if closed_y > 0:
        lines.append("")
        lines.append(f"You closed {closed_y} todo(s) yesterday.")
    text = "\n".join(lines)

    return subject, html, text


def poll(conn: sqlite3.Connection, user: dict) -> int:
    """Send the morning digest if it's after 10am local and we haven't sent yet today.

    Prints exactly one `[digest:<label>] ...` line per call so the main poll loop
    shows the digest status for every user on every cycle.

    Returns 1 if sent, 0 otherwise.
    """
    user_id = user["user_id"]
    recipient = (user.get("email") or "").strip()
    label = recipient or user_id[:8]

    api_key = os.environ.get("RESEND_API_KEY")
    from_addr = os.environ.get("RESEND_FROM")
    if not api_key or not from_addr:
        print(f"[digest:{label}] skipped: RESEND_API_KEY/RESEND_FROM not set")
        return 0

    if not recipient:
        print(f"[digest:{label}] skipped: no email on user")
        return 0

    now_local = datetime.now()
    today_str = now_local.strftime("%Y-%m-%d")

    if now_local.hour < SEND_HOUR_LOCAL:
        print(
            f"[digest:{label}] skipped: before {SEND_HOUR_LOCAL:02d}:00 local "
            f"(now {now_local.strftime('%H:%M')})"
        )
        return 0

    last_sent = get_user_state(conn, user_id, LAST_SENT_KEY)
    if last_sent == today_str:
        print(f"[digest:{label}] skipped: already sent today ({today_str})")
        return 0

    buckets = _fetch_buckets(conn, user_id, now_local)
    subject, html, text = _render(user, buckets, _base_url())

    try:
        import resend
        resend.api_key = api_key
        resend.Emails.send({
            "from": from_addr,
            "to": [recipient],
            "subject": subject,
            "html": html,
            "text": text,
        })
    except Exception as exc:
        print(f"[digest:{label}] send failed: {exc}")
        return 0

    set_user_state(conn, user_id, LAST_SENT_KEY, today_str)
    print(
        f"[digest:{label}] sent "
        f"(awaiting={buckets['awaiting']['count']}, "
        f"urgent={buckets['urgent']['count']}, "
        f"closed_yesterday={buckets['closed_yesterday']})"
    )
    return 1
