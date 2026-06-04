"""Morning email digest — the product itself, sent once per day at 9am local.

The digest is the main surface, not a teaser back to the app. It leads with an
URGENT band (items that have a real clock — a deadline within ~48h) shown
distinctly with a why-now, followed by SUGGESTIONS (everything else worth a
look). Item titles link straight to the source (`relevant_link`).

Two retention rules keep it from nagging:
  - urgent items show only while their deadline is live (within 48h / overdue);
  - suggestions age out after 7 days of sitting unacted — if the user neither
    acted nor dismissed, we quietly stop surfacing them.

(Resolution detection — auto-closing items when we see the user already acted
in Gmail — is intentionally not done here yet.)
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db import get_source_connection, get_user_state, set_user_state


log = logging.getLogger(__name__)

SEND_HOUR_LOCAL = 9
URGENT_WINDOW_HOURS = 72        # a deadline within this window (or overdue) leads the digest
AGEOUT_DAYS = 7                 # suggestions sitting unacted longer than this stop surfacing
SUGGESTION_LIMIT = 15           # cap suggestions shown; urgent items are always shown in full
TITLE_MAX_CHARS = 70
LAST_SENT_KEY = "digest_last_sent_date"
CONNECT_PROMPT_KEY = "digest_connect_prompts_sent"
CONNECT_PROMPT_MAX = 3          # nudge an unconnected user this many times, then go quiet
DEFAULT_TIMEZONE = "Asia/Kolkata"

_IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:5001").rstrip("/")


def _user_tz() -> ZoneInfo:
    """Resolve the timezone used to evaluate the send gate and format deadlines.

    Single env var for now (`DIGEST_TIMEZONE`); per-user overrides can be
    layered on later via `user_state` without changing this signature.
    """
    name = os.environ.get("DIGEST_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("DIGEST_TIMEZONE=%r not found; falling back to %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def _now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(_user_tz())


def _parse_dt(s: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp into a tz-aware UTC datetime, or None."""
    if not s:
        return None
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _truncate(s: str, n: int = TITLE_MAX_CHARS) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _first_name(user: dict) -> str:
    name = (user.get("name") or "").strip()
    if name:
        return name.split()[0]
    email = (user.get("email") or "").strip()
    return email.split("@")[0] if email else "there"


def _why_now(due_local: datetime, now_local: datetime) -> str:
    """Human 'why now' for an urgent item, e.g. 'Due today at 2:00 PM'.

    Only ever called for items whose deadline is still ahead (now .. +48h), so
    there's no 'Overdue' case — once a deadline passes the item leaves the band.
    """
    has_time = (due_local.hour, due_local.minute) != (0, 0)
    time_str = due_local.strftime("%-I:%M %p")
    today = now_local.date()
    d = due_local.date()
    if d == today:
        return f"Due today at {time_str}" if has_time else "Due today"
    if d == today + timedelta(days=1):
        return f"Due tomorrow at {time_str}" if has_time else "Due tomorrow"
    label = due_local.strftime("%a")  # within the 48h window, so this/next couple days
    return f"Due {label} at {time_str}" if has_time else f"Due {label}"


def _due_hint(due_local: datetime) -> str:
    """Compact deadline hint for a (non-urgent) suggestion, e.g. 'Jun 12'."""
    return due_local.strftime("%b %-d")


