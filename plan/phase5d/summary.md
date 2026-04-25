---
phase: 5d
title: Docker migration — reproducible image + bind-mounted state via GHCR
date: 2026-04-25
status: shipped (commit pending; CI build + GHCR push + owner smoke pending)
---

# Phase 5d — Docker migration summary

Phase 5d retires the hand-provisioned systemd + apt + npm + curl stack on VPS in favor of a versioned container image published to GHCR. Owner deploys via `docker compose pull && up -d`. Three host-bind-mounted dirs preserve OAuth, vault, scheduler DB, audit logs across container restarts.

**No `src/assistant/` changes.** Phase 5d is deployment infra only.

## What shipped

- **Dockerfile** (`deploy/docker/Dockerfile`, 211 LOC, 4 stages + test target):
  - `python:3.12-slim-bookworm` digest-pinned base.
  - Stage `ghcli` installs `gh` from official cli.github.com apt repo.
  - Stage `builder` installs uv 0.11.7 + `uv sync --frozen --no-dev --no-editable` (avoids broken `.pth` in runtime — RQ8).
  - Stage `runtime` symlinks `/usr/local/bin/claude → /opt/venv/.../claude_agent_sdk/_bundled/claude` (RQ13 pivot — SDK ships 236 MB Linux ELF; nodejs+npm stage DROPPED).
  - `tini` PID 1 for signal forwarding.
  - `DISABLE_AUTOUPDATER=1` + `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` (devil W3-C1 — actual env var).
  - User `bot` uid 1000 gid 1000.
  - Build-time `claude --version` smoke catches broken bundled binary.

- **docker-compose.yml** (98 LOC, bot + autoheal sidecar):
  - `${TAG:?...}` fail-loud on missing TAG (no `:-latest` footgun).
  - `env_file` object form `[{path, required: true|false}]` (compose v2.20+ syntax — wave-1 H1 was wrong about systemd `-` prefix).
  - Full `~/.claude/` bind-mount (W2-C4 reversal — selective mount breaks claude session/memory features).
  - `user: "1000:1000"` (matches VPS `0xone` uid).
  - `stop_grace_period: 35s` + label `autoheal.stop.timeout=35` (devil W3-C3 — autoheal default `t=10` would override compose grace and SIGKILL before `.last_clean_exit` write).
  - Healthcheck: `pid+/proc/$pid/exe` readlink (catches dead pid AND pid recycle).
  - autoheal sidecar `willfarrell/autoheal:1.2.0` (pinned, devil W3-H1) + own pgrep healthcheck.
  - `mem_limit: 1500m`, `cpus: 2.0` (caps before phase 6 ffmpeg deps).
  - Logging json-file max-size 50m × max-file 5 = 250MB ceiling per container.

