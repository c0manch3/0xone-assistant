# syntax=docker/dockerfile:1.7
#
# 0xone-assistant — multi-stage image. Phase 5d.
#
# Stages:
#   1. base    — slim-bookworm + apt deps + non-root `bot` user.
#   2. ghcli   — adds `gh` from cli.github.com (amd64-only).
#   3. builder — uv 0.11.7 + venv at /opt/venv (with --no-editable).
#   4. runtime — final image: COPY venv + gh, symlink bundled claude.
#   * test     — separate target FROM runtime; re-syncs dev deps for CI.
#
# Stage 2 (nodejs + @anthropic-ai/claude-code) is intentionally absent.
# Per spike RQ13, claude-agent-sdk's Linux x86_64 wheel ships its own
# 236 MB ELF `claude` at claude_agent_sdk/_bundled/claude. The runtime
# stage symlinks /usr/local/bin/claude -> the bundled binary so that
# main.py:107-154's PATH-based invocation continues to resolve without
# any source-code change.

# Pinned by digest (manifest-list digest of python:3.12-slim-bookworm
# as of 2026-04-25). Multi-arch manifest list — Docker resolves to the
# linux/amd64 child digest at build time on amd64 hosts. Phase 5d
# builds amd64-only via buildx --platform linux/amd64.
ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:58525e1a8dada8e72d6f8a11a0ddff8d981fd888549108db52455d577f927f77

############################################################
# Stage 1: base
############################################################
FROM ${PYTHON_BASE} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        git \
        sudo \
        passwd \
    && rm -rf /var/lib/apt/lists/*

# Non-root user matching VPS uid/gid (RQ7 verified: VPS user `0xone`
# is uid=1000 gid=1000). Container `user: "1000:1000"` aligns bind-
# mount writes 1:1 with host filesystem ownership.
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home --shell /bin/bash bot

WORKDIR /app

############################################################
# Stage 2: ghcli — gh CLI for phase 7 vault git push prep
############################################################
FROM base AS ghcli

# Install gh from the official cli.github.com apt repo. amd64-only;
# arm64 reopens this whole stage (phase 9). Spike RQ1 verified gh
# 2.91.0 installs cleanly on slim-bookworm with ~14.4 MB delta.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && gh --version

############################################################
# Stage 3: builder — uv + venv (with --no-editable)
############################################################
FROM base AS builder

# Pin uv version for build determinism. The official installer drops
# the binary in /root/.local/bin; we relocate it to /usr/local/bin.
ENV UV_VERSION=0.11.7
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && uv --version

# UV_LINK_MODE=copy: bind-mounted /opt/venv during build benefits from
# real-file copies vs hardlinks across mount boundaries.
# UV_PYTHON_PREFERENCE=only-system: use the slim image's python3.12;
# don't download a managed interpreter.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# pyproject.toml + uv.lock first (cache-friendly). src/ must follow
# because uv_build (build-backend) needs the source tree to produce a
# real wheel when --no-editable is in force.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# --no-editable converts the .pth-pointing-at-/build/src layout into a
# real /opt/venv/lib/python3.12/site-packages/assistant/ directory
# (RQ8 verified). The runtime stage can then COPY just /opt/venv with
# no /build/src needed.
#
# claude-agent-sdk's manylinux_2_17_x86_64 wheel ships its 236 MB ELF
# claude binary at claude_agent_sdk/_bundled/claude (RQ13). uv sync
# pulls it in as a transitive artifact — no nodejs/npm step needed.
#
# Build-time sanity asserts both: (a) assistant package was built as
# a real dir, (b) bundled claude binary is in place + executable.
RUN uv sync --frozen --no-dev --no-editable \
    && test -d /opt/venv/lib/python3.12/site-packages/assistant \
    && test -x /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude

############################################################
# Stage 4: runtime — final slim image
############################################################
FROM ${PYTHON_BASE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bot \
    XDG_CONFIG_HOME=/home/bot/.config \
    XDG_DATA_HOME=/home/bot/.local/share \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_AUTOUPDATER=1

# Runtime OS deps. ca-certificates+curl for HTTPS to api.anthropic.com
# and api.telegram.org; git for phase 7 vault git push path; tini as
# pid 1 to forward signals + reap zombies. NO nodejs (claude is the
# self-contained ELF inside the venv per RQ13).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Mirror non-root user from base stage.
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home --shell /bin/bash bot

# COPY gh from the ghcli stage. gh on slim-bookworm is dynamically
# linked against libc + libcurl-gnutls + libexpat which already exist
# in the runtime base image's libc6/libcurl3-gnutls/libexpat1 from
# python:3.12-slim-bookworm. If a future bookworm point release drops
# either, COPY:
#   --from=ghcli /usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.4* /usr/lib/x86_64-linux-gnu/
#   --from=ghcli /usr/lib/x86_64-linux-gnu/libexpat.so.1*       /usr/lib/x86_64-linux-gnu/
COPY --from=ghcli /usr/bin/gh /usr/bin/gh

# COPY the venv from builder. Bundles:
# - assistant package as a real dir (--no-editable — RQ8)
# - claude-agent-sdk + bundled 236 MB ELF claude binary (RQ13)
# - PyStemmer 3.0.0 cp312 manylinux2014_x86_64 wheel (RQ3)
# - aiogram, pydantic, structlog, etc.
COPY --from=builder /opt/venv /opt/venv

# RQ13 symlink: make the SDK-bundled claude reachable at
# /usr/local/bin/claude so any PATH-based caller — including
# _preflight_claude_auth at main.py:107-154 ("claude --print ping")
# — resolves to the same binary the SDK transport already uses. Zero
# source-code change.
#
# `ln -sf` is intentional: idempotent if a future build COPYs an
# existing /usr/local/bin/claude. The symlink target embeds python3.12
# (matches `requires-python = ">=3.12,<3.13"` in pyproject.toml).
RUN ln -sf /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
           /usr/local/bin/claude

# Build-time sanity: catches a future SDK release that drops the
# bundled binary OR a base image swap that breaks glibc compat
# (manylinux_2_17 floor; bookworm ships glibc 2.36 — well above).
RUN /usr/local/bin/claude --version

USER bot
WORKDIR /app

# tini as PID 1 so SIGTERM from `docker stop` / compose
# `stop_grace_period` reaches the python interpreter cleanly. The
# daemon's stop() writes .last_clean_exit; without proper signal
# forwarding that write would be pre-empted on every container stop.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-m", "assistant"]

############################################################
# Test target (separate; NOT in main multi-stage chain).
# CI invokes via `--target test` for the pytest job. Never pushed to GHCR.
############################################################
FROM runtime AS test

# Switch to root to install dev deps. uv copied from builder (it's
# build-time-only in the runtime image but needed here to re-sync).
USER root

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/

# Re-sync to add pytest/ruff/mypy. --no-editable keeps the assistant
# package as a real dir (consistent with runtime stage).
RUN uv sync --frozen --no-editable \
    && /opt/venv/bin/pytest --version \
    && /opt/venv/bin/ruff --version \
    && /opt/venv/bin/mypy --version

USER bot
ENTRYPOINT []
CMD ["/opt/venv/bin/pytest", "-q", "tests/"]
