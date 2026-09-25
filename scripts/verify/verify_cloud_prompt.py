"""Verifies what HERMES_CLOUD=1 changes for the agent's framing and readiness:
the login-handoff paragraph tells the agent it is signed in to nothing and
never to ask for a credential; readiness needs the homes directory; Settings
describes the cloud Hermes as what it is. No network, no spend.

Usage: python scripts/verify/verify_cloud_prompt.py
"""
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "prompt.db")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-not-a-real-key")


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def load_prompt():
    import agent.hermes_prompt as hp
    return importlib.reload(hp)


def main() -> None:
    print("-- prompt selection --")
    os.environ.pop("HERMES_CLOUD", None)
    os.environ.pop("HERMES_PERSISTENT_BROWSER", None)
    local = load_prompt()
    check("local prompt keeps the everyday-browser handoff",
          "in their own everyday browser" in local.INSTRUCTIONS
          and "signed in to nothing" not in local.INSTRUCTIONS)

    os.environ["HERMES_CLOUD"] = "1"
    cloud = load_prompt()
    for name, text in (("first-turn", cloud.INSTRUCTIONS), ("follow-up", cloud.FOLLOWUP_INSTRUCTIONS)):
        check(f"cloud {name} prompt says the agent is signed in to nothing",
              "signed in to nothing" in text)
        check(f"cloud {name} prompt forbids asking for a credential",
              "Never ask for a password" in text or "never ask for a password" in text.lower())
        check(f"cloud {name} prompt has no macOS handoff mechanics",
              "AppleScript" not in text and "everyday browser" not in text)
        check(f"cloud {name} prompt ends the wall with an ask_user block",
              "ask_user" in text and "Skip this" in text)
    check("cloud wins over the persistent-browser flag",
          (os.environ.__setitem__("HERMES_PERSISTENT_BROWSER", "1") or True)
          and "signed in to nothing" in load_prompt().INSTRUCTIONS)
    os.environ.pop("HERMES_PERSISTENT_BROWSER", None)
    check("the placeholders are gone", "%%LOGIN_HANDOFF" not in cloud.INSTRUCTIONS
          and "%%LOGIN_HANDOFF_FOLLOWUP" not in cloud.FOLLOWUP_INSTRUCTIONS)

    print("\n-- readiness and Settings --")
    fake_bin = os.path.join(_tmp, "hermes")
    open(fake_bin, "w").close()
    os.environ["HERMES_BIN"] = fake_bin
    from agent import cloud_users, executor
    cloud_users.HOMES_DIR = os.path.join(_tmp, "missing-homes")
    ready, reason = executor.readiness("hermes")
    check("cloud readiness fails without the homes directory",
          ready is False and "HERMES_HOMES_DIR" in reason and "missing-homes" in reason)
    os.makedirs(cloud_users.HOMES_DIR)
    check("cloud readiness passes once it exists", executor.readiness("hermes") == (True, None))
    described = executor.describe_executors()
    hermes = next(o for o in described["options"] if o["name"] == "hermes")
    check("Settings describes the cloud Hermes honestly",
          hermes["description"] == executor.CLOUD_HERMES_DESCRIPTION and hermes["ready"] is True)
    check("the executor table itself is untouched",
          executor.EXECUTORS[0]["description"].startswith("Browser, terminal"))

    os.environ.pop("HERMES_CLOUD", None)
    check("local readiness ignores the homes directory",
          executor.readiness("hermes") == (True, None))
    local_desc = next(o for o in executor.describe_executors()["options"] if o["name"] == "hermes")
    check("local Settings keeps the local description",
          local_desc["description"].startswith("Browser, terminal"))

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
