#!/usr/bin/env bash
set -euo pipefail

# Phase-8 deploy fix: migrate pre-fix overlay-FS data to bind-mount.
# Issue: before commit aedbe6f, daemon ignored ASSISTANT_DATA_DIR and
# wrote to /root/.local/share/0xone-assistant/. After fix, daemon reads
# /app/data. This migration preserves existing turns/conversations/schedules/
# vault on first container start after upgrade.

OLD="/root/.local/share/0xone-assistant"
NEW="${ASSISTANT_DATA_DIR:-/app/data}"

if [ -d "$OLD" ] && [ "$(ls -A "$OLD" 2>/dev/null || true)" ]; then
    # Only migrate if new location is empty/absent — idempotent, safe to re-run
    if [ ! -d "$NEW" ] || [ -z "$(ls -A "$NEW" 2>/dev/null || true)" ]; then
        echo "[entrypoint] migrating data from $OLD → $NEW" >&2
        mkdir -p "$NEW"
        cp -a "$OLD"/. "$NEW"/ 2>/dev/null || true
        echo "[entrypoint] migration complete" >&2
    else
        echo "[entrypoint] $NEW already populated — skipping migration (data: $OLD remains untouched)" >&2
    fi
fi

# Ensure target exists for fresh installs
mkdir -p "$NEW"

# Hand off to daemon
exec "$@"
