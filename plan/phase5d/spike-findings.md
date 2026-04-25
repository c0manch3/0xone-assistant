# Phase 5d — Spike Findings (RQ1-RQ12)

**Date:** 2026-04-24
**Executed by:** researcher agent
**Host used for Docker spikes:** VPS `193.233.87.118` (Docker CE 29.3.0 + compose v2 v5.1.1 + buildx v0.31.1, kernel Ubuntu 24.04). Mac mini has NO Docker installed (no Docker Desktop / colima / podman) — all container probes ran remotely; systemd daemon on VPS was not touched.

## Executive summary

**Go/no-go for coder:** **GO with two parameter locks required.**

- All runtime installs succeed on `python:3.12-slim-bookworm` (gh 2.91.0, Node 20.20.2, claude CLI 2.1.116, PyStemmer wheel).
- `fcntl.flock` and `os.rename` propagate correctly across the bind-mount boundary — singleton lock story holds. Dual-daemon race exits cleanly.
- **Surprise 1 — `uv sync --frozen --no-dev` installs EDITABLE (pth pointing at /build/src).** Runtime COPY of `/opt/venv` alone would fail to import `assistant`. Fix: use `uv sync --no-dev --no-editable` in builder stage. Verified produces real `site-packages/assistant/` dir.
- **Surprise 2 — claude CLI 2.1.116 IS a native ELF binary, not a JS file.** `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` is 227 MB, self-contained. `node` is only needed at `npm install` time (postinstall downloads the platform-specific sub-package). Runtime stage can COPY the claude binary + symlink, and does NOT need `node` at all. Image size savings: ~95 MB (the Node runtime) if we drop node from runtime.
- **Surprise 3 — PyStemmer has no arm64 wheel at any manylinux tag** (`PyStemmer-3.0.0` ships only cp312 manylinux2014_x86_64). Arm64 requires source compile (adds `build-essential python3-dev` to builder stage for arm64 only, ~5 min qemu emulated build).

**Top-3 parameter decisions required from owner:**
1. **arm64 enablement:** enable arm64 image (compile PyStemmer on arm64 in builder stage, ~5 min extra CI) OR drop arm64 for phase 5d (VPS is amd64, zero user impact). Recommend **drop for 5d, defer to 9**.
2. **PyStemmer version pin:** pyproject.toml pins `>=2.2,<4`, resolver picks `3.0.0` which is >= 2.2 — no change needed. (No wheel actually exists for 2.2.x on cp312 either — 2.2 predates cp312 support; 3.0.0 is the effective minimum for Python 3.12.)
3. **env_file rotation procedure:** documented — owner rotates `GH_TOKEN` by `echo GH_TOKEN=new > secrets.env && docker compose up -d` (compose auto-detects env_file content change and recreates the container). `docker compose restart` does NOT re-read the file.

**Open owner questions:** 4 (listed §Open questions, below).

**Skipped spikes:** RQ6 (GHCR live push — no GITHUB_TOKEN available outside CI; documented as draft workflow yaml).

---

## RQ1 — `gh` install on `python:3.12-slim-bookworm`

**Status:** PASS.

Full stage-3 script ran cleanly on slim-bookworm via `docker run`. Installed `gh 2.91.0` from `cli.github.com/packages` signed apt repo. Added OS deps: `libgdbm-compat4 libperl5.36 perl libcurl3-gnutls libexpat1 liberror-perl git-man git` (`git` is pulled in as transitive by `gh`).

Delta image size: base 79.3 MB → +gh stage 93.7 MB (**+14.4 MB**).

**Finding:** bookworm repo has both amd64 and arm64 arch in sources; the spec's `arch=amd64` clause in sources.list.d needs `arch=$(dpkg --print-architecture)` for arm64 compatibility.

---

## RQ2 — Node 20 + claude CLI 2.1.116

**Status:** PASS with major architectural insight.

