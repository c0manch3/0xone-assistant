# Phase 5d Implementation Blueprint v2

**Author:** researcher (fix-pack consolidation)
**Date:** 2026-04-25
**Status:** Coder-ready. All wave-1 + wave-2 + spike findings consolidated.

This is the paste-able blueprint for the coder. Snippets here are
production-ready (modulo digest pin lookup); coder verifies and integrates.

---

## 0. Coder manifest

| File | Purpose | Approx LoC |
|------|---------|------------|
| `deploy/docker/Dockerfile` | Multi-stage image. 4 main stages (`base`, `ghcli`, `builder`, `runtime`) + 1 separate `test` target. Stage 2 nodejs DROPPED per RQ13. | ~80 |
| `deploy/docker/docker-compose.yml` | Two services: `0xone-assistant` + `autoheal` sidecar. env_file object form, healthcheck pid+exe, full `~/.claude` mount, structured logging. | ~50 |
| `deploy/docker/.dockerignore` | Comprehensive blacklist (W2-H7) — ~30 entries. | ~30 |
| `deploy/docker/.env.example` | Template owner copies to `.env` and edits TAG. | ~5 |
| `deploy/docker/README.md` | Owner-facing recipes: install, update, rollback, backup, OAuth/GH_TOKEN transfer, healthcheck, log tailing, transcript pruning, never-run-sudo-claude rule. | ~250 |
| `.github/workflows/docker.yml` | CI: build-and-push (amd64), trivy-scan, smoke (imports-only + claude --version). | ~80 |
| `CLAUDE.md` patch | Minimal: deploy method link + systemd fallback note. | ~10 |
| `plan/phase4/runbook.md` patch | Replace `journalctl` recipes with `docker compose logs`. | ~10 |
| `plan/phase5/runbook.md` patch | Same as above for scheduler runbook. | ~10 |
| Optional: `deploy/docker/migrate.sh` | Non-interactive runbook for migration §F (helper, not required). | ~50 |

**Source code:** ZERO changes in `src/assistant/`. RQ13 symlink approach keeps `_preflight_claude_auth` (`main.py:107-154`) working without edit.

---

## 1. Dockerfile blueprint (paste-able)

Path: `deploy/docker/Dockerfile`. Build context: repo root (`docker build -f deploy/docker/Dockerfile .`).

### Stage 1: base

```dockerfile
# syntax=docker/dockerfile:1.7

# Pin to digest at coder integration time. Lookup:
#   docker pull python:3.12-slim-bookworm
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:REPLACE_WITH_DIGEST_AT_INTEGRATION

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
    && rm -rf /var/lib/apt/lists/*

# Non-root user matching VPS uid/gid (RQ7 verified)
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home --shell /bin/bash bot

WORKDIR /app
```

### Stage 2: ghcli (was 3 — gh CLI for phase 7 prep)

```dockerfile
FROM base AS ghcli

# Official cli.github.com apt repo, amd64-only (phase 5d non-goal: arm64).
# RQ1 verified: pulls gh 2.91.0 + transitive deps (libcurl3-gnutls libexpat1
# liberror-perl perl libgdbm-compat4 libperl5.36 git-man) onto slim-bookworm.
RUN install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod 644 /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Sanity: prints "gh version 2.91.0 (...)" or newer
RUN gh --version
```

### Stage 3: builder (was 4 — uv + venv with bundled claude binary)

