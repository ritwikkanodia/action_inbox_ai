"""Verifies run tokens and cloud-user rows in db.py: mint / resolve / expire /
revoke / sweep, and stable per-user uids. No network, no spend.

Usage: python scripts/verify/verify_run_tokens.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "tokens.db")

from db import (  # noqa: E402
    ensure_cloud_user_row,
    init_db,
    mint_run_token,
    resolve_run_token,
    revoke_run_token,
    upsert_user,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


conn = sqlite3.connect(os.environ["DB_PATH"])
conn.row_factory = sqlite3.Row
init_db(conn)
upsert_user(conn, "a@example.com", "A")
upsert_user(conn, "b@example.com", "B")

print("-- run tokens --")
tok = mint_run_token(conn, "u1", "todo_1", 60)
check("token is long and urlsafe",
      len(tok) >= 40 and all(c.isalnum() or c in "-_" for c in tok))
check("plain token is not stored",
      conn.execute("SELECT COUNT(*) FROM run_tokens WHERE token_hash = ?",
                   (tok,)).fetchone()[0] == 0)
check("resolves to user and todo",
      resolve_run_token(conn, tok) == {"user_id": "u1", "todo_id": "todo_1"})
check("unknown token is None", resolve_run_token(conn, "nope") is None)
check("blank token is None", resolve_run_token(conn, "") is None)
check("None token is None", resolve_run_token(conn, None) is None)
expired = mint_run_token(conn, "u1", None, -1)
check("expired token is None", resolve_run_token(conn, expired) is None)
chat = mint_run_token(conn, "u2", None, 60)
check("chat token has todo None", resolve_run_token(conn, chat)["todo_id"] is None)
revoke_run_token(conn, tok)
check("revoked token is None", resolve_run_token(conn, tok) is None)
revoke_run_token(conn, "never-minted")
check("revoking an unknown token is harmless", resolve_run_token(conn, chat) is not None)
mint_run_token(conn, "u1", None, 60)
check("minting sweeps expired rows",
      conn.execute("SELECT COUNT(*) FROM run_tokens").fetchone()[0] == 2)

print("\n-- cloud users --")
uid1 = ensure_cloud_user_row(conn, "u1")
uid2 = ensure_cloud_user_row(conn, "u2")
check("uids start at 20001", uid1 == 20001)
check("uids are distinct", uid1 != uid2 and uid2 == 20002)
check("uid is stable on repeat", ensure_cloud_user_row(conn, "u1") == uid1)
check("rows persist", conn.execute("SELECT COUNT(*) FROM cloud_users").fetchone()[0] == 2)

print("\nall checks passed")
