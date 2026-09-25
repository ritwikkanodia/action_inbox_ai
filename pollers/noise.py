"""Deterministic filters for the todos the pollers keep generating and the user
keeps rejecting: sign-in pages, OAuth consent screens, two-step prompts,
"confirm your account" mails, new-device alerts.

Measured on the first three weeks of real data, 25 of the 27 todos in this
family were rejected, and browser history was 93% rejected overall, almost
entirely because a login wall visited on the way to something else reads to
the model as "an incomplete transaction". The prompts now say to drop these,
but the models at the poller price point follow concrete markers better than
abstract rules, so both layers exist: `is_auth_page` drops a visit before it
reaches the browser digest, and `is_auth_noise_title` drops a generated todo
whose title still describes one, whichever source produced it.

Everything here is a plain function over strings so `scripts/verify/verify_noise.py`
can pin the behaviour without an LLM call.
"""
import re
from urllib.parse import urlparse

# Path fragments that mark an authentication step rather than the thing the
# user was trying to reach. Matched case-insensitively against the URL path
# and query.
AUTH_URL_MARKERS = (
    "/login", "/log-in", "/log_in", "/logon",
    "/signin", "/sign-in", "/sign_in", "/signin/", "/accountchooser",
    "/auth/", "/auth?", "/authorize", "/authorization", "/authenticate",
    "/oauth", "/o/oauth2", "/openid", "/saml", "/sso", "/consent",
    "/callback", "/session/new", "/users/sign_in",
    "/2fa", "/two-factor", "/two-step", "/twostep", "/mfa", "/otp",
    "/verify-email", "/email-verification", "/verify_email", "/challenge",
    "/password/reset", "/reset-password", "/forgot-password",
)

# Hosts that exist only to sign people in. Exact match, or a suffix match on
# the parts after the first dot for the identity-provider families.
AUTH_HOSTS = {
    "accounts.google.com", "login.microsoftonline.com", "login.live.com",
    "appleid.apple.com", "login.salesforce.com", "id.atlassian.com",
    "auth.openai.com", "github.com/login",
}
AUTH_HOST_SUFFIXES = (".auth0.com", ".okta.com", ".onelogin.com", ".auth.us-east-1.amazoncognito.com")
AUTH_HOST_PREFIXES = ("accounts.", "account.", "auth.", "login.", "signin.", "sso.", "id.", "identity.")

# Page titles that name the auth step itself.
_AUTH_PAGE_TITLE_RE = re.compile(
    r"\b(sign[ -]?in|log[ -]?in|login|logon|sign[ -]?on|choose an account|"
    r"verify it'?s you|2-step|two-step|two-factor|2fa|one-time (code|password)|"
    r"authori[sz]e|authori[sz]ation|consent|continue with (google|apple|microsoft|github)|"
    r"authenticat(e|ion|or)|oauth|single sign)\b",
    re.IGNORECASE,
)

# Generated todo titles that still describe an auth step. Deliberately
# narrower than the page-title pattern: "verification" on its own is a real
# action (video KYC, document verification), so it only counts next to a
# sign-in word; "confirm ... account" is the account-activation mail family.
_AUTH_TODO_TITLE_RE = re.compile(
    r"\b(sign[ -]?in(to)?|log[ -]?in(to)?|login|logon|sign[ -]?on|sso|oauth|"
    r"authori[sz]e|authori[sz]ation|consent|authenticat(e|ion|or)|"
    r"(two|2)[ -](step|factor)|2fa|mfa|"
    r"(unrecogni[sz]ed|unknown|new|suspicious)[ -](sign[ -]?in|login|session|device)|"
    r"(login|sign[ -]?in|signin) (alert|attempt|notification|callback)|"
    r"account challenge|choose an account|verify it'?s you|"
    r"(verify|verification) (code|device)|verification (of )?(your )?(sign[ -]?in|login)|"
    r"password (reset|change)|reset (your )?password|"
    r"(confirm|verify|activate) (your |the )?[\w.'-]+ (google |gmail |work |business |new )?account)\b",
    re.IGNORECASE,
)


def is_auth_page(url: str | None, title: str | None = None) -> bool:
    """True when a browser visit is a sign-in, OAuth, consent or verification
    step. Checked before the visit reaches the digest the model sees."""
    if url:
        try:
            parsed = urlparse(url.strip())
        except ValueError:
            parsed = None
        if parsed is not None:
            host = (parsed.netloc or "").lower().split("@")[-1].split(":")[0]
            if host in AUTH_HOSTS or f"{host}{parsed.path}".lower().rstrip("/") in AUTH_HOSTS:
                return True
            if host.startswith(AUTH_HOST_PREFIXES) and host.count(".") >= 2:
                return True
            if host.endswith(AUTH_HOST_SUFFIXES):
                return True
            target = f"{parsed.path}?{parsed.query}".lower()
            if any(marker in target for marker in AUTH_URL_MARKERS):
                return True
    if title and _AUTH_PAGE_TITLE_RE.search(title):
        return True
    return False


def is_auth_noise_title(title: str | None) -> bool:
    """True when a generated todo's title describes signing in, granting
    consent, verifying a device or confirming an account — not a task."""
    if not title:
        return False
    return bool(_AUTH_TODO_TITLE_RE.search(title))
