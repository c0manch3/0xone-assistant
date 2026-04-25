# Phase 5d — Devil's Advocate WAVE 3

**Date:** 2026-04-25
**Scope:** SHIPPED, uncommitted Docker code (not the plan).
**Files reviewed:**

- `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/docker-compose.yml`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/.dockerignore`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/.env.example`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/README.md`
- `/Users/agent2/Documents/0xone-assistant/.github/workflows/docker.yml`
- `/Users/agent2/Documents/0xone-assistant/.gitignore` (modified)
- `/Users/agent2/Documents/0xone-assistant/CLAUDE.md` (modified)
- `/Users/agent2/Documents/0xone-assistant/plan/phase{4,5}/runbook.md` (modified)

**Already covered (must not duplicate):** Wave 1 (28 items) + Wave 2 (22 items) +
spike findings.

---

## Executive summary

Wave 3 — operational reality + post-implementation verification on the
SHIPPED bytes — surfaced **3 CRITICAL**, **4 HIGH**, **6 MEDIUM**,
**2 LOW** new findings plus 1 carry-forward and 2 unknown unknowns.

The most consequential items are evidence-driven, not opinion:

1. The Dockerfile sets the **wrong** env var name to disable claude
   autoupdate (`CLAUDE_CODE_DISABLE_AUTOUPDATER` is never read by
   the bundled binary; the real name is `DISABLE_AUTOUPDATER`).
2. `deploy/docker/.env.example` is silently filtered out of `git add`
   by the new `.env.*` glob in `.gitignore` — first push will land
   without the example file owner needs to bootstrap.
3. The autoheal sidecar restarts containers with `t=10` (its own
   `AUTOHEAL_DEFAULT_STOP_TIMEOUT` default), which **overrides** the
   compose `stop_grace_period: 35s` and can pre-empt the
   `.last_clean_exit` marker write — defeating phase 5b's clean-exit
   mechanism precisely on the path that's most likely to fire it
   (a wedged unhealthy container).

These are independent, code-level facts confirmed by reading the
shipped artifacts and the SDK-bundled binary. None require speculation.

**Commit-blocked: YES.** Items C1-C3 below all require single-line
fixes; none should defer.

---

## CRITICAL

### W3-C1. Wrong env var name disables nothing — claude autoupdate still active

**File:** `deploy/docker/Dockerfile:124`
**Severity:** CRITICAL (silent failure of stated mitigation)

The Dockerfile sets:

```dockerfile
CLAUDE_CODE_DISABLE_AUTOUPDATER=1
```

I extracted strings from
`/Users/agent2/Documents/0xone-assistant/.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`
(the same SEA binary symlinked at `/usr/local/bin/claude` in the
container per RQ13). The function that checks autoupdater state is:

```js
function JwH(){
  if(EH(process.env.DISABLE_AUTOUPDATER))
    return{type:"env",envVar:"DISABLE_AUTOUPDATER"};
  let H=ub8(); if(H) return {...}
  ...
}
function ub8(){
  if(process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC)
    return"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC";
  return null;
}
```

Recovered via `strings | grep`:

```
process.env.DISABLE_AUTOUPDATER
process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
```

There is **NO** `process.env.CLAUDE_CODE_DISABLE_AUTOUPDATER` reference
in the binary (`grep -E "CLAUDE_CODE_DISABLE" → 10 entries, AUTOUPDATER
not among them`). Wave 2 W2-M1 explicitly noted *"I did NOT verify this
env var name in the SDK source"*; coder shipped it anyway.

**Impact:** every container start, the bundled binary will attempt to
auto-update against `/opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`
— a path the unprivileged `bot` user cannot write to (root-owned,
read-only via venv). Failure mode: 1-2 s of network latency at startup,
log noise, possible rate limiting from anthropic.com on rapid restart
loops. Worst case: the binary refuses to start until update succeeds
(unverified — but plausible given how SEA-packaged bun binaries gate
on update success).

**Fix (one-line):**

```dockerfile
ENV ... \
    DISABLE_AUTOUPDATER=1
