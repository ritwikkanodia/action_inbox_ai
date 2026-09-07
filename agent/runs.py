"""In-process registry of running resolutions, so they can be stopped.

Resolution used to happen inline in the ask-ai request. That left no handle on a
run in flight: the only thing connected to it was the request blocking on it, so
"stop" had nothing to talk to. Runs now execute on a background thread
registered here, which lets a *second* request find a live run and cancel it.

The registry is per-process and deliberately not persisted. A run dies with the
worker that started it; after a restart the todo is simply where it was, since
only completed runs are written to the database.

At most one run exists per (user, todo) — the UI shows one thread, so a second
start replaces the first. Finished runs are kept so a late poll can still read
the result, and swept once they age out.
"""

import logging
import os
import signal
import threading
import time
import uuid

log = logging.getLogger(__name__)

# How long a finished run stays readable before it is swept. Long enough to
# outlive a poll that was in flight when the run ended, short enough that a
# long-lived worker doesn't accumulate threads' worth of transcripts.
RETAIN_SECONDS = 600

RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"

TERMINAL = (DONE, ERROR, CANCELLED)


class CancelToken:
    """A stop signal that can also kill the subprocess a run is waiting on.

    Executors that shell out attach their process here; `cancel` then terminates
    it directly rather than waiting for a cooperative check, because the run is
    blocked on the child for as long as it takes.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._process = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def attach_process(self, process) -> None:
        """Register the live subprocess. Kills it immediately if already cancelled."""
        with self._lock:
            self._process = process
            already = self._cancelled
        if already:
            # Stop arrived between spawning the process and attaching it.
            self._kill(process)

    def detach_process(self) -> None:
        with self._lock:
            self._process = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None:
            self._kill(process)

    @staticmethod
    def _kill(process) -> None:
        """SIGTERM the run, then SIGKILL what is left.

        Signals the whole process group when the executor started one, because
        an agent CLI is rarely a single process — it drives a browser, and
        killing only the parent would leave that browser holding the pipes this
        run is blocked on, so the stop would never land.
        """
        _signal(process, signal.SIGTERM)
        try:
            process.wait(timeout=5)
            return
        except Exception:
            pass  # Ignored the polite signal — take it out.
        _signal(process, signal.SIGKILL)


def _signal(process, sig) -> None:
    try:
        # start_new_session=True makes the child its own group leader, so its
        # pid doubles as the group id.
        os.killpg(os.getpgid(process.pid), sig)
        return
    except (AttributeError, OSError, ProcessLookupError):
        # No process group (already reaped, or not started in one) — fall back
        # to the process itself.
        pass
    try:
        process.send_signal(sig)
    except Exception:
        log.exception("Failed to signal cancelled run's process")


class Run:
    def __init__(self, run_id: str, todo_id: str, user_id: str, thread: list):
        self.run_id = run_id
        self.todo_id = todo_id
        self.user_id = user_id
        self.status = RUNNING
        # Shown while the run is in flight, replaced by the worker's final
        # thread. Seeding it with the pre-run log means a poll that lands early
        # still renders a complete conversation.
        self.thread = list(thread or [])
        self.token = CancelToken()
        self.finished_at: float | None = None

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "status": self.status, "thread": self.thread}


_runs: dict[tuple[str, str], Run] = {}
_lock = threading.Lock()


def _sweep_locked() -> None:
    cutoff = time.monotonic() - RETAIN_SECONDS
    for key, run in list(_runs.items()):
        if run.finished_at is not None and run.finished_at < cutoff:
            del _runs[key]


def get(user_id: str, todo_id: str) -> Run | None:
    with _lock:
        return _runs.get((user_id, todo_id))


def start(user_id: str, todo_id: str, thread: list, work) -> Run:
    """Run `work(token)` on a background thread and register it.

    `work` returns `(thread, status)` and is expected to render its own failures
    into the thread — this registry stays ignorant of what executors can go
    wrong, and only backstops an unexpected exception.
    """
    run = Run(uuid.uuid4().hex, todo_id, user_id, thread)

    with _lock:
        _sweep_locked()
        existing = _runs.get((user_id, todo_id))
        if existing is not None and existing.status == RUNNING:
            return existing
        _runs[(user_id, todo_id)] = run

    def target():
        try:
            final_thread, status = work(run.token)
            run.thread = final_thread
            run.status = status
        except Exception as exc:
            log.exception("Resolution run failed outside the executor")
            run.status = CANCELLED if run.token.cancelled else ERROR
            run.thread = run.thread + [
                {"role": "assistant", "content": f"⚠️ Resolution failed: {exc}"}
            ]
        finally:
            run.finished_at = time.monotonic()

    # Daemon: a wedged run must never keep the web worker from shutting down.
    threading.Thread(target=target, name=f"resolve-{todo_id}", daemon=True).start()
    return run


def discard(user_id: str, todo_id: str) -> None:
    """Forget this todo's run, stopping it first if it is still going.

    Used when the thread is reset: leaving a finished run registered would let
    the next poll re-render the conversation the user just cleared.
    """
    stop(user_id, todo_id)
    with _lock:
        _runs.pop((user_id, todo_id), None)


def stop(user_id: str, todo_id: str) -> bool:
    """Cancel the live run for this todo. Returns whether there was one."""
    run = get(user_id, todo_id)
    if run is None or run.status != RUNNING:
        return False
    run.token.cancel()
    return True
