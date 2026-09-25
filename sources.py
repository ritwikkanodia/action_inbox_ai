"""The source registry shared by the poller and the web UI.

Two layers gate a discovery source. `enabled_sources()` is the server-wide
set from `ENABLED_SOURCES` — what this host can run at all (the macOS-only
sources are opt-in here). On top of that each user can pause any discovery
source from Settings (`db.is_source_enabled`); the poller checks that
per user, per cycle, so a pause takes effect on the next poll without a
restart. The morning digest is a sender, not a discovery source, and has
no per-user toggle.
"""
import os

KNOWN_SOURCES = {"gmail", "fathom", "pocket", "browser_history", "system", "morning_digest"}
# `browser_history` (reads Dia browser history) and `system` (snapshots
# macOS Downloads/Desktop/Documents) are macOS-specific and opt-in.
DEFAULT_ENABLED_SOURCES = {"gmail", "fathom", "pocket", "morning_digest"}

# What Settings shows for each discovery source, in display order.
DISCOVERY_SOURCES = (
    {"name": "gmail", "label": "Gmail",
     "description": "New mail in each connected account is read for action items."},
    {"name": "fathom", "label": "Fathom",
     "description": "Action items from your recorded meetings."},
    {"name": "pocket", "label": "Pocket",
     "description": "Action items Pocket extracts from your recordings."},
    {"name": "browser_history", "label": "Browser history",
     "description": "Pages you visited that look like something to follow up on. macOS only."},
    {"name": "system", "label": "Files",
     "description": "New files in Downloads, Desktop and Documents. macOS only."},
)
DISCOVERY_SOURCE_NAMES = tuple(s["name"] for s in DISCOVERY_SOURCES)


def enabled_sources() -> set[str]:
    """The server-wide set. Unknown names are dropped with a log line."""
    raw = os.environ.get("ENABLED_SOURCES")
    if not raw:
        return set(DEFAULT_ENABLED_SOURCES)
    enabled = {source.strip() for source in raw.split(",") if source.strip()}
    unknown = enabled - KNOWN_SOURCES
    if unknown:
        print(f"[config] Ignoring unknown ENABLED_SOURCES values: {', '.join(sorted(unknown))}")
    return enabled & KNOWN_SOURCES