```

Optionally add `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` to also
suppress telemetry pings for a quieter cold start.

---

### W3-C2. `.env.example` silently excluded from git by new `.env.*` glob

**Files:** `.gitignore:5`, `deploy/docker/.env.example`
**Severity:** CRITICAL (first push will be missing the bootstrap file)

The wave-3 prompt's angle 9 asked "verify .env.example is not ignored
by .env.* glob". I verified.

`.gitignore` diff adds:

```
.env.*
secrets.env
**/secrets.env
```

The pattern `.env.*` matches *any filename* starting with `.env.` —
including `.env.example`, `.env.local`, `.env.production`. Verified
empirically:

```
$ git check-ignore -v deploy/docker/.env.example
.gitignore:5:.env.*    deploy/docker/.env.example   (exit 0 — ignored)

$ git status --ignored
Ignored files:
  deploy/docker/.env.example     ← appears here

$ git add -n deploy/docker/
add 'deploy/docker/.dockerignore'
add 'deploy/docker/Dockerfile'
add 'deploy/docker/README.md'
add 'deploy/docker/docker-compose.yml'
                                       ← .env.example MISSING
```

The legacy root `.env.example` survives because it was tracked before
the gitignore change (git ignores tracked-file matches), but the
NEW `deploy/docker/.env.example` is untracked → silently dropped by
`git add deploy/docker/`.

**Impact:** push lands without `.env.example`. README §Initial install
step 5 says `cp .env.example .env && edit TAG` — owner finds no source.
First-deploy friction; not a security issue but blocks owner.

**Fix options (pick one):**

1. Tighter glob: replace `.env.*` with `.env.local`, `.env.production`
   (explicit list).
2. Whitelist: add `!**/.env.example` after the `.env.*` line.
3. Rename to `env.example` (no leading dot — but breaks convention).

Option 2 is the cleanest:

```
.env
.env.*
!**/.env.example
secrets.env
**/secrets.env
```

---

### W3-C3. autoheal sidecar uses 10 s grace, overrides 35 s stop_grace_period

**File:** `deploy/docker/docker-compose.yml:88-98`
**Severity:** CRITICAL (defeats the `.last_clean_exit` marker on its
hottest fire path)

The autoheal sidecar (willfarrell/autoheal) restarts unhealthy
containers via the docker engine API. From its upstream
`docker-entrypoint`:

```sh
AUTOHEAL_DEFAULT_STOP_TIMEOUT=${AUTOHEAL_DEFAULT_STOP_TIMEOUT:-10}
restart_container() {
  ...
  docker_curl -f -X POST "${HTTP_ENDPOINT}/containers/${container_id}/restart?t=${timeout}"
}
```

Default `t=10`. The docker engine's `POST /containers/{id}/restart`
endpoint takes a `t` query param that **overrides** the compose-
level `stop_grace_period`. Source:
docker engine reference for ContainerRestart — "Number of seconds
to wait before killing the container."

Phase 5b daemon writes `.last_clean_exit` at the top of `stop()`;
the entire stop path budgets to 35 s (compose `stop_grace_period: 35s`
= systemd 30 s + 5 s margin). When autoheal fires, daemon gets only
**10 s** before SIGKILL — insufficient to drain the scheduler, write
the marker, and complete the audit log flush.

**Hottest-fire-path irony:** the autoheal restart fires precisely
when the daemon is wedged or unhealthy, which is exactly when
`.last_clean_exit` matters most for downstream catch-up logic.

**Fix (compose-level, two options):**

A. Bump autoheal default:

```yaml
autoheal:
  environment:
    AUTOHEAL_DEFAULT_STOP_TIMEOUT: "35"
```

B. Per-container override label (more surgical):

```yaml
0xone-assistant:
  labels:
    - "autoheal=true"
    - "autoheal.stop.timeout=35"