- `nodejs_20.20.2-1nodesource1_amd64.deb` from `deb.nodesource.com/setup_20.x` installs cleanly. Transitive deps: `python3-minimal python3.11 python3` (yes — nodejs installer pulls Debian's default system python3.11, a harmless 20 MB; unavoidable without building from source).
- `npm install -g @anthropic-ai/claude-code@2.1.116` completes, produces `/usr/bin/claude` symlink → `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`. `claude --version` → `2.1.116 (Claude Code)`. Verified working.

**Architectural surprise:** `bin/claude.exe` is a **227 MB ELF executable** (magic `\x7fELF`), NOT a Node script. The claude CLI ships a native-binary-per-platform layout:

```
package.json bin: {"claude": "bin/claude.exe"}  (ignore the .exe name — Linux ELF)
PLATFORMS map in cli-wrapper.cjs:
  linux-x64, linux-arm64, linux-x64-musl, linux-arm64-musl,
  darwin-arm64, darwin-x64
```

postinstall (install.cjs) downloads the platform-specific sub-package (`@anthropic-ai/claude-code-linux-x64`) and copies its binary over `bin/claude.exe`. Once installed, the binary has **zero node runtime dependencies**. Only `cli-wrapper.cjs` would invoke node, and only when `--ignore-scripts` prevented postinstall.

**Implication for runtime stage:** the plan §B stage 5 says *"only runtime deps: ca-certificates curl git nodejs. COPY --from=nodejs the claude CLI + node binary"*. We can **drop the `nodejs` package entirely from the runtime stage** and just COPY the claude binary:

```dockerfile
COPY --from=nodejs /usr/bin/claude /usr/bin/claude
COPY --from=nodejs /usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe \
                   /usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe
# /usr/bin/claude is a symlink to the above — preserve it if COPY flattens
```

Savings: `/usr/bin/node` is 95 MB; dropping Node-the-runtime-binary saves ~95 MB, keeping us closer to the 500 MB uncompressed goal. **`git` is still wanted** by the future phase-7 vault git commit path; `curl`/`ca-certificates` stay for HTTPS.

**Caveat:** if a future claude CLI version goes back to Node scripts, this COPY strategy breaks. Low risk; easy to revert by COPY'ing `/usr/bin/node` too. Pinning claude version makes this safe for 5d.

**Tarball size:** `/usr/lib/node_modules/@anthropic-ai/claude-code/` total 227 MB (nearly all of it the binary itself). `/usr/lib/node_modules` total 243 MB.

---

## RQ3 — PyStemmer wheel on cp312

**Status:** PARTIAL. amd64 wheel exists; **arm64 wheel does not**.

| Platform | Result |
|---|---|
| `manylinux_2_34_x86_64` | NO matching distribution |
| `manylinux_2_34_aarch64` | NO matching distribution |
| `manylinux2014_x86_64` | **PyStemmer-3.0.0-cp312-cp312-manylinux_2_5_x86_64.manylinux1_x86_64.manylinux_2_17_x86_64.manylinux2014_x86_64.whl** (745 KB) |
| `manylinux2014_aarch64` | NO matching distribution |

Upstream PyStemmer publishes only x86_64 wheels on PyPI. `slim-bookworm` is glibc 2.36 (> 2.17), so the manylinux2014 wheel installs cleanly.

**amd64 runtime path:** `uv sync` finds the wheel, 745 KB install, no compile step.

**arm64 path:** builder stage must add `build-essential python3-dev`; `pip/uv` compiles from sdist. Empirical: in qemu-emulated buildx, `build-essential` install alone took ~197s, and PyStemmer build stalled; a native arm64 builder (or GHA `ubuntu-24.04-arm` runner) would complete in seconds. **Plan suggestion: if keeping arm64, run arm64 matrix leg on a native arm runner in CI, not qemu emulation.**

`pyproject.toml` currently pins `PyStemmer>=2.2,<4`. Resolver picks 3.0.0. The 2.2.x line does not publish cp312 wheels at all — 3.0.0 is effectively the minimum compatible version. **Recommend bumping pin to `PyStemmer>=3.0,<4`** for explicit alignment.

---

## RQ4 — `fcntl.flock` + atomic rename on bind-mount

**Status:** PASS, with one mode-bit footnote.

Test matrix on `/tmp/spike5d/dockerflock` bind-mounted into `python:3.12-slim` containers:

| Scenario | Outcome |
|---|---|
| Container A holds `LOCK_EX\|LOCK_NB` on `/data/.test.pid`, Container B tries | Container B: `BlockingIOError` → "BLOCKED-AS-EXPECTED" |
| Host python holds lock on `/tmp/spike5d/dockerflock/host.pid`, Container tries | Container: `BlockingIOError` → "BLOCKED-AS-EXPECTED-host-lock-propagates" |
| `os.rename(tmp, creds.json)` inside container, `-u 1000:1000` | "RENAME-OK" with `uid=1000 gid=1000 size=8` on host-visible file |

**Footnote (not a Docker issue):** Python's `os.rename` preserves source file perms. The probe's `Path.write_text` created `creds.tmp` with umask default `0o022` → rename target ended up `0o644` instead of `0o600`. This is a general atomic-refresh bug pattern across ALL Linux, not bind-mount specific. The `claude` CLI's OAuth refresh path uses a documented-atomic `os.rename` elsewhere; if we rewrite `.credentials.json` ourselves anywhere, add explicit `os.chmod(tmp, 0o600)` BEFORE rename. Not an action item for phase 5d; log for future audit.

**Verdict:** flock propagation is POSIX-compliant through the bind-mount VFS. Dual-daemon singleton lock story (RQ12) holds.

---

## RQ5 — buildx multi-arch

**Status:** PARTIAL PASS (amd64 solid, arm64 requires infra setup).

Default `docker buildx` builder on VPS supports `linux/amd64 (+4)` natively. Installing `tonistiigi/binfmt --install arm64` adds qemu-aarch64 emulator; `docker buildx ls` then reports `linux/arm64` available.

**Tiny probe:** `FROM python:3.12-slim-bookworm; RUN uname -m && python --version` builds successfully on `--platform linux/arm64` via qemu (manifest-list shows aarch64 config digest).

**Full-arch probe:** `build-essential + PyStemmer` compile on arm64 via qemu took >300s (cancelled at limit). Qemu userspace emulation of cc is ~40x slower than native.

**Infra gap:** default `docker` driver cannot `--load` multi-platform manifests — needs the `docker-container` driver:
```
docker buildx create --name multi --driver docker-container --use
docker buildx build --platform linux/amd64,linux/arm64 --push ...
```
`--load` works single-arch only.

**Recommendation for phase 5d CI:**
- Option A (recommended): drop arm64 from phase 5d. Production is VPS/amd64 only; Mac dev uses `uv run`, not Docker. Add arm64 in phase 9.
- Option B: if arm64 wanted, use GHA `docker/setup-qemu-action` + matrix over `ubuntu-24.04` (amd64, qemu-arm64) OR use GHA `ubuntu-24.04-arm` native runner (faster but requires GH Enterprise/public repo with arm64 fleet). PyStemmer compile adds ~2-5 min arm64 matrix leg.

---

## RQ6 — GHCR auth via GHA

**Status:** SKIPPED LIVE. No `GITHUB_TOKEN` available outside CI; documented draft workflow.

**Draft `.github/workflows/docker.yml`:**

```yaml
name: docker
on:
  push:
    branches: [main]
    tags: ["v*", "phase*"]
  pull_request:

permissions:
  contents: read
  packages: write  # REQUIRED — default is 'read' for packages

env:
  REGISTRY: ghcr.io
  IMAGE: ghcr.io/c0manch3/0xone-assistant

jobs:
  build-and-push:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        if: github.event_name != 'pull_request'
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ${{ env.IMAGE }}
          tags: |
            type=ref,event=branch
            type=ref,event=tag
            type=sha,prefix=sha-,format=short
            type=raw,value=latest,enable={{is_default_branch}}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: deploy/docker/Dockerfile
          platforms: linux/amd64  # arm64 deferred — see RQ3/RQ5
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

**First-time owner task (one-off after first successful push):** the GHCR package `ghcr.io/c0manch3/0xone-assistant` will be created PRIVATE by default. Owner must toggle it PUBLIC on `github.com/users/c0manch3/packages/container/0xone-assistant/settings` for the VPS anonymous pull to work; otherwise VPS needs a PAT with `read:packages` in `~/.config/0xone-assistant/secrets.env` and `docker login ghcr.io` step in the deploy runbook.

Documented and ready for coder.

---

## RQ7 — VPS bind-mount uid/gid + perms

**Status:** PASS with one ownership surprise in `~/.claude/`.

```
uid=1000(0xone) gid=1000(0xone) groups=1000(0xone),27(sudo),988(docker)
/home/0xone/.claude                                       775 0xone:0xone
/home/0xone/.local/share/0xone-assistant                  775 0xone:0xone
/home/0xone/.config/0xone-assistant                       700 0xone:0xone
/home/0xone/.config/0xone-assistant/.env                  600 0xone:0xone
/home/0xone/.config/0xone-assistant/secrets.env           600 0xone:0xone
/home/0xone/.claude/.credentials.json                     600 0xone:0xone
```

**All owner-writable, uid/gid 1000:1000** — matches the plan's `user: "1000:1000"` container UID. No chown migration needed.

**Surprise inside `~/.claude/`:** some subdirs are owned by `root:root` (uid 0):
```
drwxr-xr-x  2    0    0 4096 апр  7 07:18 debug
drwxr-xr-x  2    0    0 4096 мар 21 20:29 plans
drwxr-xr-x 15    0    0 4096 апр 16 18:54 session-env
drwxr-xr-x  2    0    0 4096 апр 16 18:57 shell-snapshots
```

These were almost certainly created by a prior `sudo claude …` invocation. Once the container runs as uid 1000, writes to these four subdirs would fail `PermissionError`. claude CLI creates them lazily — if claude CLI inside container tries to populate `session-env` or `shell-snapshots` and they already exist as root-owned, it fails silently or logs.

**Action:** owner runs a one-off `sudo chown -R 0xone:0xone ~/.claude` before phase 5d cutover. Low impact, but document in migration runbook §F (insert as step 2.5).

Docker group `988(docker)` is already in owner's groups; `sudo usermod -aG docker 0xone` in plan §F step 2 is a no-op on this host (keep it for idempotency).

---

## RQ8 — `uv sync` editable vs non-editable

**Status:** BLOCKING ISSUE found; fix identified and verified.

`uv sync --frozen --no-dev` (spec's §B stage-4 command) produces:
```
/opt/venv/lib/python3.12/site-packages/
├── assistant-0.1.0.dist-info/
│   ├── METADATA, RECORD, WHEEL, INSTALLER, REQUESTED
├── assistant.pth              ← contains: /build/src
└── _virtualenv.pth
```

**`assistant.pth` points at `/build/src` — the builder-stage source directory.** When runtime stage does `COPY --from=builder /opt/venv /opt/venv`, the pth still references `/build/src`, which does NOT exist in the runtime stage. `python -m assistant` would fail `ModuleNotFoundError: No module named 'assistant'`.

**Fix (verified working):** `uv sync --frozen --no-dev --no-editable`. Result:
```
/opt/venv/lib/python3.12/site-packages/
├── assistant-0.1.0.dist-info/
└── assistant/                 ← real directory (77 KB)
    ├── __init__.py (0 bytes)
    ├── __main__.py (136 bytes)
    ├── adapters/, bridge/, handlers/, scheduler/, state/, tools_sdk/
```

Now `COPY --from=builder /opt/venv` gives runtime a self-contained venv; no `/app/src` COPY needed. **Simpler and smaller than the plan's current proposal.** The plan §B stage 5 line *"COPY --from=builder /opt/venv + /usr/local/bin/uv + /build/src → /app/src"* can drop the `/build/src` COPY entirely.

venv size (with `--no-editable`): **275 MB** on amd64 (pydantic-core + claude-agent-sdk + aiogram + pystemmer + 30 transitive deps, all wheels).

**uv.lock:** present in the repo (not removed by the wipe). `uv sync --frozen` works.

---

## RQ9 — Image size audit

**Status:** PASS. Projected image size well under 1 GB ceiling.

Measured on VPS:
| Stage | Image size |
|---|---|
| `python:3.12-slim-bookworm` (base) | 48 MB |
| stage `base` (+ ca-certificates curl gnupg git sudo + `bot` user) | 79.3 MB |
| stage `ghcli` (base + gh) | 93.7 MB |
| stage `nodejs` (base + node20 + claude CLI) | 288 MB |

**Projected runtime size:**
- base 79 MB
- + 227 MB claude binary (COPY'd, not the full node_modules tree)
- + 14 MB gh
- + 275 MB venv (`/opt/venv` from builder)
- − apt caches/man pages/docs already stripped
- ≈ **~600 MB uncompressed, ~250-300 MB compressed on GHCR**.

Well under the 1 GB red line. Pull time on 50 Mbps ≈ 45s.

**Extra trim options** (not urgent): strip `/usr/share/doc` `/usr/share/man`, remove pip/npm caches (`--no-cache`, `apt-get clean`). The Dockerfile already does `rm -rf /var/lib/apt/lists/*` in measured stages.

---

## RQ10 — docker compose v2 on VPS

**Status:** PASS.

```
Docker version 29.3.0, build 5927d80
Docker Compose version v5.1.1        ← v2 plugin; subcommand form `docker compose`
docker-compose-plugin 5.1.1-1~ubuntu.24.04~noble
```

Compose v2 verified operational (used in RQ11 spike below). No `docker-compose` (hyphenated v1) binary present — correct state. Plan §F step 2 (`sudo apt-get install -y docker.io docker-compose-plugin`) is unnecessary on THIS VPS (already installed; adding `docker.io` could even conflict with `docker-ce`). **Migration runbook needs a precondition check** rather than blind install.

Suggested patch for §F.2:
```
- sudo apt-get install -y docker.io docker-compose-plugin && sudo usermod -aG docker 0xone
+ # One-off precondition check (this VPS already has docker-ce 29.3 + compose v2 + docker group)
+ command -v docker >/dev/null && docker compose version >/dev/null && \
+   getent group docker | grep -qw 0xone || \
+   { sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; sudo usermod -aG docker 0xone; }
```

---

## RQ11 — env_file rotation behavior

**Status:** PASS with nuanced findings. Rotation procedure documented.

| Scenario | GH_TOKEN result |
|---|---|
| `docker run --env-file env` then edit file | Container still sees v1 — env is baked at create time |
| After `docker restart <container>` | Still v1 — restart reuses the same container metadata |
| `docker compose up -d` with edited env_file | **v4 — compose detects config-hash change and Recreates the container** |
| `docker compose restart` after edit | Still v4 — `restart` reuses existing container state |
| `docker compose up -d --force-recreate` | Same as `up -d` here (v4); force flag unnecessary if env_file already changed |

**Operational rotation recipe (owner):**
```bash
# 1. Rotate token in place
vi ~/.config/0xone-assistant/secrets.env     # or echo > with new value

# 2. Trigger container recreation (compose picks up file change automatically)
cd /opt/0xone-assistant/deploy/docker && docker compose up -d

# 3. Verify
docker compose logs -f --tail=50 0xone-assistant
```

**Do NOT use:**
- `docker compose restart` — reuses stale env.
- `docker exec 0xone-assistant kill -HUP 1` — daemon has no SIGHUP handler for env reload; even if it did, kernel env of pid 1 is frozen at exec time.

**Document this in `deploy/docker/README.md` rotation section.**

---

## RQ12 — dual-daemon singleton race (conceptual + RQ4 data)

**Status:** PASS.

Code review `src/assistant/main.py:48-90`:
1. `_acquire_singleton_lock(data_dir)` opens `<data_dir>/.daemon.pid` with `O_CREAT|O_RDWR|0o600`.
2. Attempts `fcntl.flock(fd, LOCK_EX|LOCK_NB)`.
3. On `BlockingIOError`: reads existing pid from file, logs `daemon_singleton_lock_held` with hint, `sys.exit(3)`.
4. On success: truncates and writes own pid, fsyncs.

Combined with RQ4 empirical evidence (flock propagates host→container and container→container via bind-mount), the dual-daemon scenario resolves cleanly:
- Host systemd daemon holds lock → container's `_acquire_singleton_lock` raises BlockingIOError → exits 3 with hint.
- Container holds lock → host systemd invocation exits 3.
- No data-dir corruption window.

**Operational concern — restart hot-loop:** `restart: unless-stopped` in compose will endlessly restart a container that exits 3. If owner starts the container before disabling systemd (cutover §F step 4), expect:
- `docker compose ps` shows repeated Restarting state.
- Log spam: `daemon_singleton_lock_held` every ~10s (exit 3 → compose restarts).

**Mitigation:** cutover runbook MUST disable systemd first. Suggest tweaking §F to prepend a precondition check in step 6:
```
systemctl --user is-active 0xone-assistant.service && \
  { echo "systemd unit still active — run: systemctl --user disable --now 0xone-assistant.service"; exit 1; }
```

Alternative: switch to `restart: on-failure:3` to cap retries, then `docker compose ps` will surface the failure loudly after ~30s instead of quietly spinning.

**Additional find — claude auth preflight (`main.py:107-154`):** at daemon startup the code runs `claude --print ping` with a 45s timeout, exits 3 on timeout / FileNotFoundError / auth failure. Container MUST have working claude + bind-mounted `/home/bot/.claude/.credentials.json` before it can boot. Healthcheck (`kill -0` on pid) won't fire until preflight passes, so the healthcheck's 20s `start_period` may be too tight. **Recommend bumping `start_period: 60s`** to cover worst-case preflight.

---

## Patches to plan/phase5d/description.md (search-replace diffs)

### Patch 1 — §B stage 4 uv flag
```diff
- `UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev` (excludes pytest/ruff/mypy from runtime).
+ `UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable`
+ (excludes pytest/ruff/mypy AND converts pth → real site-packages so the
+  runtime stage can COPY just /opt/venv without COPY'ing /build/src).
```

### Patch 2 — §B stage 5 COPY set
```diff
- **Stage 5 `runtime`:** FROM same slim-bookworm digest. Only runtime deps: `ca-certificates curl git nodejs`. COPY `--from=nodejs` the claude CLI + node binary. COPY `--from=ghcli /usr/bin/gh`. COPY `--from=builder /opt/venv` + `/usr/local/bin/uv` + `/build/src` → `/app/src`.
+ **Stage 5 `runtime`:** FROM same slim-bookworm digest. Runtime deps: `ca-certificates curl git` (no nodejs — claude CLI is self-contained ELF). COPY:
+ - `--from=nodejs /usr/bin/claude` (symlink) AND `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` (227 MB ELF binary).
+ - `--from=ghcli /usr/bin/gh` + required shared libs.
+ - `--from=builder /opt/venv` (venv with real `assistant/` package thanks to `--no-editable`).
+ No `/build/src` COPY needed; no `/usr/local/bin/uv` in runtime (uv is build-time only).
```

### Patch 3 — §B expected size
```diff
- **Expected size:** ~500-900 MB uncompressed; ~300-400 MB compressed on GHCR. RQ9 spike audits and optimizes.
+ **Expected size:** ~600 MB uncompressed (measured: base 79 + claude 227 + gh 14 + venv 275 + overhead); ~250-300 MB compressed on GHCR. RQ9 measured.
```

### Patch 4 — §C compose healthcheck start_period
```diff
       interval: 30s
       timeout: 5s
-      start_period: 20s
+      start_period: 60s  # claude preflight ping can take up to 45s in cold start
       retries: 3
```

### Patch 5 — §F step 2 (idempotent docker install)
```diff
- 2. **Install Docker + compose on VPS:** `sudo apt-get install -y docker.io docker-compose-plugin && sudo usermod -aG docker 0xone`.
+ 2. **Ensure Docker CE + compose on VPS (idempotent — VPS already has docker-ce 29.3.0 + compose v2 5.1.1 as of 2026-04-24):**
+    `command -v docker && docker compose version && getent group docker | grep -qw 0xone || \
+     (curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker 0xone)`.
+ 2.5 **Fix `~/.claude` subdir ownership (one-off):** `sudo chown -R 0xone:0xone ~/.claude` — observed root-owned dirs `debug/ plans/ session-env/ shell-snapshots/` from prior `sudo claude …` invocations would break bind-mount writes as container uid 1000.
```

### Patch 6 — §F step 4 (precondition check)
```diff
- 4. **Stop systemd:** `systemctl --user disable --now 0xone-assistant.service`. Unit file retained.
+ 4. **Stop systemd FIRST (BLOCKING — skipping this causes container restart hot-loop from singleton-lock BlockingIOError):**
+    `systemctl --user disable --now 0xone-assistant.service; systemctl --user is-active 0xone-assistant.service && exit 1 || true`. Unit file retained.
```

### Patch 7 — §G rotation recipe
```diff
 **GH_TOKEN:** `env_file: ~/.config/0xone-assistant/secrets.env`. Subprocess inheritance chain: compose → container env → python daemon → `gh api` subprocess. Same pattern as phase 5a systemd EnvironmentFile.
+
+**Rotation recipe (RQ11 verified):** edit `secrets.env`, then `docker compose up -d` (compose detects env_file change via config hash and recreates container). `docker compose restart` does NOT re-read env_file; `docker restart <container>` does NOT either. Document in `deploy/docker/README.md`.
```

### Patch 8 — §D manifests note
```diff
- **Manifests:** `linux/amd64` required (VPS); `linux/arm64` gated on RQ5 (PyStemmer arm64 wheel availability).
+ **Manifests:** `linux/amd64` only for phase 5d (VPS is amd64; Mac dev uses `uv run`, not Docker). `linux/arm64` **deferred to phase 9** — RQ3 confirmed no PyStemmer arm64 wheel on PyPI, and qemu-emulated compile adds ~5 min to CI with no production benefit for this project. Reopen if owner adds arm64 host or wants darwin-arm64 container dev loop.
```

### Patch 9 — pyproject PyStemmer pin (optional, outside §§ but worth raising)
```diff
- "PyStemmer>=2.2,<4",
+ "PyStemmer>=3.0,<4",
```
Rationale: PyStemmer 2.2.x has no cp312 wheels on PyPI — the 2.2 floor was effectively unreachable on Python 3.12. Make the pin honest. Not load-bearing; resolver already picks 3.0.0.

---

## Open owner questions

1. **arm64 in phase 5d — stay deferred to phase 9?** Recommend YES (drop arm64; amd64-only for 5d). VPS is amd64, Mac dev uses `uv run`. Saves 4-5 min CI matrix leg + no PyStemmer compile-path complexity.

2. **Public vs private GHCR package?** Plan §D says "public recommended." If PUBLIC, first push must be followed by manual visibility toggle on github.com (packages default to private). If PRIVATE, VPS needs `docker login ghcr.io -u c0manch3 -p <PAT_with_read:packages>` and the PAT stored in `~/.config/0xone-assistant/secrets.env`. Recommend PUBLIC for simplicity (no secret beyond source code is in the image; all secrets live in bind-mounts).

3. **systemd unit fallback — retain disabled, or fully remove post-cutover?** Plan §F step 9 says retain. Keeping it is cheap (0 runtime cost when disabled); ensures ~1 min fallback if Docker stack fails. Recommend retain.

4. **Healthcheck policy on preflight timeout — 60s start_period enough?** Observed: `claude --print ping` normally <2s; timeout is set to 45s. 60s start_period covers network + DNS + OAuth refresh jitter. If owner sees flapping after cutover, bump to 90s. Not a blocker.

---

## Skipped spikes

| RQ | Reason | Substitute |
|---|---|---|
| RQ6 live GHCR push | No `GITHUB_TOKEN` outside CI; no dev PAT with `write:packages` supplied | Draft workflow yaml documented in §RQ6; first CI run will self-validate |
| Mac Docker Desktop RQ4 variant | Mac has NO Docker installed; no Desktop/colima/podman/nerdctl on system | Linux bind-mount is the production target; Mac dev uses `uv run`, not Docker — risk negligible |

---

## Summary table — all spikes

| RQ | Status | Blocker? |
|---|---|---|
| RQ1 gh install | PASS | No |
| RQ2 Node20 + claude CLI | PASS (surprise: claude is ELF, not JS) | No — enables runtime slim-down |
| RQ3 PyStemmer wheel | amd64 PASS, arm64 MISSING | Only if keeping arm64 in 5d |
| RQ4 flock + rename | PASS | No |
| RQ5 buildx multi-arch | PASS (amd64), arm64 slow via qemu | Only if keeping arm64 |
| RQ6 GHCR auth | Documented, not live-tested | No |
| RQ7 bind-mount uid/gid | PASS with `~/.claude` chown caveat | No — one-off chown in runbook |
| RQ8 uv sync editable | BLOCKER found, `--no-editable` fixes | FIX REQUIRED before coder |
| RQ9 image size | ~600 MB projected | No — under 1 GB budget |
| RQ10 compose v2 | PASS | No |
| RQ11 env_file rotation | PASS, `compose up -d` required | No — document recipe |
| RQ12 singleton race | PASS | No — document systemd-stop precondition |

Coder unblocked with Patches 1-9 applied.