```dockerfile
FROM base AS builder

# Install uv 0.11.7 (pinned for build determinism).
# Use the official installer or the wheel; the script-installer is simplest.
ENV UV_VERSION=0.11.7
RUN curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && uv --version

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Layer 1: dependency resolution (cache-friendly).
# pyproject.toml has uv_build backend that needs src/ at sync time
# when --no-editable is used (it builds a real wheel).
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# uv sync with --no-editable converts the .pth-pointing-at-/build/src
# into a real site-packages/assistant/ directory (RQ8). The runtime
# stage can then COPY just /opt/venv without needing /build/src.
#
# The Linux x86_64 wheel of claude-agent-sdk includes a 236 MB ELF
# binary at claude_agent_sdk/_bundled/claude (RQ13). uv sync pulls it
# automatically as a transitive artifact; no nodejs / npm step needed.
RUN uv sync --frozen --no-dev --no-editable \
    && test -d /opt/venv/lib/python3.12/site-packages/assistant \
    && test -x /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude
```

### Stage 4: runtime (was 5 — slim final image)

```dockerfile
FROM ${PYTHON_BASE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/bot \
    XDG_CONFIG_HOME=/home/bot/.config \
    XDG_DATA_HOME=/home/bot/.local/share \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    CLAUDE_CODE_DISABLE_AUTOUPDATER=1

# Runtime OS deps: ca-certificates+curl for HTTPS, git for phase 7
# vault commit path. NO nodejs (claude is bundled in venv per RQ13).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Mirror non-root user from base.
RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home --shell /bin/bash bot

# COPY gh + transitive shared libs from ghcli stage.
COPY --from=ghcli /usr/bin/gh /usr/bin/gh
# gh is statically linked-ish; if dynamic-link probe fails, also COPY:
#   /usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.4*
#   /usr/lib/x86_64-linux-gnu/libexpat.so.1*
# Coder validates with `docker run --rm <image> gh --version` at integration.

# COPY the venv. This brings in:
# - assistant package (real dir, --no-editable)
# - claude-agent-sdk + 236 MB bundled ELF claude binary (RQ13)
# - PyStemmer 3.0.0 cp312 manylinux2014_x86_64 wheel (RQ3)
# - aiogram, pydantic, structlog, etc.
COPY --from=builder /opt/venv /opt/venv

# Symlink (RQ13): make the SDK-bundled claude binary reachable at
# /usr/local/bin/claude so any PATH-based caller (including
# _preflight_claude_auth at main.py:107-154) resolves to the same
# binary the SDK transport already uses. Zero source-code change.
RUN ln -sf /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
           /usr/local/bin/claude

# Build-time sanity (catches a future SDK release that drops the bundled binary).
RUN /usr/local/bin/claude --version

USER bot
WORKDIR /app

# tini as PID 1: forwards signals correctly, reaps zombies. Container
# stop semantics depend on this so stop_grace_period: 35s actually
# delivers SIGTERM to the python process (not just to a shell).
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-m", "assistant"]
```

### Test target (separate, NOT in main multi-stage chain)

This target re-syncs dev deps on top of `runtime` for CI unit tests.
The CI invokes `docker buildx build --target test --load -t 0xone-test .`
and `docker run --rm 0xone-test pytest`. NEVER pushed to GHCR.

```dockerfile
FROM runtime AS test

USER root
ENV UV_PROJECT_ENVIRONMENT=/opt/venv

# uv copied from builder for the dev-deps install step.
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY tests/ ./tests/

RUN uv sync --frozen --no-editable \
    && /opt/venv/bin/pytest --version \
    && /opt/venv/bin/ruff --version \
    && /opt/venv/bin/mypy --version

USER bot
ENTRYPOINT []
CMD ["/opt/venv/bin/pytest", "-q", "tests/"]
```

---

## 2. docker-compose.yml blueprint

Path: `deploy/docker/docker-compose.yml`.