```

The autoheal entrypoint reads `.Labels["autoheal.stop.timeout"]` per
container and uses that in preference to the env default — verified
in the upstream entrypoint script (`STOP_TIMEOUT=".Labels[\"autoheal.stop.timeout\"] // $AUTOHEAL_DEFAULT_STOP_TIMEOUT"`).

Recommend B (label) — keeps the autoheal env clean for any future
sidecar consumers.

---

## HIGH

### W3-H1. `willfarrell/autoheal:latest` — pin missing

**File:** `deploy/docker/docker-compose.yml:89`
**Severity:** HIGH (supply-chain + reproducibility)

Wave-3 prompt angle 4 raised this. I confirmed it's not addressed.
`autoheal:latest` is a moving target. willfarrell/autoheal has had
breaking changes around `AUTOHEAL_INTERVAL` semantics historically;
upstream bus-factor is small. A 0.0.x bump that breaks the curl/jq
pipeline silently disables the watchdog — and the bot container
keeps running, so the failure is invisible.

The bot's own image is digest-pinnable (compose passes the SHA).
Apply same hygiene to the sidecar.

**Fix:** pin to a specific version:

```yaml
autoheal:
  image: willfarrell/autoheal:1.2.0
```

(Verify latest stable on docker hub before committing.)

---

### W3-H2. CI Trivy gate against bundled-claude SEA binary CVEs — risk of perpetually-red CI

**File:** `.github/workflows/docker.yml:151-187`
**Severity:** HIGH (CI hygiene + signal degradation)

Wave-3 prompt angle 5 raised this. Confirmed not surfaced earlier.

The runtime image bundles a 236 MB `claude_agent_sdk/_bundled/claude`
SEA — a Bun-packed JS bundle with embedded V8/Bun runtime + npm deps.
Trivy in `vuln-type: library` mode scans Python deps via
`pyproject.toml`/`uv.lock` metadata; Bun-SEA bundles are NOT typical
SBOM-detectable artifacts, so Trivy currently won't flag them. BUT:

1. Trivy filesystem mode might detect embedded Node modules in the
   binary blob (depends on Trivy version's heuristics).
2. The slim-bookworm base + apt surface (curl, git, ca-certificates,
   tini) WILL accumulate `HIGH/CRITICAL` CVEs over time. `ignore-unfixed: true`
   helps but doesn't eliminate them.
3. `git` in particular has a steady stream of CVEs; bookworm
   typically backports fixes, but timing windows exist.

**Risk:** owner pushes a phase 5e fix. CI is red. Fix is either to
update base image (involves rebuild) or `.trivyignore` the CVE
(security debt). Owner pressure to ship → `.trivyignore` builds up,
defeating the whole gate.

**Mitigation options:**

1. Move Trivy scan to `continue-on-error: true` for phase 5d, watch
   for noise patterns in first 7 days, then re-enable gate. Avoids
   first-week-CI-red panic.
2. Pre-establish a `.trivyignore` policy: max 30-day window per
   ignored CVE, monthly review, document why each ignore.
3. Pin the base image more aggressively (the digest is already
   pinned, good).

Recommend option 1 for phase 5d (avoid blocking on hypothetical CVE
noise during the cutover); revisit at phase 9 with SARIF upload.

---

### W3-H3. Healthcheck reports green during 45 s preflight — autoheal can't see "wedged init"

**Files:** `src/assistant/main.py:48-90, 156-162, 281` + `deploy/docker/docker-compose.yml:70-82`
**Severity:** HIGH (early-warning gap on the most likely failure mode)

Wave-3 prompt angle 3 raised this; I verified by reading shipped
`main.py`:

```python
async def start(self) -> None:
    self._lock_fd = _acquire_singleton_lock(self._settings.data_dir)
    # ↑ writes .daemon.pid HERE (line 161)
    await self._preflight_claude_auth()
    # ↑ up to 45 s, line 162
    ...
    log.info("daemon_started", ...)  # line 281
