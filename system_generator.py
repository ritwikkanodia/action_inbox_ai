import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


SYSTEM_PROMPT = """\
You are a system health assistant for a macOS machine. Given a snapshot of the
current system state, identify issues that genuinely need human action.

Be selective — only flag real problems, not normal healthy state. When nothing
needs attention, return an empty list.

Respond with JSON only, matching this exact schema:
{
  "todos": [
    {
      "title": "<action verb + concrete subject>",
      "suggested_action": "<one sentence: what the user should do>",
      "reasoning": "<one sentence: why this is a problem based on the data>"
    }
  ]
}

Title rules:
- MUST start with an action verb: Clean up, Free, Organise, Review, Clear,
  Archive, Delete, Upgrade, Restart, Check.
- MUST name a concrete noun (folder name, volume, specific directory).
- No vague titles like "Manage storage" or "Fix memory".

Signal guidance:
- Disk: flag if used_pct > 85, or a single dir is consuming > 50 GB.
- Folders: flag if file_count > 10 with subdir_count < 2 (flat and crowded),
  or age_buckets["90d+"] > 10 (lots of stale files), or dominant type is
  archive/dmg (uninstalled installers piling up).
- Memory: flag if pressure is "critical" or swap_used_gb > 2.
- Do NOT flag healthy state (normal pressure, <80% disk, tidy folders).
- Do NOT recreate todos already present in "Existing open todos".

Empty todos list is a valid response.
"""


def generate_todos(snapshot_json: str, open_todos: list[str] | None = None) -> list[dict]:
    blocks = [f"System snapshot:\n\n{snapshot_json}"]
    if open_todos:
        blocks.append(
            "Existing open todos (do not duplicate):\n"
            + "\n".join(f"- {t}" for t in open_todos)
        )
    user_content = "\n\n".join(blocks)
    try:
        response = _get_client().chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        todos = parsed.get("todos", [])
        if not isinstance(todos, list):
            return []
        return [t for t in todos if isinstance(t, dict) and t.get("title")]
    except Exception as exc:
        print(f"[system] LLM call failed: {exc}")
        return []
