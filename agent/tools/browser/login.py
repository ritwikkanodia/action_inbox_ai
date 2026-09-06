"""One-time helper: open the agent's Chromium profile so you can log into sites.

Run with:
    python -m agent.tools.browser.login

A headed Chromium window opens using the persistent profile at
~/.action_inbox_ai/chrome-profile. Log into the sites you want the agent to use
(Amazon, your airline, etc.), then return to the terminal and press Enter to
close. Cookies persist for all future agent runs.
"""

from agent.tools.browser.session import close_session, run_in_browser


def _open_blank(ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("about:blank")
    return "ready"


def main() -> None:
    run_in_browser(_open_blank)
    print(
        "Chromium is open. Log into the sites you want the agent to use,\n"
        "then return here and press Enter to close the browser."
    )
    try:
        input()
    except KeyboardInterrupt:
        pass
    finally:
        close_session()


if __name__ == "__main__":
    main()