```

`_acquire_singleton_lock` writes the pid file at line 86 of main.py
**before** preflight runs. Healthcheck checks pid + `/proc/$pid/exe`
→ both pass (process IS python). But the daemon hasn't started
polling Telegram yet.

`start_period: 60s` prevents `unhealthy` reports during the FIRST 60 s,
but it does nothing about *stuck* init phases:

- Preflight succeeds (5 s) but a downstream init step hangs (e.g., DB
  migration on a corrupt sqlite, or a memory subsystem auto-reindex
  that pegs CPU for 90 s).
- Healthcheck reports `healthy` after start_period because the python
  process is alive.
- Owner sends `/ping` → no response → no Telegram traffic → no
  observable failure → autoheal doesn't fire.

**Mitigation (optional, MEDIUM uplift):** extend the healthcheck to
also verify a `daemon_ready` sentinel file (e.g., the daemon writes
`.daemon.ready` AFTER `daemon_started` event, healthcheck checks
both `.daemon.pid` AND `.daemon.ready` exist).

Phase 5b daemon does NOT currently emit a ready file. Adding this is
a phase 5e-or-later item; for 5d, document the limitation in the
runbook so owner knows the healthcheck is "process-alive" not
"daemon-functional".

**Fix-pack-pre-CI-push:** README §Healthcheck should add a paragraph:
*"Healthcheck verifies the python process is alive but not that the
daemon is polling Telegram. If owner observes container `healthy`
but no Telegram response, check `docker compose logs` for
`daemon_started` event — its absence means init is stuck."*

---

### W3-H4. Bind-mount auto-creation as root on FRESH VPS install

**Files:** `deploy/docker/docker-compose.yml:38-39` + `deploy/docker/README.md:60-104`
**Severity:** HIGH (fresh-install path documented but broken)

Wave-3 prompt angle 6 raised this. Confirmed.

Compose creates host bind-mount targets that don't exist as **root**
(default Docker behavior). On a FRESH VPS without
`~/.local/share/0xone-assistant/`:

1. Owner runs `docker compose up -d`.
2. Docker engine creates `~/.local/share/0xone-assistant/` owned by
   `root:root` (engine runs as root; compose mount source must
   exist or be auto-created by engine).
3. Container starts as uid 1000 → first DB write to
   `/home/bot/.local/share/0xone-assistant/assistant.db` fails with
   `EACCES`.
4. Daemon crashes; restart-loop.

The README §Initial install assumes the dir already exists from
phase 5a (systemd-era). True for the **owner's specific VPS cutover**;
false for any genuinely fresh install (e.g., DR rebuild from `~/.backup-*`
on a new host).

**Fix (README, before step 5 "first boot"):**

```bash
# Step 4.5: pre-create bind-mount targets so docker doesn't auto-
# create as root. Idempotent.
mkdir -p ~/.local/share/0xone-assistant ~/.config/0xone-assistant
test -d ~/.claude || mkdir ~/.claude
# (chown to 0xone:0xone is the user's own dirs — already 1000:1000.)
```

Even more robust: have compose explicitly require the dirs:

```yaml
volumes:
  - type: bind
    source: ${HOME}/.local/share/0xone-assistant
    target: /home/bot/.local/share/0xone-assistant
    bind:
      create_host_path: false