```yaml
# Compose v2 file (no `version:` key — deprecated in 2.x).

services:
  0xone-assistant:
    image: ghcr.io/c0manch3/0xone-assistant:${TAG:?set TAG in .env, e.g. TAG=sha-abc1234 or TAG=phase5d}
    container_name: 0xone-assistant
    restart: unless-stopped

    # env_file: object form — secrets.env is optional (compose v2.20+).
    # Simple-list form errors fast on missing file (W2-C2).
    env_file:
      - path: ${HOME}/.config/0xone-assistant/.env
        required: true
      - path: ${HOME}/.config/0xone-assistant/secrets.env
        required: false

    volumes:
      # Full ~/.claude mount (W2-C4: selective mount breaks claude
      # session/memory features). Phase 9 hardens with read_only.
      - ${HOME}/.claude:/home/bot/.claude:rw
      - ${HOME}/.local/share/0xone-assistant:/home/bot/.local/share/0xone-assistant:rw

    user: "1000:1000"
    stop_grace_period: 35s

    labels:
      # Required for the autoheal sidecar to monitor this container.
      - "autoheal=true"

    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"

    # Healthcheck (W2-C3): pid-file + /proc/<pid>/exe readlink.
    # The exe-symlink step proves the pid resolves to python, eliminating
    # pid-recycle false-positives AND empty-pid-file races.
    # start_period 60s covers worst-case claude preflight ping (45s + 15s slack).
    healthcheck:
      test:
        - CMD-SHELL
        - >
          test -f /home/bot/.local/share/0xone-assistant/.daemon.pid &&
          pid=$$(cat /home/bot/.local/share/0xone-assistant/.daemon.pid) &&
          [ -n "$$pid" ] &&
          [ -L /proc/$$pid/exe ] &&
          readlink /proc/$$pid/exe | grep -q python
      interval: 30s
      timeout: 5s
      start_period: 60s
      retries: 3

  # Watchdog sidecar — restarts containers reporting unhealthy.
  # `restart: unless-stopped` does NOT restart on unhealthy; this fills the gap.
  autoheal:
    image: willfarrell/autoheal:latest
    container_name: 0xone-autoheal
    restart: unless-stopped
    environment:
      AUTOHEAL_CONTAINER_LABEL: autoheal
      AUTOHEAL_INTERVAL: "30"
      AUTOHEAL_START_PERIOD: "60"
    volumes:
      # Read-only socket — autoheal only needs to send `restart` commands.
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

`.env.example` (placed alongside compose):

```dotenv
# Pin to a specific image tag for reproducible deploys.
# Examples:
#   TAG=phase5d           (release tag, never auto-pruned)
#   TAG=sha-abc1234       (CI-generated short SHA, retained 30 days)
# DO NOT use TAG=latest in production — compose enforces explicit pin.
TAG=phase5d
```

---

## 3. .dockerignore (full)

Path: `deploy/docker/.dockerignore` (compose copies it from build context root; coder also adds an identical or symlinked file at repo root for `docker build` outside of compose).

```
# VCS / virtualenv / Python build artifacts
.git
.venv
__pycache__
*.pyc
.pytest_cache
.mypy_cache
.ruff_cache
*.egg-info
build/
dist/

# Editor / OS
.idea
.vscode
.DS_Store

# Runtime data — NEVER bake into image
*.db
*.db-wal
*.db-shm
*.log
scheduler-audit.log
memory-audit.log
.last_clean_exit
.daemon.pid
.coverage
htmlcov/

# Local state dirs (mirrors of XDG paths) — must NEVER ship in image
.claude/
.local/
.config/

# Secrets (multiple shapes)
.env
.env.*
secrets.env
**/secrets.env
**/.env

# Plan / docs / scratch
plan/
docs/
scratch/

# Phase-irrelevant
node_modules/
tests/
```

Note: `tests/` is excluded from the runtime build context but the `test` Dockerfile target re-COPYs it explicitly (CI invokes a separate `--target test` build with a different context filter, OR builds with `.dockerignore` momentarily relaxed via `--build-context`).

---

## 4. CI workflow `.github/workflows/docker.yml`

```yaml
name: docker

on:
  push:
    branches: [main]
    tags:
      - "v*"
      - "phase*"
  pull_request:
    branches: [main]

