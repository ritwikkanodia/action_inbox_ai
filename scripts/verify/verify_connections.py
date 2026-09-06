"""Verifies account-aware source_connections helpers."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import (
    init_db,
    upsert_user,
    get_source_connection,
    set_source_credentials,
    clear_source_connection,
    list_gmail_accounts,
)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")

    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "a"}, account_id="a@example.com")
    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "b"}, account_id="b@example.com")

    accounts = list_gmail_accounts(conn, user_id)
    check("two gmail accounts listed", len(accounts) == 2)
    check("accounts carry addresses",
          {a["account_id"] for a in accounts} == {"a@example.com", "b@example.com"})

    row_a = get_source_connection(conn, user_id, "gmail", "a@example.com")
    check("account a fetched by id", row_a["credentials"]["token"] == "a")
    check("result carries account_id", row_a["account_id"] == "a@example.com")

    row_b = get_source_connection(conn, user_id, "gmail", "b@example.com")
    check("account b independent", row_b["credentials"]["token"] == "b")

    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "a2"}, account_id="a@example.com")
    check("reconnect does not duplicate", len(list_gmail_accounts(conn, user_id)) == 2)
    check("reconnect updates credentials",
          get_source_connection(conn, user_id, "gmail", "a@example.com")
          ["credentials"]["token"] == "a2")

    check("None account_id falls back to first",
          get_source_connection(conn, user_id, "gmail") is not None)

    set_source_credentials(conn, user_id, "fathom", "api_key", {"api_key": "k"})
    check("fathom stored under ''",
          get_source_connection(conn, user_id, "fathom")["account_id"] == "")

    clear_source_connection(conn, user_id, "gmail", "a@example.com")
    check("scoped clear removes one", len(list_gmail_accounts(conn, user_id)) == 1)
    check("other account survives",
          get_source_connection(conn, user_id, "gmail", "b@example.com") is not None)
    check("fathom untouched by gmail clear",
          get_source_connection(conn, user_id, "fathom") is not None)

    set_source_credentials(conn, user_id, "gmail", "oauth2",
                           {"token": "c"}, account_id="c@example.com")
    clear_source_connection(conn, user_id, "gmail")
    check("unscoped clear removes all", list_gmail_accounts(conn, user_id) == [])
    check("fathom still untouched",
          get_source_connection(conn, user_id, "fathom") is not None)

    print("\nAll connection-helper checks passed.")


if __name__ == "__main__":
    main()