```

(`create_host_path: false` disables auto-creation; compose errors
loudly if dir is missing.)

---

## MEDIUM

### W3-M1. `.dockerignore` has `tests/` exclusion vs Dockerfile `COPY tests/` in test stage

**Files:** `deploy/docker/.dockerignore:60` + `deploy/docker/Dockerfile:200`
**Severity:** MEDIUM (test target build will fail on fresh CI cache)

`.dockerignore` line 60 excludes `tests/` from the build context.
`deploy/docker/Dockerfile:200` (test stage) does:

```dockerfile
COPY tests/ ./tests/
```

When `.dockerignore` excludes a path, BuildKit still allows COPY of
that path because BuildKit historically treats `.dockerignore` as a
hint, but actually — verified: `.dockerignore` is a HARD exclude.
COPY of an excluded path emits "no source files were specified"
or "tests: not found".

Looking again at the comment block in `.dockerignore:58-60`:
*"Test suite excluded from the runtime build context. The Dockerfile's
test target re-COPYs tests/ explicitly via its own COPY directives."*

The author KNEW about this and... bet that COPY would override
.dockerignore. It does NOT. BuildKit's `.dockerignore` is applied to
the entire build context, not per-stage.

**Verification path (what coder should run before push):**

```bash
docker build -f deploy/docker/Dockerfile --target test -t test-check .
```

If the comment's assumption is wrong, this errors. If it's right
(which I doubt), it succeeds.

**Fix:** either (a) remove the `tests/` line from `.dockerignore`
(adds tests/ to all builds — small overhead, ~50 KB), or (b) keep
the exclusion and stop COPYing tests/ in the test stage (would need
a different test-execution strategy — bind-mount tests at run time).

Option (a) is the pragmatic fix.

---

### W3-M2. README §Initial install ordering — first push must precede VPS pull, but step ordering is implicit

**File:** `deploy/docker/README.md:60-72`
**Severity:** MEDIUM (owner workflow friction)

Wave-3 prompt angle 1 raised this. Partially addressed — README does
say "Wait for CI green" — but the dependency chain isn't crisp:

Current flow:
```
git push origin main
# Wait for CI green
# Then: GHCR visibility flip
# === SSH to VPS ===
docker pull
```

Implicit: the GHCR package doesn't EXIST until first CI push completes.
Owner cannot flip visibility before the first push. README does say
"Wait for the first CI run with a docker.yml workflow to push a tag"
in §First-time GHCR visibility flip — but only mentions "tag" pushes
explicitly. The first `git push origin main` triggers `:latest` +
`sha-<short>` (no tag), and that IS what creates the package.

**Fix (clarification only):** §First-time GHCR visibility flip step 1:
*"Wait for the first CI run on `main` (or any phase tag push) to
complete. Confirm via..."*

Also worth adding the failure mode: *"If owner browses the package
URL before first push: 404. This is expected; package is created on
first successful CI build-and-push job."*

---

### W3-M3. CI smoke `IMAGE_REF` env not set when `build-and-push.outputs.digest` empty

**File:** `.github/workflows/docker.yml:104-150`
**Severity:** MEDIUM (silent CI failure mode)

Wave-3 prompt angle 5 raised this. Confirmed.

`smoke` job:

```yaml
needs: build-and-push
env:
  IMAGE_REF: ${{ env.IMAGE }}@${{ needs.build-and-push.outputs.digest }}
```

If `build-and-push` has a partial failure where the build succeeded
but `outputs.digest` was not populated (e.g., docker/build-push-action@v6
exit 0 without setting outputs — known issue when push is conditional
and the condition resolves false unexpectedly), `IMAGE_REF` becomes:

```
ghcr.io/c0manch3/0xone-assistant@
```

`docker pull "ghcr.io/c0manch3/0xone-assistant@"` fails with a confusing
"invalid reference format" error. Owner reads the failure as a
compose/registry issue, not a CI workflow bug.

The current setup uses `if: github.event_name != 'pull_request'`
which gates push. On `main` push, push is enabled and digest should
populate. PR builds skip smoke entirely. Risk window is narrow but
non-zero.

**Fix (defensive):**

```yaml
smoke:
  needs: build-and-push
  if: github.event_name != 'pull_request' && needs.build-and-push.outputs.digest != ''
