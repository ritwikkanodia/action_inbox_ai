from agents import Agent

from agent.tools.browser.tools import (
    browser_click,
    browser_goto,
    browser_observe,
    browser_read_text,
    browser_type,
)

BROWSER_INSTRUCTIONS = """\
You drive a real Chromium browser to complete one specific sub-task handed to you \
by a parent agent. The browser is already logged into the user's accounts where \
applicable; cookies persist across runs.

Loop: observe → decide → act → observe.

Guidelines:
- Start with browser_goto on a URL you can derive from the task, then browser_observe \
  to see what's on the page. Don't guess selectors — use the role and name strings \
  exactly as they appear in the observe output.
- Use browser_read_text when you need the page's content (prices, status text, \
  results) rather than its structure.
- Be cautious with write actions (submitting forms, confirming orders, sending \
  messages). If a write action wasn't clearly authorized by the sub-task, stop and \
  report what you'd do instead of doing it.
- Stop as soon as the sub-task is satisfied and return a concise plain-text result.
- If you hit a blocker — a login wall, a captcha, an ambiguous choice, repeated \
  tool errors — stop and return one short line describing the blocker. Do not \
  guess past it."""

browser_agent = Agent(
    name="BrowserAgent",
    model="gpt-5.2",
    instructions=BROWSER_INSTRUCTIONS,
    tools=[
        browser_goto,
        browser_observe,
        browser_click,
        browser_type,
        browser_read_text,
    ],
)

use_browser = browser_agent.as_tool(
    tool_name="use_browser",
    tool_description=(
        "Delegate a browser task to a sub-agent that drives a real Chromium window "
        "logged into the user's accounts. Give it ONE concrete goal in plain "
        "English (e.g. 'On amazon.com find the status of my most recent order and "
        "return the item name and expected delivery date'). Returns a short text "
        "result. Logged-in sessions persist across calls. Use for actions "
        "web_search cannot do: authenticated dashboards, account-specific info, "
        "form submissions."
    ),
    max_turns=15,
)
