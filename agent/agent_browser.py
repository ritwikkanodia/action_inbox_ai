"""Keep the agent's browser alive between turns, so Hermes attaches to it
instead of launching — and killing — a fresh one each time.

Hermes normally launches the real Chrome binary on a *copy* of the user's
profile at the start of a turn and terminates it from an `atexit` hook at the
end. Two things follow from that, both bad for an inbox that is supposed to run
unattended. Every launch re-snapshots the profile, which needs the user's own
Chrome closed (it holds the password databases locked; see
`agent.chrome_profile`). And anything signed into that window — by the agent,
or by the user finishing a login the agent handed back — is gone before the
next turn can use it.

Hermes is built for the other arrangement too. Before launching, it looks for
a Chrome *already running* on the profile copy (`DevToolsActivePort` plus a
`/json/version` handshake), and if it finds one it attaches to that, skips the
snapshot — its own comment marks overlaying a live profile as never-do — and
leaves it alone on exit: no `Popen` handle, so not its to terminate. So this
module launches that Chrome itself, detached, and the turn-to-turn continuity
falls out: one live browser, one cookie jar, logins that stick.

The trade is freshness. A browser that is never re-snapshotted drifts from the
user's real Chrome: a site they log into over there is not seen over here.
For an agent that accumulates its own sessions as it works, that is the right
side of the trade; `refresh_snapshot` exists for when it is not.

Opt-in via `HERMES_PERSISTENT_BROWSER`, like the autoclose it mostly
replaces. macOS only.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

from agent import chrome_profile

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("HERMES_PERSISTENT_BROWSER", "").strip().lower() in {
    "1", "true", "yes",
}

# Hermes' own locations and launch line, mirrored rather than imported: this
# process runs in a different venv, and Hermes' browser modules pull in its
# whole config layer on import. `COPY_DIR` is `real_profile_copy_dir("chrome")`;
# the flags are `_REAL_PROFILE_CHROME_FLAGS` minus `--headless=new`, since the
# point of a window that stays is being able to look at it.
HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
COPY_DIR = os.path.join(HERMES_HOME, "browser-profile", "chrome")
SNAPSHOT_MARKER = os.path.join(COPY_DIR, ".hermes-snapshot-complete")
DEVTOOLS_PORT_FILE = os.path.join(COPY_DIR, "DevToolsActivePort")
REAL_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LAUNCH_FLAGS = (
    "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
    "--disable-background-networking", "--disable-component-update",
    "--disable-default-apps", "--disable-hang-monitor", "--disable-popup-blocking",
    "--disable-prompt-on-repost", "--disable-sync", "--disable-features=Translate",
    "--no-startup-window",
)

# The interpreter Hermes runs under, for the one thing this module borrows
# from it: `snapshot_real_profile`, which knows how to copy a Chrome profile
# without losing keychain-encrypted cookies. Derived from the standard install
# layout; `HERMES_PYTHON` overrides.
HERMES_PYTHON = os.environ.get("HERMES_PYTHON") or os.path.join(
    HERMES_HOME, "hermes-agent", "venv", "bin", "python"
)

LAUNCH_DEADLINE_SECONDS = 30.0


def _supported() -> bool:
    return sys.platform == "darwin" and os.path.isfile(REAL_BINARY)


def snapshot_ready() -> bool:
    """Whether the profile copy has ever been fully populated.

    Hermes writes the marker only after a complete copy, so a torn one (disk
    full, interrupted) does not count — launching Chrome on it would mean a
    signed-out agent that does not know it.
    """
    return os.path.isfile(SNAPSHOT_MARKER)


def is_alive() -> bool:
    """True when a Chrome we can attach to is running on the profile copy.

    The same test Hermes applies, for the same reason: `DevToolsActivePort`
    outlives a crashed Chrome, and its port can be recycled by any other local
    DevTools server, so the browser id it names has to match what the endpoint
    reports before either of us trusts it.
    """
    try:
        with open(DEVTOOLS_PORT_FILE, encoding="utf-8") as fh:
            port, browser_path = fh.readline().strip(), fh.readline().strip()
    except OSError:
        return False
    if not port.isdigit() or not browser_path.startswith("/devtools/browser/"):
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=2
        ) as resp:
            ws_url = str(json.load(resp).get("webSocketDebuggerUrl") or "")
    except Exception:
        return False
    return ws_url.endswith(browser_path)


def refresh_snapshot() -> bool:
    """Re-copy the user's real profile into the copy dir. Returns success.

    Two preconditions, both handled here: the user's Chrome must be closed
    (it locks the password databases) and no Chrome may be running on the
    copy (overlaying a live profile corrupts it). The copy itself is Hermes'
    own `snapshot_real_profile`, run under Hermes' interpreter, because a
    naive file copy of a macOS Chrome profile produces a browser that has
    silently dropped every keychain-encrypted cookie.
    """
    if is_alive():
        logger.warning("refresh_snapshot: a browser is running on the copy; not overlaying it")
        return False
    if not os.path.isfile(HERMES_PYTHON):
        logger.warning("refresh_snapshot: Hermes interpreter not found at %s", HERMES_PYTHON)
        return False

    closed = chrome_profile.close_for_run()
    try:
        result = subprocess.run(
            [HERMES_PYTHON, "-c",
             "from hermes_cli.browser_connect import snapshot_real_profile;"
             "d, e = snapshot_real_profile('chrome');"
             "import sys; print(e or '', file=sys.stderr); sys.exit(1 if e else 0)"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("refresh_snapshot: could not run Hermes' snapshot: %s", exc)
        return False
    finally:
        if closed:
            chrome_profile.restore()

    if result.returncode != 0:
        logger.warning("refresh_snapshot failed: %s", (result.stderr or "").strip())
        return False
    logger.info("profile snapshot refreshed")
    return True


def _browser_env() -> dict:
    """A minimal environment for the browser: Chrome needs HOME for the
    keychain and little else, and it has no business seeing this process's
    API keys."""
    keep = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL")
    return {k: v for k, v in os.environ.items() if k in keep}


def launch() -> bool:
    """Start a detached Chrome on the profile copy; wait for its debug port."""
    try:
        os.unlink(DEVTOOLS_PORT_FILE)  # a stale port file would fool the reuse probe
    except OSError:
        pass
    argv = [REAL_BINARY, f"--user-data-dir={COPY_DIR}", *LAUNCH_FLAGS]
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            # Its own session: it must outlive this request, this thread, and
            # — if the debug reloader has its way — this process.
            start_new_session=True,
            env=_browser_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not launch the agent browser: %s", exc)
        return False

    deadline = time.monotonic() + LAUNCH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if is_alive():
            logger.info("agent browser up on %s (pid %s)", COPY_DIR, proc.pid)
            return True
        if proc.poll() is not None:
            logger.warning("agent browser exited during startup (status %s)", proc.returncode)
            return False
        time.sleep(0.25)
    logger.warning("agent browser did not expose a debug port within %ss", LAUNCH_DEADLINE_SECONDS)
    return False


def ensure_running() -> bool:
    """Make sure an attachable browser is up before a turn. Returns whether it is.

    False is not an error, just the signal for the caller to fall back to
    Hermes' own launch-and-snapshot path (with the autoclose, if enabled).
    """
    if not ENABLED or not _supported():
        return False
    if is_alive():
        return True
    if not snapshot_ready() and not refresh_snapshot():
        return False
    return launch()