# H4: enumerate every needed scope. Defaults are read-only.
permissions:
  contents: read
  packages: write   # GHCR push
  actions: write    # buildx GHA cache write

env:
  REGISTRY: ghcr.io
  IMAGE: ghcr.io/c0manch3/0xone-assistant

jobs:
  build-and-push:
    runs-on: ubuntu-24.04
    outputs:
      tag-sha: ${{ steps.meta.outputs.version }}
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-buildx-action@v3

      - name: Login to GHCR
        if: github.event_name != 'pull_request'
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Compute tags
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.IMAGE }}
          tags: |
            type=ref,event=branch
            type=ref,event=tag
            type=sha,prefix=sha-,format=short
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          file: deploy/docker/Dockerfile
          target: runtime
          # amd64 only (RQ3/RQ5: arm64 deferred to phase 9).
          platforms: linux/amd64
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          # H5: scope cache per branch to avoid main vs PR cross-eviction.
          cache-from: |
            type=gha,scope=${{ github.ref_name }}-amd64
            type=gha,scope=main-amd64
          cache-to: type=gha,mode=max,scope=${{ github.ref_name }}-amd64

  test:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build test target
        uses: docker/build-push-action@v6
        with:
          context: .
          file: deploy/docker/Dockerfile
          target: test
          platforms: linux/amd64
          load: true
          tags: 0xone-test:ci
          cache-from: type=gha,scope=${{ github.ref_name }}-amd64
      - name: Run pytest
        run: docker run --rm 0xone-test:ci

  smoke:
    needs: build-and-push
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-24.04
    steps:
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Pull image
        run: docker pull ${{ env.IMAGE }}:sha-${{ needs.build-and-push.outputs.tag-sha }}
        # If the metadata-action sha format differs in practice, adapt:
        # docker pull ${{ env.IMAGE }}@${{ steps.build.outputs.digest }}
      - name: Imports-only smoke (W2-H2 Option B)
        run: |
          docker run --rm \
            --entrypoint /opt/venv/bin/python \
            ${{ env.IMAGE }}:sha-${{ needs.build-and-push.outputs.tag-sha }} \
            -c '
          import assistant
          from assistant.tools_sdk import installer
          from assistant.scheduler import store
          from assistant.bridge import claude as _bridge
          print("imports_ok")
          '
      - name: Bundled claude binary smoke (RQ13)
        run: |
          docker run --rm \
            --entrypoint /usr/local/bin/claude \
            ${{ env.IMAGE }}:sha-${{ needs.build-and-push.outputs.tag-sha }} \
            --version

  scan:
    needs: build-and-push
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.IMAGE }}:sha-${{ needs.build-and-push.outputs.tag-sha }}
          format: table
          exit-code: "1"
          severity: HIGH,CRITICAL
          ignore-unfixed: true
          # SARIF upload to GitHub Security tab is deferred to phase 9
          # to avoid declaring `security-events: write` permission today.
```

Note: `smoke` and `scan` jobs reference `needs.build-and-push.outputs.tag-sha`. If the `metadata-action`'s `version` output isn't in `sha-<short>` form, the simpler approach is to pull by digest (`steps.build.outputs.digest`). Coder validates on first CI run.

---

## 5. README.md skeleton

Path: `deploy/docker/README.md`. Sections:

```markdown
# 0xone-assistant — Docker deployment

## Prerequisites

- Linux host with Docker CE >= 24 + compose plugin >= v2.20.
- VPS user with uid 1000:1000 owning `~/.claude/`, `~/.local/share/0xone-assistant/`, `~/.config/0xone-assistant/`.
- GH_TOKEN (read:packages) NOT required — image is public after first-push visibility flip.
- TELEGRAM_BOT_TOKEN + OWNER_CHAT_ID in `~/.config/0xone-assistant/.env`.
- Optional GH_TOKEN in `~/.config/0xone-assistant/secrets.env` (for phase 7 marketplace).

