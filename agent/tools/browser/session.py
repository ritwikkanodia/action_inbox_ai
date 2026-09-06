"""Dedicated worker thread that owns Playwright.

Playwright's sync API binds its dispatcher to the thread that first called it.
Flask serves requests on a thread pool, and openai-agents Runner.run_sync calls
tools from worker threads, so calls from different request handlers hit
different threads and crash with `greenlet.error`.

This module runs Playwright on one long-lived worker thread and exposes
`run_in_browser(fn)` for any caller to marshal a function onto that thread and
get the result back synchronously.
"""

import atexit
import logging
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

_ROOT = Path.home() / ".action_inbox_ai"
_PROFILE_DIR = _ROOT / "chrome-profile"
_TRACE_DIR = _ROOT / "traces"

T = TypeVar("T")

_SHUTDOWN = object()

_worker: threading.Thread | None = None
_queue: "queue.Queue[tuple[Callable, threading.Event, list]] | None" = None
_lock = threading.Lock()


def _launch_context(pw):
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
        slow_mo=300,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    trace_started = False
    try:
        ctx.tracing.start(screenshots=True, snapshots=True, sources=True)
        trace_started = True
    except Exception:
        logger.exception("Failed to start Playwright tracing")
    return ctx, trace_started


def _is_alive(ctx) -> bool:
    if ctx is None:
        return False
    try:
        # browser is None for persistent contexts; probe via pages instead.
        _ = ctx.pages
        return ctx.browser is None or ctx.browser.is_connected()
    except Exception:
        return False


def _worker_loop(q: "queue.Queue[tuple[Callable, threading.Event, list]]") -> None:
    pw = None
    ctx = None
    trace_started = False
    try:
        pw = sync_playwright().start()
        ctx, trace_started = _launch_context(pw)

        while True:
            item = q.get()
            if item is _SHUTDOWN:
                break
            fn, done, slot = item
            if not _is_alive(ctx):
                logger.warning("Browser context closed; relaunching")
                try:
                    ctx.close()
                except Exception:
                    pass
                try:
                    ctx, trace_started = _launch_context(pw)
                except Exception as e:
                    slot.append(("err", e))
                    done.set()
                    continue
            try:
                slot.append(("ok", fn(ctx)))
            except Exception as e:
                slot.append(("err", e))
            finally:
                done.set()
    finally:
        if ctx is not None and trace_started:
            try:
                _TRACE_DIR.mkdir(parents=True, exist_ok=True)
                path = _TRACE_DIR / f"trace-{datetime.now():%Y%m%d-%H%M%S}.zip"
                ctx.tracing.stop(path=str(path))
                logger.info("Saved Playwright trace to %s", path)
            except Exception as e:
                logger.warning("Could not finalize Playwright trace: %s", e)
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _ensure_worker() -> "queue.Queue":
    global _worker, _queue
    with _lock:
        if _worker is None or not _worker.is_alive():
            _queue = queue.Queue()
            _worker = threading.Thread(
                target=_worker_loop, args=(_queue,), name="playwright-worker", daemon=True
            )
            _worker.start()
            atexit.register(close_session)
        return _queue


def run_in_browser(fn: Callable[..., T]) -> T:
    """Run `fn(ctx)` on the dedicated Playwright thread and return its result.

    `fn` receives the BrowserContext as its single argument. Use `ctx.pages[0]`
    or `ctx.new_page()` inside to get a Page.
    """
    q = _ensure_worker()
    done = threading.Event()
    slot: list = []
    q.put((fn, done, slot))
    done.wait()
    status, value = slot[0]
    if status == "err":
        raise value
    return value


def close_session() -> None:
    """Signal the worker thread to shut down. Safe to call multiple times."""
    global _worker, _queue
    with _lock:
        if _queue is not None:
            try:
                _queue.put(_SHUTDOWN)
            except Exception:
                pass
        worker = _worker
        _worker = None
        _queue = None
    if worker is not None and worker.is_alive():
        worker.join(timeout=5)
