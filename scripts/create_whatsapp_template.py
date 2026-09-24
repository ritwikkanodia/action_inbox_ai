"""Submit the `new_todo_notice` WhatsApp template for approval and wait.

Meta delivers a business-initiated message outside the 24-hour service window
only as an approved template, so the new-todo notice needs one to reach a
phone that has not written to the app that day. This submits the template
whose six placeholders `push_notify.template_params` fills — source, title,
suggested action, three option labels — under the UTILITY category, then polls
until Meta approves or rejects it. Run it once per WhatsApp Business Account:

    python scripts/create_whatsapp_template.py            # submit + wait
    python scripts/create_whatsapp_template.py --status   # just look

Needs META_WA_WABA_ID and META_WA_ACCESS_TOKEN (with whatsapp_business_management)
in .env. Afterwards set META_WA_NOTICE_TEMPLATE=new_todo_notice. Re-running
with the template already present reports its status instead of failing.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)

NAME = "new_todo_notice"
LANGUAGE = "en"
GRAPH = f"https://graph.facebook.com/{os.environ.get('META_GRAPH_VERSION', 'v23.0').strip() or 'v23.0'}"

# Fixed text carries the structure — placeholders may not contain newlines —
# and Meta rejects a body that starts or ends with a placeholder, or one that
# is mostly placeholders, so the wording around them is not padding.
BODY = (
    "Action Inbox found a new todo from {{1}}: {{2}}\n"
    "Suggested: {{3}}\n\n"
    "How should I handle it?\n"
    "1. {{4}}\n2. {{5}}\n3. {{6}}\n"
    "Reply with a number, or tell me what to do."
)
EXAMPLE = [
    "gmail", "Reply to Bob about the Q4 deck", "Reply confirming the new deadline",
    "Reply with dates", "Decline politely", "Forward to Sam",
]


def _call(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{GRAPH}/{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            err = json.loads(detail)["error"]
            raise SystemExit(f"Graph API {exc.code} (code {err.get('code')}): {err.get('message')}")
        except (ValueError, KeyError):
            raise SystemExit(f"Graph API {exc.code}: {detail[:400]}")


def status(waba: str, token: str) -> dict | None:
    fields = "name,status,category,language,rejected_reason,id"
    found = _call("GET", f"{waba}/message_templates?name={NAME}&fields={fields}", token).get("data") or []
    for t in found:
        if t.get("name") == NAME and t.get("language") == LANGUAGE:
            return t
    return None


def submit(waba: str, token: str) -> dict:
    payload = {
        "name": NAME,
        "language": LANGUAGE,
        "category": "UTILITY",
        "allow_category_change": True,
        "components": [{"type": "BODY", "text": BODY, "example": {"body_text": [EXAMPLE]}}],
    }
    return _call("POST", f"{waba}/message_templates", token, payload)


def main() -> None:
    waba = os.environ.get("META_WA_WABA_ID", "").strip()
    token = os.environ.get("META_WA_ACCESS_TOKEN", "").strip()
    if not waba or not token:
        raise SystemExit("Set META_WA_WABA_ID and META_WA_ACCESS_TOKEN in .env first "
                         "(the WABA id is entry[].id on any webhook delivery).")

    current = status(waba, token)
    if current is None:
        if "--status" in sys.argv:
            print(f"{NAME}: not submitted")
            return
        created = submit(waba, token)
        print(f"submitted {NAME}: id={created.get('id')} status={created.get('status')}")
        current = status(waba, token) or created

    print(f"{NAME}: {current.get('status')} ({current.get('category')}, {current.get('language')})")
    if "--status" in sys.argv:
        return

    # Utility templates usually clear in minutes; give it up to an hour.
    deadline = time.time() + 3600
    while current.get("status") == "PENDING" and time.time() < deadline:
        time.sleep(20)
        current = status(waba, token) or current
        print(f"  … {current.get('status')}")

    if current.get("status") == "APPROVED":
        print(f"approved. Set META_WA_NOTICE_TEMPLATE={NAME} in .env and restart the poller.")
    elif current.get("status") == "REJECTED":
        raise SystemExit(f"rejected: {current.get('rejected_reason')}. Edit BODY and resubmit "
                         "under a new name, or appeal in WhatsApp Manager.")
    else:
        print("still pending; run again with --status later.")


if __name__ == "__main__":
    main()