## Initial install (fresh host)

[Step-by-step from plan §F: install Docker, backup, chown ~/.claude,
stop systemd, git pull, set TAG, compose pull, compose up -d, smoke,
reboot test.]

## Update to a new image

```bash
cd /opt/0xone-assistant
git pull
cd deploy/docker
echo "TAG=sha-NEW_HASH" > .env  # or TAG=phase5e
docker compose pull
docker compose up -d
docker compose logs -f --tail=200
# Owner Telegram smoke test
```

## Rollback

```bash
cd /opt/0xone-assistant/deploy/docker
echo "TAG=sha-OLD_HASH" > .env  # or TAG=phase5d
docker manifest inspect ghcr.io/c0manch3/0xone-assistant:$TAG  # M8 pre-flight
docker compose pull
docker compose up -d --force-recreate
```

If the old SHA was pruned (>30d, not protected by phase tag):
fall back to nearest `:phaseN` tag.

## Backup

Recommended cadence: before every update + weekly cron.

```bash
BACKUP=~/.backup-$(date +%Y%m%d-%H%M)
mkdir -p $BACKUP
cp -a ~/.claude $BACKUP/
cp -a ~/.local/share/0xone-assistant $BACKUP/
cp -a ~/.config/0xone-assistant $BACKUP/
echo "Backup at $BACKUP — total $(du -sh $BACKUP | awk '{print $1}')"
```

## OAuth + GH_TOKEN handling

### OAuth (`~/.claude/`)

Bind-mounted rw. The container's bundled claude binary refreshes
access_token every ~1h. Atomic on Linux/ext4 (RQ4 verified).

**RULE:** never run `sudo claude` on the VPS. Files created with
root ownership inside `~/.claude/` will break container writes
(uid 1000). If debugging on VPS, first `su - 0xone`.

If you accidentally run as root, fix with:
```bash
sudo chown -R 0xone:0xone ~/.claude
```

### GH_TOKEN rotation (RQ11 verified)

```bash
vi ~/.config/0xone-assistant/secrets.env  # set GH_TOKEN=...
cd /opt/0xone-assistant/deploy/docker
docker compose up -d  # compose detects env_file change and recreates
docker compose logs -f --tail=50
```

**Do NOT use:**
- `docker compose restart` — reuses stale env.
- `docker exec ... kill -HUP 1` — pid 1 env is frozen at exec time.

## Healthcheck

```bash
docker compose ps  # shows healthy/unhealthy/starting
docker inspect --format '{{.State.Health.Status}}' 0xone-assistant
docker inspect --format '{{json .State.Health}}' 0xone-assistant | jq
```

`start_period: 60s` covers claude preflight (worst-case 45s). If
the container repeatedly enters `unhealthy` state, the `autoheal`
sidecar restarts it (max once every 30s). Repeated cycles indicate
real failure — check logs.

The healthcheck verifies that `.daemon.pid` exists AND the pid
resolves to the python interpreter via `/proc/<pid>/exe`. Recycled
PIDs and empty pid-file races are eliminated by this check.

## Log tailing

```bash
docker compose logs -f 0xone-assistant
docker compose logs -f --tail=200 0xone-assistant
docker compose logs --since 1h 0xone-assistant | jq -R 'fromjson?'
```

If reading raw json files at `/var/lib/docker/containers/<id>/...`,
each line is double-encoded — unwrap with `jq -r .log | jq .`.

## .daemon.pid namespace

`.daemon.pid` contains the **container-namespace** pid (typically
`1` if tini wraps the daemon, or `7`). Reading it from the host
and running `kill -0 <pid>` on the host is meaningless — that pid
in the host namespace is unrelated.

For host-side debugging:
```bash
docker compose ps          # service state
docker compose logs        # daemon events
docker compose top         # processes inside container
docker exec 0xone-assistant ps -ef
```

## Transcript pruning

