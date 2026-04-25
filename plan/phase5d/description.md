# Phase 5d — Docker migration (reproducible image + bind-mounted state)

**Pivot note:** Phase 5a put the daemon on VPS `193.233.87.118` with a hand-provisioned Python 3.12 / `uv` / Node 20 + `@anthropic-ai/claude-code@2.1.116` / `gh` / PyStemmer / systemd user unit stack. Phase 5b shipped the scheduler on top. Owner discovered the provisioning cliff: every new host (or a nuked VPS) replays the same manual shell recipe. Phase 5d replaces the systemd unit + manual apt/npm/curl provisioning with a versioned container image published to GHCR plus a `docker-compose.yml` that wires the same three host-persistent state trees (OAuth creds, data dir, env dir) as bind mounts. No code changes in `src/assistant/`.

**Wave-2 architectural reset (2026-04-25):** RQ13 spike confirmed `claude-agent-sdk==0.1.63` Linux x86_64 wheel ships its own 236 MB ELF `claude` binary at `claude_agent_sdk/_bundled/claude`. The SDK transport prefers that path over `$PATH` (`subprocess_cli.py:63-94`). Stage 2 (nodejs + `npm install -g @anthropic-ai/claude-code`) is **DROPPED** — single-source-of-truth for the claude binary is now the SDK wheel. A one-line symlink in the runtime stage keeps `_preflight_claude_auth`'s argv `claude` resolution working without source changes.

## A. Goal & non-goals

**Goal.** One command boots the bot on any Linux/amd64 host with Docker:
```bash
cd /opt/0xone-assistant && docker compose pull && docker compose up -d
```
The image `ghcr.io/c0manch3/0xone-assistant:<tag>` bakes Python 3.12 + `uv` + `gh` + PyStemmer + the app's pinned venv (which transitively brings the SDK-bundled `claude` binary). Three bind mounts hold all stateful data on the host; no container-managed volumes; destroying the container never destroys OAuth, vault, scheduler DB, audit logs. Image is built and pushed by a GitHub Actions workflow on `main` + tag push; `docker compose pull && up -d --force-recreate` ships new daemon version. Systemd unit retired from runtime (repo copy retained as documented fallback).

**Non-goals (phase 5d):**
- Kubernetes / swarm / multi-host scale.
- Second container running scheduler out-of-process (phase 8).
- Dev hot-reload (no `/app` bind mount; source baked).
- macOS-native Docker Desktop production optimization — Mac stays dev host with `uv run`.
- Windows support.
- GPU / whisper / media deps (phase 6).
- SSH deploy key mount (phase 7).
- Image signing / SBOM / Cosign / Trivy SARIF upload to security tab (phase 9).
- `read_only: true`, cap_drop, no-new-privileges, tmpfs hardening (phase 9).
- `ANTHROPIC_API_KEY` fallback (CLAUDE.md invariant).
- Moving singleton-lock model — `.daemon.pid` flock stays inside container; bind-mounted data dir is single serialization point.
- Caddy / reverse proxy changes.
- **Single-platform: amd64 only.** PyStemmer publishes no arm64 wheel on PyPI; qemu-emulated compile adds ~5 min CI for zero production benefit (VPS is amd64). arm64 reopens phase 9 if owner adds an arm64 host or wants darwin-arm64 container dev loop.
- Rootless Docker on VPS (phase 9).

## B. Image design

**Base:** `python:3.12-slim-bookworm` pinned by digest. Rationale: bookworm matches Ubuntu 24.04 glibc lineage; slim ~80 MB layer vs 1 GB for full bookworm; `gh` repo supports bookworm officially; glibc 2.36 satisfies the `manylinux_2_17` floor of the SDK-bundled claude ELF.

**Multi-stage (4 stages — stage 2 nodejs DROPPED per RQ13):**

