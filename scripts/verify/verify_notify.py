"""Verifies the macOS new-todo notification: argv shape, escaping, the
platform/env gate, and that the fathom save helper reports inserts so the
poller only notifies on a real insert. Stubs subprocess — no banner appears."""
import os
import sqlite3
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import notify
from db import init_db, upsert_user, save_fathom_todo


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def _ok(*_a, **_k):
    return mock.Mock(returncode=0, stderr="")


def main() -> None:
    argv = notify.build_osascript('Reply to "Bob" re: C:\\path', "gmail", "high")
    check("uses osascript -e", argv[:2] == ["osascript", "-e"])
    script = argv[2]
    check("title names the source", 'with title "New todo · gmail"' in script)
    check("high importance is flagged", script.startswith('display notification "[high] Reply'))
    check("quotes are escaped", '\\"Bob\\"' in script)
    check("backslashes are escaped", "C:\\\\path" in script)

    long_title = "x" * 500
    argv = notify.build_osascript(long_title, "system", None)
    check("body is truncated", len(argv[2]) < 250 and "…" in argv[2])

    argv = notify.build_osascript("", "fathom", None)
    check("empty title gets a placeholder", '"(untitled)"' in argv[2])

    with mock.patch.object(notify.sys, "platform", "darwin"), \
         mock.patch.dict(os.environ, {"NOTIFY_NEW_TODOS": "1"}), \
         mock.patch.object(notify.subprocess, "run", side_effect=_ok) as run:
        check("fires on darwin when enabled", notify.notify_new_todo("t", "gmail") is True)
        check("subprocess was called once", run.call_count == 1)

    with mock.patch.object(notify.sys, "platform", "darwin"), \
         mock.patch.dict(os.environ, {"NOTIFY_NEW_TODOS": "0"}), \
         mock.patch.object(notify.subprocess, "run", side_effect=_ok) as run:
        check("NOTIFY_NEW_TODOS=0 disables", notify.notify_new_todo("t", "gmail") is False)
        check("...without shelling out", run.call_count == 0)

    with mock.patch.object(notify.sys, "platform", "linux"), \
         mock.patch.dict(os.environ, {"NOTIFY_NEW_TODOS": "1"}), \
         mock.patch.object(notify.subprocess, "run", side_effect=_ok) as run:
        check("no-op off macOS", notify.notify_new_todo("t", "gmail") is False)
        check("...without shelling out", run.call_count == 0)

    with mock.patch.object(notify.sys, "platform", "darwin"), \
         mock.patch.dict(os.environ, {"NOTIFY_NEW_TODOS": "1"}), \
         mock.patch.object(notify.subprocess, "run", side_effect=FileNotFoundError("osascript")):
        check("a failing osascript is swallowed", notify.notify_new_todo("t", "gmail") is False)

    with mock.patch.object(notify.sys, "platform", "darwin"), \
         mock.patch.dict(os.environ, {"NOTIFY_NEW_TODOS": "1"}), \
         mock.patch.object(notify.subprocess, "run",
                           return_value=mock.Mock(returncode=1, stderr="boom")):
        check("a non-zero exit is swallowed", notify.notify_new_todo("t", "gmail") is False)

    # The fathom poller gates on the save helper's return value, so a re-poll of
    # the same meeting must report False and stay silent.
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    meeting = {"recording_id": "rec1", "title": "Standup", "url": "https://x"}
    item = {"description": "Send the deck"}
    check("first fathom save reports an insert", save_fathom_todo(conn, uid, meeting, 0, item) is True)
    check("re-saving the same item reports a dup", save_fathom_todo(conn, uid, meeting, 0, item) is False)

    print("All checks passed.")


if __name__ == "__main__":
    main()