- **CI** `.github/workflows/docker.yml` (187 LOC, 4 jobs):
  - Triggers: push main → `:latest` + `:sha-<short>`; push `v*`/`phase*` tags → `:<ref>`; PR → build only (no push).
  - `permissions: { contents: read, packages: write, actions: write }`.
  - amd64-only (PyStemmer no arm64 wheel — RQ3+spike).
  - Jobs: `test` → `build-and-push` (gated `needs: [test]` — QA fix-pack #2) → `smoke` (digest pull, imports + `claude --version`) → `scan` (Trivy split: HIGH/CRITICAL OS, CRITICAL language pkgs).
  - `aquasecurity/trivy-action@0.28.0` pinned.
  - Image-script-injection defense via env-var indirection.

- **`.dockerignore`** (60 lines, 36 non-comment): excludes `.venv`, `.git`, `plan/`, `__pycache__`, `.claude`, `.local`, `.config`, `node_modules`, `dist/`, `*.db*`, `**/secrets.env`, `**/.env`. **`tests/` re-INCLUDED** post-fix-pack (test target needs it).

- **README** (`deploy/docker/README.md`, 343 LOC): install / update / rollback / backup / troubleshooting / OAuth + GH_TOKEN transfer recipes / GHCR private→public visibility flip / pid-namespace gotcha / transcript pruning / migration §F steps.

- **`.gitignore`** updates: `secrets.env`, `**/secrets.env`, `deploy/docker/.env`, `!.env.example`, `!**/.env.example` (devil W3-C2 fix — example was being ignored).

- **CLAUDE.md** updated: deployment section now Docker primary + systemd fallback. Phase 5d listed in shipped phases.

- **`plan/phase4/runbook.md` + `plan/phase5/runbook.md`**: docker compose recipes alongside systemd (systemd marked fallback).

- **`deploy/systemd/0xone-assistant.service`** + README **retained** as documented disabled fallback.

## Pivot note: RQ13 stage 2 elimination

Plan v1 had stage 2 = `apt install nodejs + npm install -g @anthropic-ai/claude-code@2.1.116`. Wave-2 devil discovered:

- `claude_agent_sdk==0.1.63` Linux x86_64 wheel (`py3-none-manylinux_2_17_x86_64.whl`) ships its own **236 MB ELF** at `claude_agent_sdk/_bundled/claude` (verified `file` output: `ELF 64-bit LSB executable, x86-64, GNU/Linux 3.2.0`).
- `subprocess_cli.py:63-94` shows SDK strictly prefers bundled binary over `shutil.which("claude")`.
- `manylinux_2_17` requires glibc ≥ 2.17; bookworm ships glibc 2.36 — comfortable margin.

Result: stage 2 + nodejs runtime DROPPED. Symlink `/usr/local/bin/claude → bundled binary` keeps `_preflight_claude_auth` working with zero `src/` change. Image: 730 MB → ~400 MB (45% smaller, single source of truth via `pyproject.toml` SDK pin instead of dual npm + Dockerfile pinning).

## Pipeline mechanics

- **12 owner Q&A** answered (base image, registry, CI trigger, tag pinning, claude CLI pin policy, systemd retain, dev workflow, container UID, dev tooling in runtime, PyStemmer wheel-first, rootful Docker, image size cap).
- **Devil wave 1 (28 items, 6 CRITICAL)**: GHCR private default, restart-on-unhealthy gap, ~/.claude bloat, smoke fake creds 401, secrets.env compose syntax misconception.
- **Researcher spike wave (RQ1-RQ12 + RQ13)**: arm64 dropped (PyStemmer), claude binary discovery, uv non-editable, root-owned subdirs.
- **Devil wave 2 (22 items, 4 CRITICAL)**: SDK bundles claude (W2-C1 = RQ13 catalyst), env_file object form syntax, healthcheck pid recycle, full ~/.claude reversal.
- **Researcher fix-pack**: description-v2 + implementation-v2.md (859 LOC paste-ready blueprint).
- **Coder**: 5 new files + 4 edits, ruff/mypy clean, 520 tests collect (no src changes).
- **4 parallel reviewers**: code (1C+3H), QA (1C+1H+4M), devops (READY-WITH-POLISH, top 3 ops risks), devil w3 (3C+4H = COMMIT BLOCKED until fix-pack).
- **Fix-pack (12 items)**: 5 CRITICAL + 3 HIGH + 4 MEDIUM applied. 7/7 verification checks pass.

## Key resolutions in fix-pack

- **CRIT** `.dockerignore tests/` removed (4 reviewers concur — would silently break CI test job).
- **CRIT** `build-and-push needs: [test]` (was running parallel, broken images could push).
- **CRIT** `DISABLE_AUTOUPDATER=1` env var name (verified via `strings` SEA blob — `CLAUDE_CODE_DISABLE_AUTOUPDATER` was no-op).
- **CRIT** `.env.example` whitelisted in .gitignore.
- **CRIT** `autoheal.stop.timeout=35` label (overrides willfarrell default t=10, preserves phase-5b clean-exit marker write).
- **HIGH** `willfarrell/autoheal:1.2.0` pinned + autoheal sidecar pgrep healthcheck.
- **HIGH** `aquasecurity/trivy-action@0.28.0` pinned.
- **HIGH** phase 5 runbook: docker recipes added to all sections.
- **MEDIUM** `mem_limit: 1500m`, `cpus: 2.0` resource caps (before phase 6 ffmpeg).
- **MEDIUM** `.env.example` TAG commented out (no 404 on first compose pull).
- **MEDIUM** README healthcheck section corrected (pid-written-during-preflight race, not interpreter swap).

## Owner smoke checklist (post-cutover)

1. SSH VPS, `cd /opt/0xone-assistant && git pull` (brings deploy/docker/* + compose.yml).
2. Owner manual: GHCR Repo → Packages → 0xone-assistant → Settings → Visibility → Public (one-shot after first CI push).
3. SSH: install Docker if absent (`sudo apt-get install -y docker.io docker-compose-plugin && sudo usermod -aG docker 0xone && newgrp docker`).
4. SSH: `~/.backup-$(date +%Y%m%d) ← cp -a ~/.claude ~/.local/share/0xone-assistant ~/.config/0xone-assistant`.
5. SSH: `sudo chown -R 0xone:0xone ~/.claude` (root-owned subdirs from prior `sudo claude` per RQ7).
6. SSH: `systemctl --user disable --now 0xone-assistant.service`.
7. SSH: copy `deploy/docker/.env.example` to `~/.config/0xone-assistant/docker.env`, set `TAG=sha-<latest>`.
8. SSH: `cd /opt/0xone-assistant/deploy/docker && docker compose pull && docker compose up -d && docker compose logs --tail=100 0xone-assistant`.
9. Owner Telegram smoke: ping (phase 2), `запомни X` (phase 4), `запланируй Y через 2 минуты` (phase 5b), `покажи marketplace скиллов` (phase 3 — needs GH_TOKEN).
10. SSH: `sudo reboot`; verify `docker compose ps` shows Up after boot.
11. After 24h green burn-in: `rm ~/.config/systemd/user/0xone-assistant.service && systemctl --user daemon-reload`. Repo copy retained.

## Known carry-forwards (phase 6+)

- **Phase 6 (media)**: extend Dockerfile with ffmpeg / libsndfile if media on VPS, OR keep transcription Mac-side.
- **Phase 7 (vault git push)**: add SSH deploy key bind-mount; compose extensible.
- **Phase 8 (out-of-process scheduler)**: second container `scheduler-worker` sharing `assistant.db` bind-mount + UDS.
- **Phase 9 (hardening)**: `read_only: true`, `cap_drop: [ALL]`, `tmpfs /tmp`, `no-new-privileges`, SBOM, Cosign signing, autoheal → host systemd-timer (drop docker-socket attack surface), GHCR sha-* tag retention auto-prune (currently manual), arm64 reopens.

## References

- `plan/phase5d/description.md` — final plan (~400 LOC).
- `plan/phase5d/implementation-v2.md` — coder blueprint (~860 LOC).
- `plan/phase5d/devil-wave-{1,2,3}.md` — 65 risk items.
- `plan/phase5d/spike-findings.md` + `spike-rq13.md` — live spikes.
- `plan/phase5d/review-{code,qa,devops}.md` — parallel reviewer findings.
- Phase 5a summary (`plan/phase5a/summary.md`) for VPS state being replaced.
- Phase 5b summary (`plan/phase5/summary.md`) for scheduler invariants preserved.
