"""macOS desktop notification for a newly discovered todo.

Shells out to `osascript -e 'display notification ...'`, which ships with macOS,
so there is nothing to install. Anywhere else (the deployed container, Linux)
this is a silent no-op, and `NOTIFY_NEW_TODOS=0` turns it off on a Mac too.

The poller does not know which signed-in user owns this machine, so it fires
for every user's new todos — fine for a single-owner local run, which is the
only place the poller has a desktop to notify anyway.

A notification must never take down a poll cycle: every failure here is
logged and swallowed.
"""
import logging
import os
import shlex
import subprocess
import sys

log = logging.getLogger("notify")

_TITLE_LIMIT = 120


def _enabled() -> bool:
    if sys.platform != "darwin":
        return False
    return os.environ.get("NOTIFY_NEW_TODOS", "1").strip().lower() not in {"0", "false", "no", ""}


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _applescript_string(s: str) -> str:
    # AppleScript string literals use backslash escapes for `"` and `\`.
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_osascript(todo_title: str, source: str, importance: str | None) -> list[str]:
    """The argv for the `osascript` call — factored out so it can be verified
    without a Mac and without popping a banner."""
    body = _truncate(todo_title, _TITLE_LIMIT) or "(untitled)"
    if importance == "high":
        body = f"[high] {body}"
    title = f"New todo · {source}"
    script = (
        f"display notification {_applescript_string(body)} "
        f"with title {_applescript_string(title)}"
    )
    return ["osascript", "-e", script]


def notify_new_todo(todo_title: str, source: str, importance: str | None = None) -> bool:
    """Show a banner for a todo that was just inserted (not deduped).

    Returns True if the notification command ran cleanly, False otherwise —
    callers can ignore the result; it exists for the verify script.
    """
    if not _enabled():
        return False
    argv = build_osascript(todo_title, source, importance)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except Exception as exc:  # missing binary, timeout, anything — never propagate
        log.warning("notification failed: %s", exc)
        return False
    if proc.returncode != 0:
        log.warning("osascript exited %s: %s", proc.returncode, proc.stderr.strip())
        return False
    log.debug("notified: %s", shlex.join(argv))
    return True
