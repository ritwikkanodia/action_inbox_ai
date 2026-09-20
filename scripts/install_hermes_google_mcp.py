"""Register (or remove) the Google Workspace MCP server in Hermes' config.

Writes one entry, `mcp_servers.action_inbox_google`, into ~/.hermes/config.yaml
(or $HERMES_HOME/config.yaml). The entry runs this repo's venv python on
`agent/google_mcp` with the repo as cwd, and forwards the three AIB_* variables
as `${VAR}` references that Hermes expands from the launching process — the
runner sets them per turn, so nothing per-user lives in the config.

Idempotent: re-running replaces the entry. Hermes' own config writes already
drop YAML comments, so this doing the same costs nothing new.

Usage:
  python scripts/install_hermes_google_mcp.py           # install / update
  python scripts/install_hermes_google_mcp.py --remove
"""
import os
import sys

import yaml

SERVER_NAME = "action_inbox_google"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def config_path() -> str:
    home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(home, "config.yaml")


def entry() -> dict:
    return {
        "command": sys.executable,
        "args": ["-m", "agent.google_mcp"],
        "cwd": REPO_ROOT,
        "env": {
            "AIB_USER_ID": "${AIB_USER_ID}",
            "AIB_ACCOUNT_ID": "${AIB_ACCOUNT_ID}",
            "AIB_DB_PATH": "${AIB_DB_PATH}",
        },
    }


def main(argv: list[str]) -> int:
    path = config_path()
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    if not isinstance(config, dict):
        print(f"{path} is not a mapping; refusing to edit it.", file=sys.stderr)
        return 1
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    if "--remove" in argv:
        if servers.pop(SERVER_NAME, None) is None:
            print(f"{SERVER_NAME} was not registered in {path}.")
            return 0
        action = "Removed"
    else:
        servers[SERVER_NAME] = entry()
        action = "Registered"
    config["mcp_servers"] = servers

    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=False)
    os.replace(tmp, path)

    print(f"{action} {SERVER_NAME} in {path}")
    if action == "Registered":
        print(yaml.safe_dump({SERVER_NAME: entry()}, sort_keys=False))
        print("Verify with:\n  AIB_USER_ID=<user id> AIB_DB_PATH=$(pwd)/gmail_events.db "
              f"hermes mcp test {SERVER_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
