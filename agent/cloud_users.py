"""One isolated Hermes per user, for the cloud container.

A cloud turn runs `hermes chat` as an unprivileged OS user of its own, with a
`HERMES_HOME` only that user can read. The isolation is the kernel's, not
config: the database is root-owned and unreadable by every `aib-*` user, each
home is 0700, and the agent's process has no terminal or file tool to try
anyway. This module owns that arrangement — the uid, the OS account, the
home, the per-user `config.yaml`, and the allowlisted environment the runner
hands the subprocess. Nothing here runs unless `HERMES_CLOUD=1`.

The config is rewritten from `render_config` on every turn, so a change to it
ships with a deploy rather than living on in homes created under an old
version. Everything it does not set falls back to Hermes' own defaults.
"""

import os
import pwd
import subprocess
from typing import NamedTuple

import yaml


def is_cloud() -> bool:
    return os.environ.get("HERMES_CLOUD", "").strip().lower() in {"1", "true", "yes"}


HOMES_DIR = os.environ.get("HERMES_HOMES_DIR", "/data/hermes")

# Each turn is a Hermes process plus a headless Chromium, roughly half a
# gigabyte; the runner waits on a semaphore of this size before spawning.
MAX_TURNS = max(1, int(os.environ.get("MAX_CLOUD_TURNS", "3") or 3))

CLOUD_MODEL = os.environ.get("HERMES_CLOUD_MODEL", "gpt-6-astra").strip() or "gpt-6-astra"

# The only names a cloud subprocess inherits from the app's environment. The
# rest — FLASK_SECRET_KEY, GOOGLE_CLIENT_SECRET, META_WA_*, RESEND_API_KEY,
# VAPID_PRIVATE_KEY and whatever Railway adds — stays in the app. OPENAI_API_KEY
# is the one shared secret that does cross: Hermes needs it to run the model,
# and it cannot read anyone's data.
ENV_ALLOWLIST = ("PATH", "HOME", "HERMES_HOME", "LANG", "PLAYWRIGHT_BROWSERS_PATH",
                 "OPENAI_API_KEY", "HERMES_DISABLE_LAZY_INSTALLS")

# What the cloud agent may use. No terminal, no file tools, no skills, no cron:
# the todo/Google tools come from the MCP server below, memory is per home
# (so per user), and `todo` is Hermes' in-memory planner, not our list.
TOOLSETS = ["browser", "web", "memory", "todo"]

UID_BASE = 20000


class CloudUser(NamedTuple):
    uid: int
    username: str
    home: str
    gid: int   # useradd's default is a private group with the same id


def ensure(conn, user_id: str, run=subprocess.run, homes_dir: str | None = None) -> CloudUser:
    """The OS user and home for `user_id`, created on first use.

    Idempotent: an existing passwd entry and an existing home are left alone.
    A missing passwd entry for a known uid is recreated — a redeploy replaces
    /etc/passwd but not the volume — and the home keeps its files. `run` and
    `homes_dir` are injectable for the verify script.
    """
    from db import ensure_cloud_user_row
    uid = ensure_cloud_user_row(conn, user_id)
    username = f"aib-{uid}"
    home = os.path.join(homes_dir or HOMES_DIR, str(uid))
    try:
        pwd.getpwnam(username)
    except KeyError:
        run(["useradd", "--uid", str(uid), "--no-create-home", "--home-dir", home,
             "--shell", "/usr/sbin/nologin", username], check=True)
    os.makedirs(home, exist_ok=True)
    _own(home, uid)
    os.chmod(home, 0o700)
    return CloudUser(uid=uid, username=username, home=home, gid=uid)


def _own(path: str, uid: int) -> None:
    # Only root can chown; the verify script runs unprivileged and the home
    # is then simply owned by whoever ran it.
    if os.geteuid() == 0:
        os.chown(path, uid, uid)


def render_config(user: CloudUser, python: str, repo: str) -> dict:
    """The per-user Hermes config. `python` runs the MCP server; `repo` is its
    cwd. The `${AIB_*}` references are expanded by Hermes from the subprocess
    environment the runner builds (`subprocess_env`)."""
    return {
        "model": {
            "provider": "openai-api",
            "default": CLOUD_MODEL,
            "base_url": "https://api.openai.com/v1",
        },
        "platform_toolsets": {"cli": list(TOOLSETS)},
        "browser": {
            "headed": False,
            "use_real_profile": False,
        },
        "mcp_servers": {
            "action_inbox_google": {
                "command": python,
                "args": ["-m", "agent.google_mcp"],
                "cwd": repo,
                "env": {
                    "AIB_USER_ID": "${AIB_USER_ID}",
                    "AIB_ACCOUNT_ID": "${AIB_ACCOUNT_ID}",
                    "AIB_TODO_ID": "${AIB_TODO_ID}",
                    "AIB_API_URL": "${AIB_API_URL}",
                    "AIB_RUN_TOKEN": "${AIB_RUN_TOKEN}",
                },
            },
        },
    }


def write_config(user: CloudUser, python: str, repo: str) -> str:
    path = os.path.join(user.home, "config.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(render_config(user, python, repo), f, sort_keys=False)
    _own(path, user.uid)
    os.chmod(path, 0o600)
    return path


def subprocess_env(user: CloudUser, binding: dict[str, str]) -> dict[str, str]:
    """The environment a cloud `hermes chat` gets: the allowlist, the user's
    home, and the per-turn binding. Built from scratch, never from a copy."""
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    env["HOME"] = user.home
    env["HERMES_HOME"] = user.home
    env.update(binding)
    return env


def grant_files(user: CloudUser, paths: list[str]) -> None:
    """Hand the user's own attached files (WhatsApp photos under UPLOADS_DIR)
    to their agent: each file becomes theirs, 0600, and the directories above
    it traversable. Root-only work; a no-op when not root, and a missing file
    is left for the turn to report."""
    if os.geteuid() != 0:
        return
    for path in paths or []:
        try:
            os.chown(path, user.uid, user.gid)
            os.chmod(path, 0o600)
            parent = os.path.dirname(path)
            while parent and parent != "/":
                # Traversable (o+x), never listable: a sibling user's agent
                # cannot enumerate other users' upload folders.
                os.chmod(parent, os.stat(parent).st_mode | 0o011)
                parent = os.path.dirname(parent)
        except OSError:
            continue