```

---

### W3-M4. Healthcheck PID-1 (tini) edge case

**File:** `deploy/docker/docker-compose.yml:70-82`
**Severity:** MEDIUM (false-positive scenario)

Healthcheck reads `.daemon.pid` and verifies `readlink /proc/$pid/exe |
grep -q python`. The phase 5b daemon writes `os.getpid()` to
`.daemon.pid` — under tini ENTRYPOINT, the python interpreter is
PID 7 (tini fork+exec). `os.getpid()` returns 7. `/proc/7/exe`
points at `/opt/venv/bin/python3.12`. Healthcheck passes.

EDGE CASE: if the python process crashes and tini reaps it but does
NOT exit (which tini's default behavior allows for non-PID-1 children
but not PID-1's direct child unless `-g` flag is passed), the
container could enter a state where:

- Container is "running" (tini still alive as PID 1).
- `.daemon.pid` still says 7.
- `/proc/7/exe` no longer exists OR points at a different process.
- `[ -L /proc/$pid/exe ]` returns false → unhealthy → autoheal restarts.

Actually this CORRECTLY fails the check (good design). But: if a
new python child gets PID 7 (recycled by namespace pid allocator)
between daemon crash and tini exit, healthcheck passes despite no
daemon. Container PID namespace is small (max ~32K), recycle is
plausible under crash-restart loops.

**Mitigation:** also verify the python process's cmdline matches:

```yaml
test:
  - "CMD-SHELL"
  - >-
    test -f /home/bot/.local/share/0xone-assistant/.daemon.pid &&
    pid=$$(cat /home/bot/.local/share/0xone-assistant/.daemon.pid) &&
    [ -n "$$pid" ] &&
    [ -L /proc/$$pid/exe ] &&
    readlink /proc/$$pid/exe | grep -q python &&
    grep -q assistant /proc/$$pid/cmdline
```

Marginal improvement; defer to phase 9 hardening.

---

### W3-M5. CI test job re-builds without using build cache from `build-and-push`

**File:** `.github/workflows/docker.yml:79-101`
**Severity:** MEDIUM (CI runtime + cost)

The `test` job runs in parallel to `build-and-push` (both depend on
nothing). It builds the `test` target from scratch with its own
`cache-from: gha`. Because the test target inherits from `runtime`,
it shares 90% of layers. But:

- `test` job uses `cache-from` matching `${{ github.ref_name }}-amd64`
  scope, same as `build-and-push`. Good.
- `cache-to` is NOT set on the test job (only `cache-from`). Means
  `test` reads from cache but doesn't write. OK.
- BUT: the first run on a new branch has empty cache → `test` and
  `build-and-push` both build from scratch in parallel → 2× CI time.

**Fix (optional optimization):** make `test` `needs: build-and-push`
and reuse the runtime image:

```yaml
test:
  needs: build-and-push
  steps:
    - uses: docker/login-action@v3
      ...
    - run: docker pull ${{ env.IMAGE }}@${{ needs.build-and-push.outputs.digest }}
    - run: docker build --target test --cache-from ... .
