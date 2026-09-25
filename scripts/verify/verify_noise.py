"""Verifies the sign-in / OAuth / verification filter (`pollers/noise.py`) and
that both generators apply it to what the model returns.

The rule is deterministic on purpose: the poller-tier model was told to drop
these and kept emitting them (25 of 27 rejected on real data), so the
prompt is backed by a filter that does not need to be believed. The title
list here is the real rejected set from that data, alongside real tasks that
share words with it ("Complete video verification", "Sign and return the
NDA") and must survive. The OpenAI client is stubbed, so no spend.

Usage: python scripts/verify/verify_noise.py
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("OPENAI_API_KEY", "verify-not-a-real-key")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

from pollers.noise import is_auth_noise_title, is_auth_page
from pollers.browser import generator as browser_gen
from pollers.browser import poller as browser_poller
from pollers.gmail import todo_generator as gmail_gen


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


REJECTED_AUTH_TITLES = [
    "Verify new OpenAI Partner Portal authenticator method",
    "Complete Facebook Login for Business authorization",
    "Complete Facebook two-step verification",
    "Review Facebook login alert near Bangalore",
    "Complete Google sign-in for Instinct",
    "Sign in to Self-driving Inbox",
    "Complete Explee authorization",
    "Log in to Notion to keep your account",
    "Review and revoke unrecognized OpenRouter sign-in",
    "Confirm Google app verification consent",
    "Complete X SSO onboarding",
    "Confirm active-ai Google Account access",
    "Sign in to ExpressVPN",
    "Complete Claude sign-in",
    "Confirm Smallest.ai Google sign-in",
    "Confirm OpenAI Platform sign-in callback",
    "Confirm Google account challenge for Cloudflare Dashboard",
    "Confirm OpenAI Platform OAuth consent",
    "Confirm EF Founder Portal OAuth consent",
    "Complete EF Founder Portal login",
    "Sign in to Google Cloud OAuth consent",
    "Check PostHog new-device login",
    "Review Duckbill sign-in and revoke unrecognized session",
    "Confirm Zoho account",
    "Confirm Zoho account before closure",
]

REAL_TASK_TITLES = [
    "Complete video verification for IDSign application 21710666",
    "Sign and return revised ABS NDA v1.1",
    "Reset Kotak MPIN or report fraud",
    "Verify Google Pay registration with Kotak Bank",
    "Complete MIT Sloan MBA application",
    "Accept Averentia business portfolio invitation",
    "Upload missing KYC documents to Rentomojo",
    "Pay Citi PremierMiles Card bill",
    "Complete Voicepanel Grade waitlist signup",
    "Reply to Madhav re: Aster pilot proposal and NDA",
    "Update Google Workspace tax information",
    "Review access request for AIP_Catalog",
    "Confirm Thursday 3pm demo slot with Priya",
    "Confirm attendance for Group Pitches - Fall'26 BA",
]

AUTH_PAGES = [
    ("https://accounts.google.com/o/oauth2/v2/auth?client_id=x", "Sign in - Google Accounts"),
    ("https://github.com/login?return_to=%2Fsettings", "Sign in to GitHub"),
    ("https://app.explee.com/auth/callback?code=abc", "Explee"),
    ("https://x.com/i/flow/login", "X"),
    ("https://auth.openai.com/authorize?response_type=code", ""),
    ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize", ""),
    ("https://id.atlassian.com/login", "Log in"),
    ("https://www.notion.so/login", "Login | Notion"),
    ("https://example.com/dashboard", "Verify it's you"),  # title alone is enough
    ("https://acme.okta.com/app/sso/saml", ""),
]

REAL_PAGES = [
    ("https://github.com/ritwikkanodia/action_inbox_ai/pull/25", "Fix ordering · Pull Request #25"),
    ("https://www.amazon.in/gp/cart/view.html", "Amazon.in Shopping Cart"),
    ("https://calendly.com/ronke/30min", "Select a Date & Time"),
    ("https://booth.slate.edu/apply/status", "Application Status"),
    ("https://docs.python.org/3/library/re.html", "re — Regular expression operations"),
    ("https://www.makemytrip.com/mytrips/date-change", "Change travel date"),
]


def check_title_filter() -> None:
    print("\n-- todo titles --")
    missed = [t for t in REJECTED_AUTH_TITLES if not is_auth_noise_title(t)]
    check(f"every rejected sign-in title is caught (missed: {missed})", not missed)
    kept = [t for t in REAL_TASK_TITLES if is_auth_noise_title(t)]
    check(f"no real task is caught (false positives: {kept})", not kept)
    check("empty and None are not noise",
          not is_auth_noise_title("") and not is_auth_noise_title(None))


def check_page_filter() -> None:
    print("\n-- browser pages --")
    missed = [u for u, t in AUTH_PAGES if not is_auth_page(u, t)]
    check(f"auth pages dropped before the digest (missed: {missed})", not missed)
    kept = [u for u, t in REAL_PAGES if is_auth_page(u, t)]
    check(f"transactional pages kept (false positives: {kept})", not kept)
    check("the poller's noise check takes the page title",
          browser_poller._is_noise("https://example.com/x", "example.com", "Choose an account")
          and not browser_poller._is_noise("https://example.com/x", "example.com", "Your order"))
    rows = [
        ("https://accounts.google.com/signin/v2", "Sign in", 1, 1),
        ("https://github.com/org/repo/pull/7", "Pull Request #7", 1, 2),
    ]
    per_url = browser_poller._aggregate_visits_by_url(rows)
    check("aggregation drops the sign-in visit and keeps the PR",
          list(per_url) == ["https://github.com/org/repo/pull/7"])


class _StubClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs):
        content = json.dumps(self.payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def check_generators() -> None:
    print("\n-- generators apply the filter to model output --")
    browser_gen._client = _StubClient({"todos": [
        {"should_generate_todo": True, "title": "Complete Google sign-in for Instinct",
         "relevant_link": "https://accounts.google.com/x", "reasoning": "/signin"},
        {"should_generate_todo": True, "title": "Review Pull Request #25 changes",
         "relevant_link": "https://github.com/o/r/pull/25", "reasoning": "/pull/"},
    ]})
    out = browser_gen.generate_todos("digest")
    check("browser generator drops the sign-in todo and keeps the PR",
          [t["title"] for t in out] == ["Review Pull Request #25 changes"])

    event = SimpleNamespace(
        actors=SimpleNamespace(from_=SimpleNamespace(email="no-reply@zoho.com", name="Zoho")),
        content=SimpleNamespace(subject="Confirm your account"),
    )
    gmail_gen._client = _StubClient({
        "should_generate_todo": True, "reasoning": "asks the user to confirm",
        "todo": {"title": "Confirm Zoho account", "importance": "high"},
    })
    result = gmail_gen.generate_todo("thread", event)
    check("gmail generator turns a sign-in todo into a skip with a reason",
          result["should_generate_todo"] is False and result["todo"] is None
          and result["reasoning"].startswith("sign-in/consent step"))

    gmail_gen._client = _StubClient({
        "should_generate_todo": True, "reasoning": "Madhav is waiting on a reply",
        "todo": {"title": "Reply to Madhav re: Aster pilot proposal", "importance": "medium"},
    })
    result = gmail_gen.generate_todo("thread", event)
    check("gmail generator leaves a real todo alone",
          result["should_generate_todo"] is True and result["todo"]["title"].startswith("Reply to Madhav"))

    for src, prompt in (("browser", browser_gen.SYSTEM_PROMPT), ("gmail", gmail_gen.SYSTEM_PROMPT)):
        check(f"{src} prompt names sign-in / consent as a drop",
              "sign-in" in prompt.lower() and "consent" in prompt.lower())


if __name__ == "__main__":
    check_title_filter()
    check_page_filter()
    check_generators()
    print("\nall checks passed")