**Stage 1 `base`:** FROM slim-bookworm digest. Install `ca-certificates curl gnupg git sudo`. Non-root user `bot` (uid 1000 gid 1000). Env: `DEBIAN_FRONTEND=noninteractive`, `PIP_NO_CACHE_DIR=1`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`.

**Stage 2 `ghcli`** (was 3): FROM `base`. Install `gh` via official `cli.github.com/packages` apt repo (key import + sources.list entry with `arch=amd64` — phase 5d amd64-only; arm64 reopens this). Target ≥ 2.45 (RQ1: 2.91.0 verified).

**Stage 3 `builder`** (was 4): FROM `base`. Install `uv` 0.11.7. PyStemmer wheel-first (no `build-essential` — RQ3 amd64 wheel verified). `UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable` (excludes pytest/ruff/mypy AND converts `.pth`-with-/build/src to a real `site-packages/assistant/` dir so the runtime stage can COPY just `/opt/venv` without COPY'ing `/build/src`). Verified by RQ8.

**Stage 4 `runtime`** (was 5): FROM same slim-bookworm digest. Runtime deps: `ca-certificates curl git` (no nodejs — claude CLI is a self-contained ELF inside the venv). COPY:
- `--from=ghcli /usr/bin/gh` + transitive shared libs (RQ1 enumerates).
- `--from=builder /opt/venv` (venv with real `assistant/` package thanks to `--no-editable`; the venv also contains the SDK-bundled `claude` ELF at `claude_agent_sdk/_bundled/claude`).
- `RUN ln -s /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude /usr/local/bin/claude` — so `_preflight_claude_auth` (`main.py:107-154`) and any future PATH-based caller resolves to the same binary the SDK transport uses (RQ13).

Env: `PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin`, `PYTHONPATH=/app/src` (NOT NEEDED post-`--no-editable` — `assistant` lives in venv site-packages; kept only if a phase-N feature ships /app/src bind-mount), `HOME=/home/bot`, `XDG_CONFIG_HOME=/home/bot/.config`, `XDG_DATA_HOME=/home/bot/.local/share`, `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` (W2-M1: suppress version-check warnings; safe even if the bundled binary ignores it).

WORKDIR `/app`. USER `bot`. ENTRYPOINT `["/opt/venv/bin/python", "-m", "assistant"]`.

**Test stage (separate target, NOT in main multi-stage chain):** FROM `runtime`. `RUN /opt/venv/bin/uv sync --frozen` (re-add dev deps: pytest, ruff, mypy). CI invokes via `--target test` for unit-test job; never pushed to GHCR.

**Expected size:** ~400 MB uncompressed (base ~80 + gh ~14 + venv ~270 + claude ELF inside venv 236; numbers overlap because the binary is part of `/opt/venv` size); ~140 MB compressed on GHCR. RQ9 measured ~600 MB before stage-2 drop; RQ13 verified ~330 MB savings = ~400 MB final. Pull time on 50 Mbps ≈ 25-30s.

**State paths in container (all bind-mounted):**
| Container | Purpose | Host origin |
|---|---|---|
| `/home/bot/.claude/` | OAuth creds + projects + session state + agents/skills/plugins | host `~/.claude/` (FULL mount per W2-C4 — selective mount breaks claude session/memory features) |
| `/home/bot/.local/share/0xone-assistant/` | vault, DBs, .daemon.pid, .last_clean_exit, audit logs, vault.lock | host `~/.local/share/0xone-assistant/` |
| `/home/bot/.config/0xone-assistant/` | .env + secrets.env (read-only via env_file, no mount) | — |

**Vault flock:** `_memory_core.py:606-639` opens `<data_dir>/.memory.lock` (or similar; coder verifies exact path during code-read) and acquires `fcntl.flock(LOCK_EX | LOCK_NB)` on every `memory_save`. RQ4 spike verified flock propagation across the bind-mount on Linux (POSIX-compliant via VFS layer), so the same kernel mechanism applies to `_memory_core.py` vault flock. RQ7 confirmed VPS data-dir filesystem (ext4); no spike escalation needed UNLESS owner moves the data dir to ZFS/btrfs.

## C. `docker-compose.yml` (path: `deploy/docker/docker-compose.yml`)

```yaml
services:
  0xone-assistant:
    image: ghcr.io/c0manch3/0xone-assistant:${TAG:?set TAG in .env, e.g. TAG=sha-abc1234 or TAG=phase5d}
    container_name: 0xone-assistant
    restart: unless-stopped
    env_file:
      - path: ${HOME}/.config/0xone-assistant/.env
        required: true
      - path: ${HOME}/.config/0xone-assistant/secrets.env
        required: false
    volumes:
      - ${HOME}/.claude:/home/bot/.claude:rw
      - ${HOME}/.local/share/0xone-assistant:/home/bot/.local/share/0xone-assistant:rw
    user: "1000:1000"
    stop_grace_period: 35s
    labels:
      - "autoheal=true"
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
    healthcheck:
      test: ["CMD-SHELL", "test -f /home/bot/.local/share/0xone-assistant/.daemon.pid && pid=$$(cat /home/bot/.local/share/0xone-assistant/.daemon.pid) && [ -n \"$$pid\" ] && [ -L /proc/$$pid/exe ] && readlink /proc/$$pid/exe | grep -q python"]
      interval: 30s
      timeout: 5s
      start_period: 60s
      retries: 3

  autoheal:
    image: willfarrell/autoheal:latest
    container_name: 0xone-autoheal
    restart: unless-stopped
    environment:
      AUTOHEAL_CONTAINER_LABEL: autoheal
      AUTOHEAL_INTERVAL: "30"
      AUTOHEAL_START_PERIOD: "60"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