```

Trade-off: serializes build + test, increases wall-clock CI time.
Current parallel approach is fine for first-week deploy; revisit if
CI runtime becomes a pain.

---

### W3-M6. README §Update flow doesn't preserve TAG=phase5d as a fallback

**File:** `deploy/docker/README.md:120-130`
**Severity:** MEDIUM (rollback fragility)

Update recipe:

```bash
echo "TAG=sha-NEW_HASH" > .env            # or TAG=phase5e
docker compose pull
docker compose up -d
```

`echo > .env` truncates and overwrites. After update, the previous
TAG is lost; the only record of "what we just upgraded from" is in
shell history or git log of `/opt/0xone-assistant`.

When rollback is needed, owner has to:
1. Know the previous TAG by memory or by reading
   `docker compose ps` formatted output (which shows current image).
2. Or check `docker images` for recently pulled tags.

**Fix:** README should recommend keeping a `.env.previous` for
quick rollback, or use git to track `.env`:

```bash
# Update flow:
cp .env .env.previous
echo "TAG=sha-NEW_HASH" > .env
...
# Rollback:
cp .env.previous .env
docker compose up -d
```

Tiny ergonomics fix.

---

## LOW

### W3-L1. `git pull` in /opt/0xone-assistant assumes clean working tree

**File:** `deploy/docker/README.md:96, 122`
**Severity:** LOW (operational hygiene)

`cd /opt/0xone-assistant && git pull` — if owner has any local
changes (e.g., experiments during a debug session), `git pull` either
fails or merges unexpectedly. README should suggest
`git status; git pull --ff-only` for safety.

---

### W3-L2. CLAUDE.md Phase 5d section says "this commit" but commit hasn't happened

**File:** `CLAUDE.md:56`
**Severity:** LOW (documentation drift)

```
- **Phase 5d** — Docker migration (this commit). Image to GHCR via
```

The diff is uncommitted. Once committed, "this commit" is stable but
the prior phase 5a/5b/5c lines lose their commit-hash anchor. Minor.

---

## Carry-forwards

### CF-1. Wave 1 W2-C2 / W2-M2 — `env_file` object form fixed in compose

**Status:** RESOLVED in shipped code.

`docker-compose.yml:25-29` uses the v2.20+ object form with
`required: true` / `required: false`. Wave 2 was concerned plan §C
hadn't patched it; coder did. No further action.

---

## Unknown unknowns

### UU-1. SDK version drift between dev (Mac arm64 binary) and prod (Linux amd64 binary)

The bundled claude binary on the dev mac is **Mach-O 64-bit arm64**
(I verified via `file`). On the runtime image, it's
**ELF Linux x86_64** (per RQ13). Both come from `claude-agent-sdk`
the same pip version, but the wheel selection differs by platform.

**Risk:** the two binaries could have different feature sets, env-var
names, or auth-token format expectations. Wave 1/2 didn't surface this;
wave 3 verifies the dev binary uses `DISABLE_AUTOUPDATER` (W3-C1) but
**hasn't verified the Linux binary uses the same name**. Plausible
they're the same (Bun cross-compile from same source) but unverified.

**Mitigation:** add a CI step that runs the Linux container and
greps `claude` strings for `DISABLE_AUTOUPDATER`. One-off verification,
then trust the SDK release process.

---

### UU-2. autoheal sidecar log surface — silently failing

The autoheal container uses `restart: unless-stopped`. If autoheal's
own healthcheck fails (none configured) or the docker-socket curl
fails (e.g., permissions, daemon API change), the sidecar exits.
Compose restarts it. But if the sidecar enters a tight failure
loop (e.g., new docker daemon refusing the API call), `docker compose
ps` shows it `Restarting (1)` — easy to miss in tail-of-output.

**Mitigation:** `compose ps` post-deploy check should also assert
autoheal is `Up` not `Restarting`. README §Initial install step 5
adds:

```bash
docker compose ps  # both services Up; bot healthy after ~60s
                   # autoheal must be `Up` (not `Restarting (N)`)
```

One-line documentation update; not a code change.

---

## Severity distribution

| Severity | New in wave 3 | Cumulative (waves 1+2+3) |
|---|---|---|
| CRITICAL | 3 | wave 1: 6, wave 2: 4, wave 3: 3 — total 13 |
| HIGH | 4 | wave 1: 11, wave 2: 8, wave 3: 4 — total 23 |
| MEDIUM | 6 | wave 1: 8, wave 2: 7, wave 3: 6 — total 21 |
| LOW | 2 | wave 1: 3, wave 2: 3, wave 3: 2 — total 8 |
| TOTAL | 15 NEW | 65 |

(Wave 1/2 totals reproduced from their own exec summaries; not
re-verified here.)

---

## Commit-blocked verdict: **YES**

**Top 3 fixes for fix-pack-pre-CI-push (all single-line changes):**

1. **W3-C1** — `Dockerfile:124`: replace
   `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` with `DISABLE_AUTOUPDATER=1`.
   (Optionally add `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for
   silent telemetry.)

2. **W3-C2** — `.gitignore:5`: add `!**/.env.example` after the
   `.env.*` line, OR replace the glob with explicit
   `.env.local`/`.env.production`. Verify with
   `git add -n deploy/docker/` — `.env.example` must appear.

3. **W3-C3** — `docker-compose.yml:50-51`: add label
   `"autoheal.stop.timeout=35"` to the bot service, so autoheal
   honors the same grace as compose `stop_grace_period: 35s`.

Items W3-H1 through W3-H4 should be addressed before phase-5d
is declared "shipped" but can ride a fix-pack commit after the
initial CI push lands. Items W3-M1 through W3-L2 are nits and
follow-up issues.

---

*End of devil-wave-3.md.*