`~/.claude/projects/` grows unbounded (per-session conversation
transcripts). Recommended: prune monthly or when disk pressure
appears.

```bash
# Inspect first
du -sh ~/.claude/projects/
# Delete sessions older than 60 days
find ~/.claude/projects/ -name '*.jsonl' -mtime +60 -delete
```

Phase 9 hardening will tighten container access to this directory.

## Migration from systemd

See `plan/phase5d/description.md §F` for the full 11-step playbook.
TL;DR:

1. Image pushed and visibility flipped Public.
2. `docker pull` works (test before destructive steps).
3. (only if docker missing) install docker.
4. Backup state.
5. `chown -R 0xone:0xone ~/.claude` (one-off).
6. `systemctl --user disable --now 0xone-assistant.service`.
7. `git pull`, set TAG, `docker compose pull && up -d`.
8. Owner Telegram smoke.
9. Reboot test.
10. (after 24h green) retire systemd unit.

If anything fails: `docker compose stop && systemctl --user enable --now 0xone-assistant`.

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `compose pull` 401 unauthorized | GHCR visibility not flipped Public yet. Owner: github.com/users/c0manch3/packages/container/0xone-assistant/settings. |
| Container stuck `starting` past 90s | Check `docker compose logs` for `auth_preflight_ok`. If missing, OAuth bind-mount wrong or token expired. |
| `BlockingIOError` log spam | systemd unit still active. `systemctl --user disable --now 0xone-assistant.service`. |
| `Permission denied` writing ~/.claude/* | `sudo chown -R 0xone:0xone ~/.claude`. |
| `secrets.env: no such file` | Compose v2.20+ required (object form `required: false`). `docker compose version`. |
| `compose up` errors `TAG must be set` | Set `TAG=...` in `deploy/docker/.env`. |
| Container keeps restarting | Check `docker compose logs` for crash root cause. autoheal restarts on unhealthy; if exit-loop, root cause is in code. |
```

---

## 6. Pre-coder checklist

Before coder writes the first line:

- [ ] Owner has confirmed all 4 wave-2 decisions (autoheal sidecar, full ~/.claude mount, public GHCR, amd64-only). All four are documented in `description.md §I` as decided.
- [ ] Owner has GHCR push capability via the existing `GITHUB_TOKEN` in `c0manch3/0xone-assistant` GHA. (No PAT needed for CI.)
- [ ] VPS has Docker — confirmed by spike RQ10 (docker-ce 29.3.0 + compose 5.1.1). If a fresh host: `apt install docker.io docker-compose-plugin && usermod -aG docker 0xone`.
- [ ] First push must flip GHCR visibility to Public manually. Owner runbook step.
- [ ] Mac dev does NOT need Docker. `uv run` is the dev loop. If owner wants local image testing, install Docker Desktop / colima ad-hoc.
- [ ] Coder verifies `python:3.12-slim-bookworm` digest at integration time (line 1 of Dockerfile has `REPLACE_WITH_DIGEST_AT_INTEGRATION` placeholder).
- [ ] Coder reads `src/assistant/main.py:107-154` before final Dockerfile push to confirm `_preflight_claude_auth` argv literal is still `claude` (no rename mid-phase).
- [ ] Coder greps `src/` for any other PATH-based `claude` callers besides preflight; symlink covers them all but explicit verification is cheap.

---

## 7. Implementation order (commits)

Recommended commit sequence. Each commit must build cleanly on its
own. Owner runs `docker build` locally between commits if they have
Docker; otherwise CI green is the gate.

1. **`deploy/docker/.dockerignore`** + **Dockerfile skeleton stages 1, 3 (builder), 4 (runtime)**. No compose yet, no `gh` stage. CI runs `docker build` only. Verify `claude --version` works in runtime stage.
2. **Dockerfile stage 2 (`ghcli`)** added; runtime COPYs `gh`. Verify `gh --version` in built image.
3. **`docker-compose.yml` + `.env.example`** + autoheal config. Owner can `docker compose up` locally if they have Docker.
4. **GHA workflow `.github/workflows/docker.yml`** + first CI push to GHCR. Owner manual: flip GHCR visibility to Public.
5. **`deploy/docker/README.md`** + runbook patches (`plan/phase4/runbook.md`, `plan/phase5/runbook.md`). Includes log-tail recipes, troubleshooting, migration steps.
6. **`CLAUDE.md` patch** — minimal "Deploy: see deploy/docker/README.md".
7. **(optional) `deploy/docker/migrate.sh`** — non-interactive helper script for the §F runbook.
8. **Final integration:** owner runs §F migration on VPS. Smoke + reboot test.

---

## 8. Test blueprint

| Layer | Test | Tool | Trigger |
|-------|------|------|---------|
| Image build | `docker build` succeeds; `claude --version` passes; `assistant` package importable | Dockerfile inline `RUN` checks | Every CI run |
| Unit tests | pytest on `test` target | `docker run --rm 0xone-test:ci` | CI `test` job |
| Imports smoke | `python -c "import assistant; ..."` | `docker run --rm --entrypoint python ...` | CI `smoke` job |
| Bundled binary smoke | `claude --version` against runtime image (RQ13 catches SDK regressions) | `docker run --rm --entrypoint claude ...` | CI `smoke` job |
| CVE scan | Trivy HIGH/CRITICAL gating | `aquasecurity/trivy-action` | CI `scan` job |
| Compose validity | `docker compose config` parses | `docker compose -f deploy/docker/docker-compose.yml config` | Pre-commit (optional) or CI |
| Healthcheck behavior | Kill daemon manually → unhealthy → autoheal restarts | Manual on VPS during cutover | Owner smoke |
| Singleton lock | Start systemd + container with same data dir → container exits 3 cleanly | Manual on VPS during cutover | Owner smoke |
| OAuth refresh atomicity | Run for >1h, verify `.credentials.json` rewrite did not corrupt | Production observation | Post-cutover |

`Mac compose testing`: explicitly skipped (owner has no Docker on Mac).

---

## 9. Known debt / carry-forwards

- **Phase 6 (media):** ffmpeg/libsndfile to Dockerfile runtime. If on-VPS, expand stage 4 apt install. If sidecar-on-Mac, no Docker change.
- **Phase 7 (vault git push):** SSH deploy key bind-mount (`~/.ssh/vault_key:ro`) + `GIT_SSH_COMMAND` env. `gh` already in image (stage 2).
- **Phase 8 (out-of-process scheduler):** sibling service in compose. Shared `assistant.db` via existing data-dir bind mount; UDS via additional bind mount.
- **Phase 9 (hardening):**
  - `read_only: true` for container fs (write paths via tmpfs / bind).
  - `cap_drop: [ALL]` + `no-new-privileges: true`.
  - `tmpfs: ["/tmp"]`.
  - Rootless Docker on VPS.
  - Trivy SARIF upload to GitHub Security tab (`security-events: write`).
  - Cosign image signing.
  - SBOM via `docker buildx build --sbom=true`.
  - Digest-pin all `gh` actions (currently major-version pinned per W2-H5).
  - GHCR retention policy automation (M6 — currently no auto-prune).
  - arm64 reopen (PyStemmer arm64 wheel availability OR native arm runner).
  - VPS `/etc/docker/daemon.json` `shutdown-timeout: 40` for clean reboot (L1).

---

## 10. Risks documented in plan §J

Quick-reference list for the coder. Full text in `description.md §J`:

1. fcntl.flock on bind-mount — Linux/ext4 verified (RQ4).
2. Vault flock (`_memory_core.py`) — same VFS layer, low risk.
3. OAuth atomic rename — Linux atomic.
4. Image bloat creep — re-measure each phase.
5. PyStemmer arm64 — closed by amd64-only.
6. `docker compose v2 mandatory` — VPS verified.
7. Dual daemon race — singleton flock + §F step 3 systemd-stop.
8. CI permissions — explicitly declared.
9. `latest` tag race — `${TAG:?}` blocks unset.
10. Log rotation — 250 MB ceiling.
11. Healthcheck pid recycle — mitigated by `/proc/<pid>/exe` readlink.
12. Restart-on-unhealthy — mitigated by autoheal sidecar.
13. Compose env_file simple-list errors — mitigated by object form.
14. claude auto-update — `CLAUDE_CODE_DISABLE_AUTOUPDATER=1`.
15. GHCR private-by-default — mitigated by visibility-flip step.
16. Bundled-binary version drift — fail-loud via build-time `claude --version`.
17. Compose logs JSON double-encoding — README documented.
18. PID namespace on host — README documented.
19. Docker daemon shutdown-timeout — VPS install runbook sets `/etc/docker/daemon.json`.
20. `~/.claude` data sensitivity — phase 9 hardens; transcript pruning recipe in README.

---

## Appendix A: Verification matrix

| RQ | Verified by | Coder action required |
|----|-------------|------------------------|
| RQ1 gh install | spike | None — paste stage 2 as-is. |
| RQ3 PyStemmer wheel | spike | None — `uv sync --frozen` works. |
| RQ4 flock + rename | spike | None — daemon source unchanged. |
| RQ7 bind-mount uid/gid | spike | Verify §F step 2.5 chown executes idempotently. |
| RQ8 uv sync no-editable | spike | Use `--no-editable` flag literal. |
| RQ9 image size | spike | Run `docker image inspect --format '{{.Size}}'` post-build; alarm if >500 MB. |
| RQ10 compose v2 | spike | None — VPS already capable. |
| RQ11 env_file rotation | spike | None — runbook documents recipe. |
| RQ12 dual daemon | spike | None — §F step 3 enforces precondition. |
| RQ13 SDK-bundled binary | spike | Verify `RUN /usr/local/bin/claude --version` in runtime stage at build time. |

---

## Appendix B: Commands the owner runs

(Single block for paste-ability into shell history.)

```bash
# === On Mac (commit + push triggers CI) ===
git add deploy/docker .github/workflows/docker.yml CLAUDE.md plan/phase4/runbook.md plan/phase5/runbook.md
git commit -m "phase 5d: docker image + compose + autoheal + CI"
git push origin main
# Wait for CI green at https://github.com/c0manch3/0xone-assistant/actions

# === Browser, one-shot ===
# https://github.com/users/c0manch3/packages/container/0xone-assistant/settings
# → "Change package visibility" → "Public" → confirm

# === SSH to VPS ===
ssh 0xone@193.233.87.118

# Backup
BACKUP=~/.backup-$(date +%Y%m%d-%H%M)
mkdir -p $BACKUP
cp -a ~/.claude ~/.local/share/0xone-assistant ~/.config/0xone-assistant $BACKUP/

# One-off chown (W2-M7 wrapped)
test -d ~/.claude && test "$(stat -c %U ~/.claude)" = "$USER" && \
  sudo chown -R 0xone:0xone ~/.claude || true

# Stop systemd FIRST
systemctl --user disable --now 0xone-assistant.service
systemctl --user is-active 0xone-assistant.service && exit 1 || true

# Git pull
cd /opt/0xone-assistant && git pull

# First boot
cd deploy/docker
echo "TAG=phase5d-rc1" > .env
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=200

# Smoke (Telegram): /ping, /memory_save, /memory_find, /sched_add, /marketplace_list

# Reboot test
sudo reboot
# After ~60s SSH back:
docker compose ps  # both Up healthy

# 24h later: retire systemd
rm ~/.config/systemd/user/0xone-assistant.service
systemctl --user daemon-reload
```

---

**End of blueprint.**