```

Rationale (post-wave-2):
- **Bind mounts, not named volumes**: standard tools (rsync, sqlite3, tar) inspect state; phase-7 vault git ops need direct file access.
- **`stop_grace_period: 35s`**: matches phase 5b `TimeoutStopSec=30s` + 5s docker margin (preserves `.last_clean_exit` marker write).
- **`restart: unless-stopped`**: semantic parity with phase 5a `Restart=on-failure` + host-reboot recovery. **Does NOT restart on unhealthy**; `autoheal` sidecar covers that gap.
- **`autoheal` sidecar** (W2-H3): off-the-shelf `willfarrell/autoheal:latest`, watches the `autoheal=true` label on `0xone-assistant`, restarts containers reporting `unhealthy`. ~5 MB image, zero ongoing maintenance. Phase 9 may replace with VPS systemd timer for stronger isolation.
- **Healthcheck pid+exe check** (W2-C3): `kill -0 $(cat .pid)` alone false-positives on pid recycle. New form: `[ -f .pid ] && pid=$(cat .pid) && [ -n "$pid" ] && [ -L /proc/$pid/exe ] && readlink /proc/$pid/exe | grep -q python`. The `readlink /proc/$pid/exe` step proves the pid resolves to the python interpreter, eliminating recycle false-positives AND the empty-pid-file race (W2-M5). Symlink dereference in `/proc` is a kernel-managed namespace-correct probe.
- **`start_period: 60s`** (RQ12 + spike-findings Patch 4): claude preflight ping has 45s timeout; 60s gives 33% headroom. Bump to 90s if owner observes flapping in production.
- **`env_file` object form with `required: false` for secrets.env** (W2-C2): compose v2 errors on missing simple-list env_file. Object form lets `secrets.env` be optional on first boot (creates it later when GH_TOKEN is provisioned).
- **`TAG=${TAG:?...}`** (M5): no `:-latest` default; missing TAG fails compose with explicit error message instead of pulling whatever last main-push built.
- **`max-size: 50m, max-file: 5`** (M4): 250 MB rotation ceiling vs original 30 MB. DEBUG-level logs at low rps fit comfortably.
- **`user: 1000:1000`**: matches VPS `0xone` uid (RQ7 verified).
- **No `ports:`**: outbound polling only.
- **No `depends_on`**: autoheal is independent; phase 8 adds scheduler sibling.

Dev compose override (`docker-compose.dev.yml` with source bind-mount + pytest target) → phase 5d backlog, NOT shipped.

## D. Registry strategy (GHCR)

- **Registry:** `ghcr.io/c0manch3/0xone-assistant`. **Visibility: PUBLIC** (no secrets in image; all secrets live in bind-mounts).
- **One-shot UI flip required:** GHCR creates new packages PRIVATE by default. After the first CI push (`gh actions runs view ...`), owner browses to `github.com/users/c0manch3/packages/container/0xone-assistant/settings` and toggles visibility to Public. Until done, VPS anonymous pull returns 401. Documented as migration runbook step 7a.
- **CI publishes** via `GITHUB_TOKEN` auto-provided; workflow grants `packages: write` + `contents: read`.
- **VPS pulls** anonymous after the visibility flip.
- **Dev pushes** from Mac — not needed for phase 5d (CI handles all pushes); if owner needs ad-hoc Mac push, use existing `gh` PAT with `write:packages`.

**Tags:**
- `:latest` — rolling on `main` push. Dev convenience only, never pinned in prod (compose forces `TAG` to be set).
- `:sha-<short>` — every CI build, stable reference for rollback.
- `:phase5d`, `:v0.5.d` — phase ship tags, never auto-pruned.

**Manifests:** `linux/amd64` only. `linux/arm64` deferred to phase 9 (RQ3: no PyStemmer arm64 wheel; RQ5: qemu compile too slow).

**Retention:** CI prunes `sha-*` tags older than 30 days (keep last 10 regardless of age); `phase*`/`v*` never auto-pruned. Implementation deferred to phase 9 — rolling shipped phase 5d ignoring storage growth (phase 5d's first-month delta is small).

## E. Build & deploy workflow

**Dev build (Mac):** Mac dev does NOT need Docker. `uv run python -m assistant` is the dev loop. If owner wants to test the image locally, install Docker Desktop / colima ad-hoc.

**CI:** `.github/workflows/docker.yml`. Triggers: `push` main → `latest + sha-short`; tag push `v*`/`phase*` → `:<ref>` + `latest`; PR → build only, no push. Single platform: `linux/amd64`.

Jobs:
1. `build-and-push` — buildx + GHA cache (`scope=main-amd64` / `scope=pr-amd64` per H5), push on non-PR. Tags via `docker/metadata-action@v5`.
2. `smoke` — image-imports-only smoke (W2-H2 Option B). Does NOT run real preflight (would 401 against Anthropic with fake creds — wave-1 C3). Uses `docker run --rm --entrypoint /opt/venv/bin/python ghcr.io/...:<sha-tag> -c "import assistant; from assistant.tools_sdk import installer; from assistant.scheduler import store; print('imports_ok')"`. Asserts non-zero exit on import failure. Verifies the venv is usable AND the SDK-bundled `claude` binary is in place: `docker run --rm --entrypoint /usr/local/bin/claude ghcr.io/...:<sha-tag> --version` (RQ13 — symlink resolves, ELF runs, prints version). Total job time ~30s.
3. `scan` — Trivy `image-mode`, fail on HIGH/CRITICAL OS pkgs and CRITICAL language pkgs. `.trivyignore` empty in phase 5d. SARIF upload to GitHub security tab is **deferred to phase 9** (avoids `security-events: write` permission surface today).

CI permissions (H4 enumerated): `contents: read`, `packages: write`, `actions: write` (for buildx GHA cache RW). Trivy as a CLI action does not need `security-events: write` because we're not uploading SARIF.

**VPS deploy:**
```bash
ssh ... "cd /opt/0xone-assistant && git pull"
ssh ... "export TAG=sha-<hash> && docker compose pull && docker compose up -d --force-recreate"
ssh ... "docker compose logs -f 0xone-assistant"
# Owner Telegram smoke
```

**Rollback:** `export TAG=sha-<oldhash> && docker compose pull && up -d --force-recreate`. O(seconds) — cached layers. Pre-flight check `docker manifest inspect ghcr.io/c0manch3/0xone-assistant:$TAG` recommended (M8). Absolute fallback: systemd unit (retained disabled in `deploy/systemd/`).

## F. Migration from systemd to Docker (one-time, VPS — wave-2 reordered)

**Step 0. Mac preconds:** image pushed to GHCR (CI green for the target tag, e.g. `phase5d-rc1`); GHCR visibility flipped Public (one-shot UI action — see §D); `git pull` from VPS would bring `deploy/docker/*`.

**Step 1. Verify pull works (BEFORE touching VPS state):**
```bash
ssh 0xone@193.233.87.118 "docker --version && docker compose version" || \
  echo "docker missing — go to step 0a"
ssh 0xone@193.233.87.118 "docker pull ghcr.io/c0manch3/0xone-assistant:phase5d-rc1"
# If pull fails with 401: GHCR not yet flipped to public; do that first.
```

**Step 0a (only if step 1 says docker missing):**
```bash
sudo apt-get install -y docker.io docker-compose-plugin || \
  curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker 0xone
# Re-SSH to pick up the docker group; re-run step 1.
```
Idempotent precondition check (RQ10 patch):
```bash
command -v docker >/dev/null && docker compose version >/dev/null && getent group docker | grep -qw 0xone || \
  { sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; sudo usermod -aG docker 0xone; }
```

**Step 2. Backup state:**
```bash
BACKUP=~/.backup-$(date +%Y%m%d-%H%M)
mkdir -p $BACKUP
cp -a ~/.claude $BACKUP/
cp -a ~/.local/share/0xone-assistant $BACKUP/
cp -a ~/.config/0xone-assistant $BACKUP/
echo "Backup at $BACKUP — total $(du -sh $BACKUP | awk '{print $1}')"
```
Backup BEFORE Docker install would be wave-1 H6 advice; wave-2 H6 reorders backup BEFORE step 0a (apt operations) so a failed apt doesn't strand the host.

**Step 2.5. Fix `~/.claude` subdir ownership (one-off, idempotent — wave-2 M7 safety wrap):**
```bash
test -d ~/.claude && \
  test "$(stat -c %U ~/.claude)" = "$USER" && \
  sudo chown -R 0xone:0xone ~/.claude || true
```
Spike RQ7 found root-owned subdirs `debug/ plans/ session-env/ shell-snapshots/` from prior `sudo claude …` invocations. Container as uid 1000 cannot write them; chown one-off fixes it. Outer-dir ownership check prevents accidental chown on a shared `/etc/skel/.claude` template.

**Step 3. Stop systemd FIRST (BLOCKING):**
```bash
systemctl --user disable --now 0xone-assistant.service
systemctl --user is-active 0xone-assistant.service && exit 1 || true
```
Skipping this causes a container restart hot-loop from singleton-lock `BlockingIOError` (spike RQ12). Unit file retained for fallback.

**Step 4. Git pull on VPS:** `cd /opt/0xone-assistant && git pull` brings new `deploy/docker/*` artifacts.

**Step 5. First boot:**
```bash
cd /opt/0xone-assistant/deploy/docker
echo "TAG=phase5d-rc1" > .env  # or: cp .env.example .env && edit
docker compose pull
docker compose up -d
docker compose ps  # expect 0xone-assistant: Up (healthy after ~60s); 0xone-autoheal: Up
docker compose logs --tail=200
```
If healthcheck stays `starting` past 90s → check `docker compose logs` for `auth_preflight_ok` event; if missing, OAuth bind-mount may be wrong.

**Step 6. Owner smoke (Telegram):** ping / memory-add / memory-find / scheduler-add / marketplace-list. Confirm each turn produces expected reply.

**Step 7. Reboot test:** `sudo reboot`. After 60s: SSH back, `docker compose ps` — expect both services Up (healthy). Verifies `restart: unless-stopped` + Docker daemon auto-start at boot.

**Step 8. Retire systemd unit (owner gate after 24h of green prod):**
```bash
rm ~/.config/systemd/user/0xone-assistant.service
systemctl --user daemon-reload
```
Repo copy in `deploy/systemd/` stays as documented fallback.

**Failure rollback at any step:** `docker compose stop && systemctl --user enable --now 0xone-assistant` restores phase 5a state. Backup at `$BACKUP` from step 2 is the worst-case restore.

## G. OAuth + gh token handling

**OAuth (`~/.claude/`):** bind-mount rw, FULL directory (W2-C4 reversal: selective mount of just `.credentials.json` breaks claude session/memory features). Container's bundled `claude` binary refreshes access_token every ~1h via `os.rename(tmp, target)` — atomic on Linux bind mounts (RQ4 verified). Divergence risk (dev-Mac vs VPS) unchanged from phase 5a.

**Data-sensitivity caveat:** `~/.claude/projects/*.jsonl` contains full conversation transcripts. Phase 5d full-mounts them for functional reasons; mitigations:
1. Single-tenant container, no untrusted code in MCP tool surface.
2. Phase 9 adds `read_only: true` for everything except bind-mounts; `cap_drop: ALL`; `no-new-privileges: true`.
3. Owner SHOULD periodically prune `~/.claude/projects/` (recipe in `deploy/docker/README.md`).

**RULE for owner (W2-H4):** never run `claude` CLI on the VPS as `sudo` — it creates root-owned files inside `~/.claude/` that the container as uid 1000 cannot write. If debugging on VPS, `su - 0xone` first.

**GH_TOKEN:** `env_file: ~/.config/0xone-assistant/secrets.env`. Subprocess inheritance chain: compose → container env → python daemon → `gh api` subprocess. Same pattern as phase 5a systemd EnvironmentFile.

**Rotation recipe (RQ11 verified):** edit `secrets.env`, then `docker compose up -d` (compose detects env_file change via config hash and recreates container). `docker compose restart` does NOT re-read env_file; `docker restart <container>` does NOT either. `docker exec ... kill -HUP 1` does NOT reload env_file (kernel env of pid 1 is frozen at exec time). Document in `deploy/docker/README.md`.

**TELEGRAM_BOT_TOKEN + OWNER_CHAT_ID:** `env_file: ~/.config/0xone-assistant/.env`. Same pattern.

**No secret baked into image.** `.dockerignore` excludes `.env`, `secrets.env`, `.credentials.json`, `**/secrets.env`, `**/.env`.

## H. Testing strategy

**Unit tests — dedicated `test` Dockerfile target** (NOT in main multi-stage chain). CI `--target test` re-syncs dev deps + runs pytest; fails push job if tests fail. Runtime image stays slim.

**Integration smoke in CI (W2-H2 Option B):** `docker run --rm --entrypoint /opt/venv/bin/python <image> -c "import assistant; ..."` asserts module load. Plus: `docker run --rm --entrypoint /usr/local/bin/claude <image> --version` confirms RQ13 symlink works (catches an SDK version that drops the bundled binary). NO real Claude API call — preflight tautology (wave-1 C3) avoided.

**Trivy scan** in CI, fail on HIGH/CRITICAL OS packages + CRITICAL language packages. `.trivyignore` empty in phase 5d. SARIF upload deferred to phase 9.

**Owner smoke runbook** `deploy/docker/README.md`: compose up → ps healthy → logs → Telegram regression.

**Mac compose testing:** owner's machine has no Docker (RQ note). Skip unless owner installs Docker Desktop ad-hoc; production target is VPS Linux.

## I. Owner Q&A

All 12 wave-0 questions decided + 4 new (Q-R1..Q-R4) from researcher fix-pack:

| # | Decision |
|---|----------|
| Q1 Base image | `python:3.12-slim-bookworm` digest-pinned. **DECIDED.** |
| Q2 Claude CLI pin policy | Manual bump per phase — but via SDK `pyproject.toml` pin (`claude-agent-sdk>=0.1.59,<0.2`), NOT npm. **DECIDED — single source of truth.** |
| Q3 Registry | GHCR only. **DECIDED.** |
| Q4 CI trigger | Every `main` push + tag push. PRs build only. **DECIDED.** |
| Q5 Tag pinning | `TAG=sha-<hash>` pinned in VPS `.env`. Compose enforces via `${TAG:?...}`. **DECIDED.** |
| Q6 Systemd unit lifecycle | Retain `deploy/systemd/` as documented fallback (disabled). **DECIDED.** |
| Q7 Dev workflow on Mac | `uv run python -m assistant`. No Docker for Mac dev. **DECIDED.** |
| Q8 Container UID | `user: "1000:1000"`. **DECIDED.** |
| Q9 Dev tooling in runtime | No — separate `test` target. **DECIDED.** |
| Q10 PyStemmer strategy | Wheel-first; resolver picks 3.0.0 on cp312. Optional `pyproject.toml` bump `>=3.0,<4` (cosmetic — 2.2 floor is unreachable on 3.12). **DECIDED.** |
| Q11 Rootful vs rootless Docker | Rootful for phase 5d. **DECIDED.** |
| Q12 Image size red line | 1 GB / 400 MB compressed. Actual: ~400 MB / ~140 MB compressed (post-RQ13). **DECIDED — comfortably under cap.** |
| Q-R1 GHCR public-vs-private | **PUBLIC** (no secrets in image). One-shot UI flip after first push. **DECIDED.** |
| Q-R2 arm64 in phase 5d | **DROP** — defer to phase 9. **DECIDED.** |
| Q-R3 Restart-on-unhealthy | **`autoheal` sidecar** (`willfarrell/autoheal`). **DECIDED.** |
| Q-R4 Healthcheck robustness | **pid+exe `/proc/<pid>/exe` check**. **DECIDED.** |

## J. Known risks (post-wave-2)

1. **fcntl.flock on bind-mount.** Low on Linux/ext4 (RQ4 + RQ7 verified). Medium on hypothetical ZFS/btrfs migration — re-spike if owner moves data dir.
2. **Vault flock (`_memory_core.py:606`).** Same VFS layer as pid-file flock; covered by RQ4. Low risk.
3. **OAuth `.credentials.json` atomic refresh.** Linux atomic via `os.rename` (RQ4 verified). Mac Docker Desktop may differ — Mac is dev-only, no Docker.
4. **Image bloat creep.** Today ~400 MB; phase 6 media deps may add ffmpeg etc. Re-measure each phase.
5. **PyStemmer wheel only on amd64.** arm64 path requires source compile; phase 5d drops arm64 — risk closed.
6. **`docker compose` v2 mandatory.** RQ10 verified VPS has 5.1.1.
7. **Dual daemon race** (container + stale systemd). Singleton flock catches second-to-start (RQ12 verified). Migration §F step 3 disables systemd FIRST to avoid restart hot-loop.
8. **CI `GITHUB_TOKEN` permissions.** Workflow declares `packages: write` + `actions: write` explicitly.
9. **`latest` tag race.** Compose `${TAG:?...}` errors loudly if TAG unset. M5 closed.
10. **Log rotation.** `max-size: 50m × max-file: 5` = 250 MB ceiling per container.
11. **Healthcheck false-positive from pid recycle.** **MITIGATED** by `/proc/<pid>/exe` readlink check (W2-C3).
12. **Restart-on-unhealthy.** **MITIGATED** by `autoheal` sidecar (W2-H3).
13. **Compose `env_file` simple-list errors on missing file.** **MITIGATED** by object form `required: false` on `secrets.env` (W2-C2).
14. **claude binary auto-update reaching out to Anthropic.** `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` env var set in Dockerfile (W2-M1; safe even if SDK-bundled binary ignores it).
15. **GHCR private-by-default after first push.** **MITIGATED** by §F step 0 visibility-flip note.
16. **Bundled-binary version drift if a future SDK 0.1.x patch removes `_bundled/claude`.** Symlink `RUN ln -s …` would fail at build time → loud failure, intentional (RQ13 caveat).
17. **`docker compose logs` JSON double-encoding** (W2-M4) — owner unwraps via `docker compose logs --no-log-prefix` or `jq -r .log` if reading raw container json file. Documented in README.
18. **Container PID namespace confuses host-side `kill`** (W2-H8) — `.daemon.pid` is container-namespaced; owner uses `docker compose logs/ps/top` instead of host `kill`. Documented in README.
19. **Docker daemon shutdown-timeout vs compose stop_grace_period** (L1) — host reboot uses Docker daemon's `shutdown-timeout` (default 10s), NOT compose's 35s. Set `/etc/docker/daemon.json` `shutdown-timeout: 40` in install runbook.
20. **`~/.claude` data sensitivity** (W2-C4) — full mount of conversation transcripts inside container. Mitigated by single-tenant model + phase 9 hardening + transcript pruning recipe in README.

## K. Critical files to create

- `deploy/docker/Dockerfile` — multi-stage image (4 main stages + 1 test target). Stage 2 nodejs DROPPED.
- `deploy/docker/docker-compose.yml` — prod compose: 0xone-assistant + autoheal services. env_file object form, healthcheck pid+exe, volumes, logging, labels.
- `deploy/docker/.dockerignore` — comprehensive enumerated list (W2-H7):
  ```
  .venv
  .git
  __pycache__
  *.pyc
  .pytest_cache
  .mypy_cache
  .ruff_cache
  .idea
  .vscode
  .DS_Store
  *.db
  *.db-wal
  *.db-shm
  *.log
  scheduler-audit.log
  memory-audit.log
  node_modules
  dist/
  build/
  *.egg-info
  plan/
  tests/
  .claude/
  .local/
  .config/
  .last_clean_exit
  .daemon.pid
  .env
  .env.*
  secrets.env
  **/secrets.env
  **/.env
  docs/
  scratch/
  .coverage
  htmlcov/
  ```
- `deploy/docker/README.md` — install/update/rollback/backup recipes, OAuth + GH_TOKEN transfer, healthcheck explainer, log-tail recipes, transcript-pruning recipe, `.daemon.pid` namespace explainer, never-run-sudo-claude rule.
- `.github/workflows/docker.yml` — CI build/push/scan/smoke (amd64-only).
- CLAUDE.md patch — minimal: "Deploy method: Docker compose; see `deploy/docker/README.md`" + "Fallback: `deploy/systemd/`". Don't inline build commands (W2-H10 — keeps future phases free to amend).
- `plan/phase4/runbook.md` + `plan/phase5/runbook.md` — Docker log-tail recipes (`docker compose logs -f` instead of `journalctl`); s/docker-compose/docker compose/ pass (M2).
- Optional `deploy/docker/docker-compose.dev.yml` (Mac dev override) — **DEFERRED**.

**No source-code changes.** `config.py` already honors `XDG_DATA_HOME`/`XDG_CONFIG_HOME` via `platformdirs`. `_preflight_claude_auth` continues to call `"claude"` argv — runtime symlink at `/usr/local/bin/claude` (RQ13) makes it work without source change.

## L. Spikes status (post-wave-2)

| RQ | Status | Notes |
|---|---|---|
| RQ1 gh install | **PASS** | gh 2.91.0 on slim-bookworm; +14.4 MB layer. |
| RQ2 Node 20 + claude CLI npm | **OBSOLETED by RQ13** | Stage 2 dropped. |
| RQ3 PyStemmer wheel | **PASS amd64** | 3.0.0 cp312 manylinux2014_x86_64 wheel exists. arm64 dropped. |
| RQ4 flock + atomic rename | **PASS** | Both pid-file and vault-flock paths covered (same VFS layer). Confirmed via spike. |
| RQ5 buildx multi-arch | **DROPPED** | amd64-only phase 5d. |
| RQ6 GHCR via GHA | **DOCUMENTED** | Draft workflow yaml; first CI run self-validates. |
| RQ7 bind-mount uid/gid | **PASS** | uid/gid 1000:1000 across all paths. `~/.claude` chown caveat covered by §F step 2.5. ext4 confirmed. |
| RQ8 uv sync editable | **PASS** | `--no-editable` produces real `site-packages/assistant/` dir. |
| RQ9 image size | **PASS** | ~600 MB pre-RQ13; ~400 MB post-RQ13 stage drop. |
| RQ10 compose v2 | **PASS** | VPS docker-ce 29.3.0 + compose 5.1.1. |
| RQ11 env_file rotation | **PASS** | `compose up -d` recreates container; documented. |
| RQ12 dual-daemon race | **PASS** | Singleton flock catches; §F step 3 enforces systemd-stop precondition. |
| RQ13 SDK-bundled claude binary | **PASS — DROP STAGE 2** | Linux x86_64 wheel ships 236 MB ELF; SDK transport prefers it; symlink `/usr/local/bin/claude → venv path` in runtime stage covers preflight. ~330 MB image savings. |

Coder unblocked.

## M. Phase 6+ prerequisites

- **Phase 6 (media):** new OS deps (ffmpeg / libsndfile / CUDA) extend Dockerfile or new stage. If media stays on Mac: sidecar out of scope, bot calls via HTTPS.
- **Phase 7 (vault git commit):** `git` already in image; add SSH deploy key bind-mount (`~/.ssh/vault_key:ro` + `GIT_SSH_COMMAND` env). Compose file extensible.
- **Phase 8 (out-of-process scheduler):** add sibling service `scheduler-worker` with shared `assistant.db` bind-mount + UDS bind-mount for IPC.
- **Phase 9 (hardening):** `read_only: true`, tmpfs /tmp, cap_drop ALL, no-new-privileges, SBOM, Cosign, Trivy SARIF upload, rootless Docker, arm64 reopen, GHCR retention policy. Phase 5d stays compatible.

---

**LOC estimate:** ~120 Dockerfile + ~50 compose + ~30 .dockerignore + ~80 CI workflow + ~250 README + ~30 CLAUDE.md/runbook patches ≈ ~560 LoC across artifacts. Owner-facing cutover ≈ 30-45 min.

**Risk level:** Low-Medium. No `src/` changes (RQ13 symlink keeps preflight working without source edit). fcntl.flock + OAuth refresh + singleton-lock all verified via spikes. Healthcheck robustness fixed (W2-C3). Restart-on-unhealthy covered by autoheal sidecar.
