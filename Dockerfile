# Action Inbox on Railway, with a per-user Hermes executor.
#
# Built on the published Hermes image, which already carries the Hermes venv
# (/opt/hermes/.venv), the agent-browser CLI and a headless Chromium under
# PLAYWRIGHT_BROWSERS_PATH, all world-readable (the image seals /opt/hermes as
# a+rX). This app gets its own venv at /app/venv so the two never share a
# dependency tree. The Hermes entrypoint (s6, gateway, dashboard) is replaced
# by scripts/cloud_start.sh: nothing of Hermes runs as a service here, only
# `hermes chat` per turn, spawned by the app as that user's own OS account.
#
# Pinned to the release installed on the dev Mac (Hermes v0.21.4 = 2026.9.21).
# Bump deliberately; a newer Hermes may change the config keys in
# agent/cloud_users.py.
ARG HERMES_IMAGE=nousresearch/hermes-agent:v2026.9.21
FROM ${HERMES_IMAGE}

USER root

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HERMES_CLOUD=1 \
    HERMES_HOMES_DIR=/data/hermes \
    DB_PATH=/data/gmail_events.db \
    UPLOADS_DIR=/data/uploads \
    HERMES_BIN=/opt/hermes/.venv/bin/hermes \
    HERMES_BROWSER_HEADED=0 \
    TODO_EXECUTOR=hermes

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY requirements.txt .
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --upgrade pip \
    && /app/venv/bin/pip install -r requirements.txt

# The repo, readable by every uid: the per-user MCP server process imports it.
# .dockerignore keeps the local database, .env and venv out of the image.
COPY . .
RUN chmod -R a+rX /app && mkdir -p /data

ENV PATH="/app/venv/bin:${PATH}"

EXPOSE 8000
ENTRYPOINT []
CMD ["bash", "/app/scripts/cloud_start.sh"]
