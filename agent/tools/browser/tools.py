import logging

from agents import function_tool

from agent.tools.browser.session import run_in_browser

logger = logging.getLogger(__name__)

_MAX_TEXT = 8000


def _page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


@function_tool
def browser_goto(url: str) -> str:
    """Navigate to a URL. Returns the page title and final URL once loaded."""
    def _go(ctx):
        page = _page(ctx)
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        return f"loaded: {page.title()} @ {page.url}"

    try:
        return run_in_browser(_go)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


@function_tool
def browser_observe() -> str:
    """Return a compact ARIA snapshot of the current page.

    Lists interactive elements (links, buttons, inputs, etc.) with role and
    accessible name. Use the role+name pairs as arguments to browser_click and
    browser_type.
    """
    def _obs(ctx):
        page = _page(ctx)
        snap = page.locator("body").aria_snapshot()
        if len(snap) > _MAX_TEXT:
            snap = snap[:_MAX_TEXT] + "\n…[truncated]"
        return snap

    try:
        return run_in_browser(_obs)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


@function_tool
def browser_click(role: str, name: str) -> str:
    """Click an element identified by accessibility role and accessible name.

    Use role and name exactly as they appear in browser_observe output, e.g.
    role='button', name='Confirm booking'.
    """
    def _click(ctx):
        page = _page(ctx)
        page.get_by_role(role, name=name).click(timeout=10000)
        return f"clicked {role!r}:{name!r} -> {page.url}"

    try:
        return run_in_browser(_click)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


@function_tool
def browser_type(role: str, name: str, text: str, submit: bool = False) -> str:
    """Type text into a field identified by role and accessible name.

    If submit=True, press Enter after typing.
    """
    def _type(ctx):
        page = _page(ctx)
        el = page.get_by_role(role, name=name)
        el.fill(text, timeout=10000)
        if submit:
            el.press("Enter")
        return f"typed into {role!r}:{name!r}{' and submitted' if submit else ''}"

    try:
        return run_in_browser(_type)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


@function_tool
def browser_read_text() -> str:
    """Return the visible text content of the current page (truncated)."""
    def _read(ctx):
        page = _page(ctx)
        t = page.locator("body").inner_text()
        if len(t) > _MAX_TEXT:
            t = t[:_MAX_TEXT] + "\n…[truncated]"
        return t

    try:
        return run_in_browser(_read)
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"
