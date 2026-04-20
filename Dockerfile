# syntax=docker/dockerfile:1.7
#
# 0xone-assistant — production image.
#
# Base: uv's official Debian bookworm-slim image with Python 3.12 and uv
# pre-installed (~180 MB). Keeps parity with midomis-bot; no separate
# builder stage because dependencies are pure Python + small C ext shims
# (Pillow, lxml) already provided as manylinux wheels for py3.12.
#
# Runs as root. This matches the midomis pattern: OAuth credentials live
# in `/root/.claude/` via bind-mount from the host's `/home/0xone/.claude`.
# Switching to a non-root UID would require re-chowning the bind-mounted
# OAuth dir on the host, which is out of scope for phase 8.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ── System deps ──────────────────────────────────────────────────────────
# git + openssh-client: phase-8 vault auto-commit (GitPython push via SSH).
# gh: GitHub CLI, used by tools/gh/ read-only helpers. Installed from the
#     official cli.github.com apt repo because Debian bookworm does not
#     ship `gh` in its default archive.
# ca-certificates + curl: TLS trust store + fetcher for the gh repo key.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        openssh-client; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends gh; \
    apt-get purge -y --auto-remove gnupg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# ── Node.js 20 + Claude Code CLI ─────────────────────────────────────────
# The `claude` CLI is a Node.js tool distributed as the npm package
# `@anthropic-ai/claude-code`. The Python `claude-agent-sdk` invokes it
# via subprocess, so the binary must live on PATH inside the container.
# Phase-2 preflight (daemon startup) hard-stops if `claude --version`
# is not resolvable — see commit 54d41b0.
#
# NodeSource's setup script pins the apt repo for Node.js 20.x (LTS);
# we then install the CLI globally and smoke-test it in the same layer
# so a missing/broken binary fails the build rather than runtime.
RUN set -eux; \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    npm install -g @anthropic-ai/claude-code@latest; \
    node --version; \
    npm --version; \
    claude --version; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /root/.npm

# uv tunables: never download a different Python interpreter, always use the
# one baked into the base image; install into the project's .venv.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ── Dependency layer ─────────────────────────────────────────────────────
# Copy only the manifest + lockfile first so that code edits don't bust the
# (expensive) dependency install cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && rm -rf /root/.cache

# ── Project layer ────────────────────────────────────────────────────────
# The full source (src/, tools/, skills/, daemon/, etc.) lands here. The
# .dockerignore drops caches, tests, secrets, and local state.
COPY . .

# Install the project itself (editable-equivalent, from local sources).
RUN uv sync --frozen --no-dev \
    && rm -rf /root/.cache

# Data dir is a mount point; create it so first-boot works even if the
# host-side volume is empty.
RUN mkdir -p /app/data

# Copy entrypoint shim for overlay-FS migration (phase-8 deploy fix).
# Migrates /root/.local/share/0xone-assistant/ (pre-aedbe6f) to the
# bind-mounted /app/data on first start after upgrade. Idempotent.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# No EXPOSE / HEALTHCHECK: phase 8 is Telegram long-polling only, with no
# HTTP surface. Phase 9 will add /health + /metrics and the corresponding
# directives.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "assistant"]
