"""Google Workspace tools for the Hermes executor, served over MCP (stdio).

Registered once in ~/.hermes/config.yaml by scripts/install_hermes_google_mcp.py
and bound to a user and account per run by the AIB_* environment variables the
runner sets. Credentials come from the app's own database; nothing secret
crosses the environment. See docs/superpowers/specs/2026-09-20-hermes-google-workspace-tools-design.md.
"""
