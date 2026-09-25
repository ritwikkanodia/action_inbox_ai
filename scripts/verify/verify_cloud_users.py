"""Verifies agent/cloud_users.py: the per-user OS account, home, config and
environment for a cloud Hermes. `useradd` and `pwd` are stubbed; the home is
a temp directory; nothing needs root. No network, no spend.

Usage: python scripts/verify/verify_cloud_users.py
"""
import os
import sqlite3
import stat
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "cloud.db")
os.environ["HERMES_CLOUD"] = "1"
os.environ["HERMES_HOMES_DIR"] = os.path.join(_tmp, "homes")
os.environ["MAX_CLOUD_TURNS"] = "2"
os.environ["HERMES_CLOUD_MODEL"] = "gpt-test-model"
for secret in ("FLASK_SECRET_KEY", "GOOGLE_CLIENT_SECRET", "META_WA_ACCESS_TOKEN",
               "RESEND_API_KEY", "VAPID_PRIVATE_KEY", "GOOGLE_CLIENT_ID"):
    os.environ[secret] = f"verify-{secret.lower()}"
os.environ["OPENAI_API_KEY"] = "verify-openai-key"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/pw-browsers"

import yaml  # noqa: E402

from agent import cloud_users  # noqa: E402
from db import init_db, upsert_user  # noqa: E402


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


class FakePwd:
    """Stands in for `pwd.getpwnam`: unknown until `useradd` has run."""
    def __init__(self):
        self.known = set()

    def getpwnam(self, name):
        if name not in self.known:
            raise KeyError(name)
        return (name,)


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    init_db(conn)
    alice, _ = upsert_user(conn, "alice@example.com")
    bob, _ = upsert_user(conn, "bob@example.com")

    print("-- module settings --")
    check("is_cloud reads HERMES_CLOUD", cloud_users.is_cloud())
    check("MAX_TURNS reads MAX_CLOUD_TURNS", cloud_users.MAX_TURNS == 2)
    check("CLOUD_MODEL reads HERMES_CLOUD_MODEL", cloud_users.CLOUD_MODEL == "gpt-test-model")
    check("HOMES_DIR reads HERMES_HOMES_DIR", cloud_users.HOMES_DIR == os.environ["HERMES_HOMES_DIR"])

    print("\n-- ensure --")
    fake_pwd = FakePwd()
    cloud_users.pwd = fake_pwd
    calls = []

    def fake_run(argv, check=False, **kw):
        calls.append(argv)
        fake_pwd.known.add(argv[-1])

    user = cloud_users.ensure(conn, alice, run=fake_run)
    check("uid is 20000 + row", user.uid == 20001 and user.username == "aib-20001")
    check("home is under HOMES_DIR by uid",
          user.home == os.path.join(os.environ["HERMES_HOMES_DIR"], "20001"))
    check("useradd ran once with the exact argv",
          calls == [["useradd", "--uid", "20001", "--no-create-home", "--home-dir", user.home,
                     "--shell", "/usr/sbin/nologin", "aib-20001"]])
    check("home exists and is 0700",
          os.path.isdir(user.home) and stat.S_IMODE(os.stat(user.home).st_mode) == 0o700)
    again = cloud_users.ensure(conn, alice, run=fake_run)
    check("second ensure runs no useradd and returns the same user", again == user and len(calls) == 1)
    other = cloud_users.ensure(conn, bob, run=fake_run)
    check("another user gets the next uid and their own home",
          other.uid == 20002 and other.home != user.home and len(calls) == 2)
    fake_pwd.known.discard("aib-20001")   # a redeploy replaced /etc/passwd
    cloud_users.ensure(conn, alice, run=fake_run)
    check("a missing passwd entry for a known uid is recreated with the same uid",
          calls[-1][2] == "20001" and len(calls) == 3)

    print("\n-- config --")
    cfg = cloud_users.render_config(user, "/usr/bin/python3", "/app")
    check("toolsets are browser, web, memory, todo — nothing else",
          cfg["platform_toolsets"]["cli"] == ["browser", "web", "memory", "todo"])
    check("no terminal or file toolset anywhere",
          not ({"terminal", "file", "skills", "cronjob"} & set(cfg["platform_toolsets"]["cli"])))
    check("browser is headless on a copy of nothing",
          cfg["browser"]["headed"] is False and cfg["browser"]["use_real_profile"] is False)
    check("model is the OpenAI provider and the cloud model",
          cfg["model"]["provider"] == "openai-api" and cfg["model"]["default"] == "gpt-test-model")
    mcp = cfg["mcp_servers"]["action_inbox_google"]
    check("MCP server runs the repo's module with the repo as cwd",
          mcp["command"] == "/usr/bin/python3" and mcp["args"] == ["-m", "agent.google_mcp"]
          and mcp["cwd"] == "/app")
    check("MCP env forwards the five AIB_* references and no DB path",
          set(mcp["env"]) == {"AIB_USER_ID", "AIB_ACCOUNT_ID", "AIB_TODO_ID", "AIB_API_URL",
                              "AIB_RUN_TOKEN"}
          and all(v == "${" + k + "}" for k, v in mcp["env"].items()))
    path = cloud_users.write_config(user, "/usr/bin/python3", "/app")
    check("config written into the home", path == os.path.join(user.home, "config.yaml"))
    with open(path) as f:
        loaded = yaml.safe_load(f)
    check("written config round-trips", loaded == cfg)
    check("config file is 0600", stat.S_IMODE(os.stat(path).st_mode) == 0o600)

    print("\n-- environment --")
    env = cloud_users.subprocess_env(user, {"AIB_USER_ID": alice, "AIB_ACCOUNT_ID": "",
                                            "AIB_TODO_ID": "t1", "AIB_API_URL": "http://127.0.0.1:8000",
                                            "AIB_RUN_TOKEN": "tok", "AIB_DB_PATH": ""})
    check("HOME and HERMES_HOME are the user's home",
          env["HOME"] == user.home and env["HERMES_HOME"] == user.home)
    check("OPENAI_API_KEY and PATH pass through",
          env["OPENAI_API_KEY"] == "verify-openai-key" and env.get("PATH") == os.environ["PATH"])
    check("PLAYWRIGHT_BROWSERS_PATH passes through", env["PLAYWRIGHT_BROWSERS_PATH"] == "/opt/pw-browsers")
    leaked = [k for k in ("FLASK_SECRET_KEY", "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_ID",
                          "META_WA_ACCESS_TOKEN", "RESEND_API_KEY", "VAPID_PRIVATE_KEY",
                          "DB_PATH", "HERMES_HOMES_DIR") if k in env]
    check("no app secret or path leaks into the subprocess", leaked == [])
    check("the binding is present and AIB_DB_PATH is blank",
          env["AIB_RUN_TOKEN"] == "tok" and env["AIB_DB_PATH"] == "")
    check("nothing else from os.environ leaks",
          set(env) <= set(cloud_users.ENV_ALLOWLIST) | {"AIB_USER_ID", "AIB_ACCOUNT_ID", "AIB_TODO_ID",
                                                         "AIB_API_URL", "AIB_RUN_TOKEN", "AIB_DB_PATH"})

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