def _fetch_buckets(conn: sqlite3.Connection, user_id: str, now_local: datetime) -> dict:
    """Split open work into an urgent band (live clock) and suggestions.

    Urgent: a parseable due_date within URGENT_WINDOW_HOURS (or already overdue).
    Suggestions: everything else still open, minus anything that has sat unacted
    for more than AGEOUT_DAYS.
    """
    tz = now_local.tzinfo
    now_utc = datetime.now(timezone.utc)
    urgent_ceiling = now_local + timedelta(hours=URGENT_WINDOW_HOURS)
    ageout_cutoff = now_utc - timedelta(days=AGEOUT_DAYS)
    inbox_url = f"{_base_url()}/"

    rows = conn.execute(
        """
        SELECT todo_id, title, due_date, importance, relevant_link, created_at
        FROM todos
        WHERE user_id = ?
          AND status IN ('open', 'ongoing')
          AND COALESCE(decision, '') <> 'rejected'
          AND title IS NOT NULL AND title != ''
        """,
        (user_id,),
    ).fetchall()

    urgent, suggestions = [], []
    for todo_id, title, due_date, importance, link, created_at in rows:
        due_dt = _parse_dt(due_date)
        due_local = due_dt.astimezone(tz) if due_dt is not None else None
        item = {
            "id": todo_id,
            "title": _truncate(title),
            "link": link or inbox_url,
            "importance": importance,
        }

        # Urgent only while the deadline is still ahead and within the window.
        # A date-only deadline ("due today") stays live through end of that day;
        # once any deadline passes, the item drops out of the band rather than
        # screaming at the top forever.
        if due_local is not None:
            has_time = (due_local.hour, due_local.minute) != (0, 0)
            effective = (
                due_local
                if has_time
                else due_local.replace(hour=23, minute=59, second=59, microsecond=0)
            )
            if now_local <= effective <= urgent_ceiling:
                item["why_now"] = _why_now(due_local, now_local)
                item["_sort"] = due_dt
                urgent.append(item)
                continue

        # suggestion — age out anything sitting unacted past the window
        created_dt = _parse_dt(created_at)
        if created_dt is not None and created_dt < ageout_cutoff:
            continue
        if due_local is not None:
            item["due_hint"] = _due_hint(due_local)
        item["_sort"] = created_dt or now_utc
        suggestions.append(item)

    urgent.sort(key=lambda i: i["_sort"])  # soonest deadline first
    # most important first, then freshest within a tier (newest created on top)
    suggestions.sort(
        key=lambda i: (_IMPORTANCE_RANK.get(i["importance"], 3), -i["_sort"].timestamp()),
    )

    def strip(items):
        return [{k: v for k, v in i.items() if not k.startswith("_")} for i in items]

    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    yesterday_start = (now_local - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    closed_yesterday = conn.execute(
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

    shown = strip(suggestions[:SUGGESTION_LIMIT])
    return {
        "urgent": {"count": len(urgent), "items": strip(urgent)},
        "suggestions": {
            "count": len(suggestions),
            "shown": len(shown),
            "items": shown,
        },
        "closed_yesterday": closed_yesterday,
    }


def _render_connect_prompt(user: dict, base_url: str) -> tuple[str, str, str]:
    """Email for a signed-up user who hasn't connected Gmail yet — nudge them to."""
    name = _first_name(user)
    settings_url = f"{base_url}/settings"
    subject = "Connect Gmail to start your daily digest"

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:560px;margin:0 auto;padding:32px 24px;color:#222;">
  <p style="font-size:18px;margin:0 0 16px 0;">
    Morning, {escape(name)}. You're signed up — one step left.
  </p>
  <p style="font-size:15px;line-height:1.5;margin:0 0 24px 0;color:#444;">
    Connect your Gmail and each morning I'll surface the emails that actually need
    action — replies you owe, deadlines, things waiting on you — with the urgent
    ones up top. Right now I can't see your inbox, so there's nothing to send yet.
  </p>
  <p style="margin:24px 0;">
    <a href="{escape(settings_url)}" style="display:inline-block;background:#1a73e8;color:#fff;
       padding:12px 22px;border-radius:6px;text-decoration:none;font-weight:600;">
      Connect Gmail</a>
  </p>
  <p style="margin:24px 0 0 0;font-size:13px;color:#888;">
    Takes about 30 seconds. You can disconnect anytime from Settings.
  </p>
</body></html>"""

    text = (
        f"Morning, {name}. You're signed up — one step left.\n\n"
        "Connect your Gmail and each morning I'll surface the emails that need "
        "action, with the urgent ones up top. Right now I can't see your inbox, "
        "so there's nothing to send yet.\n\n"
        f"Connect Gmail: {settings_url}\n"
    )
    return subject, html, text


def _render(
    user: dict, buckets: dict, base_url: str, gmail_connected: bool = True
) -> tuple[str, str, str]:
    """Return (subject, html, text)."""
    if not gmail_connected:
        return _render_connect_prompt(user, base_url)

    name = _first_name(user)
    inbox_url = f"{base_url}/"
    today_str = datetime.now().strftime("%a, %b %-d")

    urgent = buckets["urgent"]
    suggestions = buckets["suggestions"]
    closed_y = buckets["closed_yesterday"]
    u, s = urgent["count"], suggestions["count"]
    inbox_zero = u == 0 and s == 0

    # ---- subject ----
    if inbox_zero:
        subject = f"Inbox zero — {today_str}"
    elif u > 0:
        lead = urgent["items"][0]["title"]
        subject = f"⏰ {u} urgent — {_truncate(lead, 48)}"
    else:
        subject = f"{s} suggestion{'s' if s != 1 else ''} for today"

    # ---- hero ----
    if inbox_zero:
        hero = (
            f'<p style="font-size:18px;margin:0 0 8px 0;">'
            f"Inbox zero. Nice work, {escape(name)}."
            f"</p>"
        )
    else:
        if u and s:
            lede = (
                f"<strong>{u}</strong> urgent · "
                f"<strong>{s}</strong> suggestion{'s' if s != 1 else ''}."
            )
        elif u:
            lede = (
                f"<strong>{u}</strong> item{'s' if u != 1 else ''} with a deadline "
                f"need{'s' if u == 1 else ''} you."
            )
        else:
            lede = f"<strong>{s}</strong> suggestion{'s' if s != 1 else ''} worth a look."
        hero = (
            f'<p style="font-size:18px;margin:0 0 20px 0;">'
            f"Morning, {escape(name)}. {lede}"
            f"</p>"
        )

    # ---- urgent band (visually distinct, leads the email) ----
    urgent_html = ""
    if u:
        items = "".join(
            f'<li style="margin:0 0 12px 0;padding:13px 15px;background:#fff;'
            f'border:1px solid #fee2e2;border-left:4px solid #dc2626;border-radius:6px;">'
            f'<a href="{escape(t["link"])}" style="color:#111;font-weight:600;font-size:16px;'
            f'text-decoration:none;line-height:1.35;">{escape(t["title"])}</a>'
            f'<div style="margin-top:5px;font-size:13px;font-weight:600;color:#dc2626;">'
            f'⏰ {escape(t["why_now"])}</div>'
            f"</li>"
            for t in urgent["items"]
        )
        urgent_html = f"""
        <div style="margin:24px 0;">
          <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.6px;
                     color:#dc2626;margin:0 0 12px 0;font-weight:700;">
            Urgent · {u}
          </h2>
          <ul style="list-style:none;padding:0;margin:0;">{items}</ul>
        </div>
        """

    # ---- suggestions ----
    suggestions_html = ""
    if s:
        items = "".join(
            f'<li style="margin:8px 0;font-size:15px;line-height:1.5;">'
            f'<a href="{escape(t["link"])}" style="color:#1a73e8;text-decoration:none;">'
            f'{escape(t["title"])}</a>'
            + (
                f'<span style="color:#999;font-size:13px;"> · due {escape(t["due_hint"])}</span>'
                if t.get("due_hint")
                else ""
            )
            + "</li>"
            for t in suggestions["items"]
        )
        more = ""
        if s > suggestions["shown"]:
            extra = s - suggestions["shown"]
            more = (
                f'<p style="margin:8px 0 0 0;font-size:13px;">'
                f'<a href="{inbox_url}" style="color:#666;">+{extra} more in your inbox →</a>'
                f"</p>"
            )
        suggestions_html = f"""
        <div style="margin:28px 0;">
          <h2 style="font-size:13px;text-transform:uppercase;letter-spacing:.6px;
                     color:#666;margin:0 0 10px 0;font-weight:700;">
            Suggestions · {s}
          </h2>
          <ul style="list-style:none;padding:0;margin:0;">{items}</ul>
          {more}
        </div>
        """

    # ---- footer: yesterday's win ----
    footer = ""
    if closed_y > 0:
        plural = "s" if closed_y != 1 else ""
        footer = (
            '<div style="margin:32px 0 0 0;padding:16px 18px;background:#ecfdf5;'
            'border-left:4px solid #10b981;border-radius:6px;">'
            '<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.6px;color:#059669;margin-bottom:6px;">🎉 Yesterday&rsquo;s win</div>'
            '<div style="font-size:16px;color:#064e3b;line-height:1.45;">'
            f'You closed <strong style="font-size:20px;color:#047857;">{closed_y}</strong> '
            f"todo{plural} yesterday. Keep the streak going!"
            "</div></div>"
        )

    # ---- secondary CTA (the app is now the optional engine, not the destination) ----
    cta = (
        f'<p style="margin:28px 0 0 0;font-size:13px;">'
        f'<a href="{inbox_url}" style="color:#666;">Manage everything in your inbox →</a>'
        f"</p>"
    )

    html = f"""<!doctype html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                   max-width:560px;margin:0 auto;padding:32px 24px;color:#222;">
  {hero}
  {urgent_html}
  {suggestions_html}
  {footer}
  {cta}
</body></html>"""

    # ---- plain text ----
    lines = []
    if inbox_zero:
        lines.append(f"Inbox zero. Nice work, {name}.")
    else:
        lines.append(f"Morning, {name}.")
        if u:
            lines.append("")
            lines.append(f"URGENT ({u}):")
            for t in urgent["items"]:
                lines.append(f"  • {t['title']} — {t['why_now']}")
                lines.append(f"    {t['link']}")
        if s:
            lines.append("")
            lines.append(f"Suggestions ({s}):")
            for t in suggestions["items"]:
                hint = f" (due {t['due_hint']})" if t.get("due_hint") else ""
                lines.append(f"  • {t['title']}{hint}")
                lines.append(f"    {t['link']}")
            if s > suggestions["shown"]:
                lines.append(f"  +{s - suggestions['shown']} more in your inbox.")
    if closed_y > 0:
        lines.append("")
        lines.append(f"You closed {closed_y} todo(s) yesterday.")
    lines.append("")
    lines.append(f"Manage everything: {inbox_url}")
    text = "\n".join(lines)

    return subject, html, text


def poll(conn: sqlite3.Connection, user: dict) -> int:
    """Send the morning digest if it's after the send hour and not yet sent today.

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

    now_local = _now_local()
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

    gmail_connected = bool(get_source_connection(conn, user_id, "gmail"))
    prompts_sent = 0

    if gmail_connected:
        # Active user → the real digest.
        buckets = _fetch_buckets(conn, user_id, now_local)
        subject, html, text = _render(user, buckets, _base_url(), gmail_connected=True)
        sent_desc = (
            f"urgent={buckets['urgent']['count']}, "
            f"suggestions={buckets['suggestions']['count']}, "
            f"closed_yesterday={buckets['closed_yesterday']}"
        )
    else:
        # Not connected → a *bounded* nudge to connect, then go quiet. Sending a
        # contentless prompt daily forever would just train them to ignore us and
        # erode sender reputation for the users who do engage.
        prompts_sent = int(get_user_state(conn, user_id, CONNECT_PROMPT_KEY) or 0)
        if prompts_sent >= CONNECT_PROMPT_MAX:
            print(
                f"[digest:{label}] skipped: gmail not connected, "
                f"connect-prompt cap reached ({prompts_sent}/{CONNECT_PROMPT_MAX})"
            )
            return 0
        subject, html, text = _render_connect_prompt(user, _base_url())
        sent_desc = f"connect-prompt {prompts_sent + 1}/{CONNECT_PROMPT_MAX}"

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
    if not gmail_connected:
        set_user_state(conn, user_id, CONNECT_PROMPT_KEY, str(prompts_sent + 1))
    print(f"[digest:{label}] sent ({sent_desc})")
    return 1
