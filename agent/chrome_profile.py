"""Close and restore the user's Chrome around a Hermes run, so the agent's
browser can inherit their logins without anyone quitting Chrome by hand.

Hermes launches its browser against a *copy* of the real Chrome profile
(`browser.use_real_profile`). Making that copy means backing up Chrome's auth
databases with SQLite, and a running Chrome holds three of them —
`Login Data`, `Login Data For Account`, `Web Data` — with a write lock. Hermes
fails closed on any locked one ("never launch a silently signed-out session"),
so the browser never opens and the turn is spent on nothing. `Cookies`, the
database that actually carries the sessions, copies fine; the block is the
password and autofill stores.

Hermes deliberately refuses to close the browser itself: quitting is
destructive and it wants a per-attempt human decision. That is the right call
for a general-purpose CLI and the wrong one for an inbox that is supposed to
run unattended, so the decision is made *once* here, by an env var, instead of
once per run by a person.

Two rules keep that safe:

- **Opt-in.** Off unless `HERMES_AUTOCLOSE_CHROME` is set, so nothing changes
  for anyone who has not asked for it.
- **Never force-kill.** Only an AppleScript `quit`, which lets Chrome save its
  session so "Continue where you left off" (or Cmd+Shift+T) restores the tabs.
  A Chrome that will not quit inside the grace window — a `beforeunload`
  prompt, say — is left strictly alone, and the run proceeds to whatever Hermes
  makes of it. Losing a browser tab must never be the cost of a todo.

macOS only; every other platform is a no-op.
"""

import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# Opt-in, per the module docstring. Anything but an explicit truthy value
# leaves the previous behaviour exactly as it was.
AUTOCLOSE = os.environ.get("HERMES_AUTOCLOSE_CHROME", "").strip().lower() in {
    "1", "true", "yes",
}

# How long to let Chrome save its session and exit. Chrome writes its session
# and cookie state on the way out; a short bound keeps a hung quit from eating
# the whole turn, and a hung quit is never escalated.
QUIT_GRACE_SECONDS = float(os.environ.get("HERMES_CHROME_QUIT_GRACE", "20"))

_POLL_SECONDS = 0.5

# The real application binary. Matching it is necessary but not sufficient:
# Hermes launches its agent browser from this *same* binary, pointed at its
# own profile copy, so a path match alone would mistake the run's own browser
# for the user's — and quitting that would be a spectacular own goal.
_CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Flags that mark a Chrome as automation-launched rather than the user's own.
# A person opening Chrome gets a bare binary, or at most a URL; an agent-
# browser process (Hermes' or anyone's) carries a profile override and a
# DevTools port. Any one of these disqualifies a process from being "the
# user's Chrome".
_AUTOMATION_FLAGS = ("--user-data-dir=", "--remote-debugging-port", "--headless")


def _supported() -> bool:
    return sys.platform == "darwin"


def is_running() -> bool:
    """True when the user's own Chrome is up.

    Matched on the real binary path, then filtered: a process carrying any
    automation flag is an agent browser, not the user's, however identical the
    binary. Chrome's helper processes (renderers, GPU) live under
    `Contents/Frameworks/...` and are deliberately *not* matched: they linger
    briefly after the browser itself goes away, and waiting on them would make
    a clean quit look like a hung one.
    """
    if not _supported():
        return False
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Never let a process probe break a run; assume not running and let
        # Hermes report the profile lock in its own words.
        logger.debug("chrome probe failed: %s", exc)
        return False
    for line in result.stdout.splitlines():
        if not line.startswith(_CHROME_BINARY):
            continue
        if any(flag in line for flag in _AUTOMATION_FLAGS):
            continue
        return True
    return False


def _quit() -> None:
    """Ask Chrome to quit the way the user's own Cmd+Q does."""
    subprocess.run(
        ["osascript", "-e", 'quit app "Google Chrome"'],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _relaunch() -> None:
    """Bring Chrome back, restoring whatever the graceful quit saved.

    `-n` matters. Hermes' agent browser is the same Chrome binary under a
    different profile, and one often outlives the turn (its daemon reaps it
    on an inactivity timer). LaunchServices sees "Google Chrome is already
    running" and a plain `open -a` merely raises that window — the user's own
    Chrome, with their own profile, never comes back. `-n` insists on a new
    instance; Chrome's singleton lock is per profile directory, so the two
    coexist.
    """
    subprocess.run(
        ["open", "-n", "-a", "Google Chrome"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def close_for_run() -> bool:
    """Quit Chrome so Hermes can snapshot the profile. Returns whether it did.

    The return value is the caller's obligation to restore: True means this
    process took the browser away and owes the user it back. False covers every
    other case — opt-in off, not macOS, Chrome already closed, or a quit that
    did not land — and means the caller must NOT relaunch something it never
    closed.
    """
    if not AUTOCLOSE or not _supported() or not is_running():
        return False

    logger.info("closing Chrome so Hermes can snapshot its profile")
    try:
        _quit()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not ask Chrome to quit: %s", exc)
        return False

    deadline = time.monotonic() + QUIT_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not is_running():
            logger.info("Chrome closed; profile is now snapshottable")
            return True
        time.sleep(_POLL_SECONDS)

    # Still up: something is holding it — an unsaved-changes prompt, most
    # likely. Leave it be. The run continues and Hermes reports the locked
    # profile itself; that is a far better outcome than taking the tab away.
    logger.warning(
        "Chrome did not quit within %ss; leaving it alone and letting the run "
        "proceed (Hermes will report the locked profile)",
        QUIT_GRACE_SECONDS,
    )
    return False


def restore() -> None:
    """Relaunch Chrome after a run that closed it. Never raises."""
    try:
        logger.info("restoring Chrome")
        _relaunch()
    except (OSError, subprocess.SubprocessError) as exc:
        # The turn is already done and its result is what matters; a browser
        # that did not come back is worth a log line, not a failed resolution.
        logger.warning("could not relaunch Chrome: %s", exc)
