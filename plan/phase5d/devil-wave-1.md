# Phase 5d — Devil's Advocate Wave 1

**Reviewer:** devils-advocate (wave 1)
**Date:** 2026-04-21
**Verdict:** YELLOW — coder NOT blocked, but 6 CRITICAL items must be resolved
or explicitly accepted by owner before `deploy/docker/*` is authored.
Prescription is mostly sound; gaps are at the seams (filesystem,
GHCR policy, workflow permissions, healthcheck semantics).

---

## Executive summary

The plan gets the big-picture architecture right: multi-stage image,
bind-mounted state, GHCR distribution, systemd retained as fallback,
OAuth-only auth preserved. What it MISSES is concentrated in:

1. **Filesystem correctness** — bind-mount uid/gid alignment, pre-creating
   host paths before first `compose up`, second flock (vault, not just
   pid-file) is not mentioned (§B table + §J risk list both omit the
   vault flock in `_memory_core.py:612`).
2. **GHCR default-private** — first push creates package as PRIVATE; VPS
   anonymous pull breaks until owner flips visibility (§D).
3. **Smoke-job tautology** — §E smoke job "docker run with fake creds" will
   fail at `_preflight_claude_auth` (401) so the assert `daemon_started`
   within 15s cannot pass. The smoke job as written does not test what it
   claims (§E + §H second paragraph).
4. **Healthcheck semantics** — `unless-stopped` does NOT restart on
   unhealthy status. Compose tests that claim "if unhealthy, restart
   kicks in" are wrong (§C + §J item 10).
5. **Migration step ordering** — §F backups happen AFTER Docker install
   instead of before; first `compose pull` has no pre-flight credential
   check; reboot test doesn't handle in-flight scheduler fire.
6. **Data-sensitivity leak** — bind-mounting entire `~/.claude` includes
   `.claude/projects/*.jsonl` transcripts (155 MB on Mac today, grows on
   VPS). Any compromise of container gets full transcript corpus. Plan
   only discusses `.credentials.json`.

Net: plan is executable; the researcher or coder should patch §C, §D,
§E, §F before first Dockerfile byte is written.

**Counted items: 28** (6 CRITICAL, 10 HIGH, 8 MEDIUM, 4 LOW).

---

## CRITICAL (block coder until resolved or explicitly accepted)

### C1. Second flock (vault) missing from plan
Plan §B, §J item 1 and the dockerfile table at §B only mention `.daemon.pid`
flock. But `src/assistant/tools_sdk/_memory_core.py:612` has a SECOND
`fcntl.flock(LOCK_EX | LOCK_NB)` on the vault lock file, used during every
`memory_save` call. Phase 4 `_memory_core.py:574` even warns explicitly:
"network filesystem where `fcntl.flock` is silently a no-op."

**Impact:** RQ4 spike as written only tests pid-file flock. Vault flock is
invoked on every memory-write turn; if bind-mount layer silently no-ops it
on some FS (ZFS, btrfs, NFS, Docker-on-Mac gRPC-FUSE), concurrent writes
from scheduler-origin + telegram-origin turn corrupt the vault.

**Fix:** extend RQ4 to verify flock on BOTH pid-file AND vault lock file
path (`<data_dir>/.memory.lock` or similar — coder to confirm exact name).
Document VPS filesystem type before shipping (`df -T ~/.local/share/` on VPS).

### C2. GHCR default-private blocks first VPS pull
§D says "public package recommended". GitHub-Container-Registry's default
visibility for a brand-new package pushed by an Actions workflow is
**PRIVATE** (org- or user-scoped). First `docker compose pull` on VPS
attempts anonymous pull → 401/404. Owner must manually flip visibility
at `github.com/users/c0manch3/packages/container/0xone-assistant/settings`
after the first push.

**Impact:** §F step 6 "First boot: docker compose pull" fails on first
cutover attempt; owner has to interrupt migration, go to GitHub UI, wait
for propagation, retry.

**Fix:** §D must include an explicit "post-first-push, flip to public"
step, OR the VPS must pull with a read-only PAT and that PAT provisioning
must be in §F step 2 (alongside Docker install).

### C3. Smoke job will fail-fast on OAuth preflight (§E + §H)
§E smoke job: "docker run with fake creds, assert `daemon_started` within
15s then SIGTERM." §H integration test: "Container with fake OAuth + bogus
Telegram token → assert `daemon_started` event within 30s OR clean exit."

Phase 5a shipped `_preflight_claude_auth` that validates the session
against the Anthropic API BEFORE `daemon_started` fires. Fake creds → 401
→ `ClaudeAuthError` → process exits non-zero with no `daemon_started`
event.

**Impact:** the CI smoke job either:
- never produces `daemon_started` (test is effectively a no-op assertion
  that will always fail — breaks CI on every push);
- has to bypass preflight via a skip-env-var, which then stops validating
  the production codepath.

**Fix:** the smoke job must either (a) call a dedicated `python -m
assistant --self-check` codepath that exercises imports + MCP server
startup without network, or (b) seed a real test-account OAuth token in
a CI secret. The plan must pick one; neither is currently written.

### C4. `restart: unless-stopped` does NOT restart on unhealthy
§C healthcheck + §J risk 10 both assume "if unhealthy, restart policy
kicks in". This is false. `docker compose` restart policies (`no`,
`always`, `on-failure`, `unless-stopped`) trigger on container EXIT, not
on unhealthy. An unhealthy container stays unhealthy forever until
something else kills it.

**Impact:** daemon wedged (Telegram polling dead, but process alive) is
detected by healthcheck but not remediated. Container sits unhealthy
indefinitely.

**Fix:** either (a) use an external watchdog (systemd timer on VPS that
runs `docker compose ps --format json | jq ... && docker compose restart`
if unhealthy), or (b) add `autoheal` sidecar container, or (c) accept
that healthcheck is status-only and document. Plan currently conflates
the two semantics.

### C5. Host dirs must exist with correct uid BEFORE `compose up`
Plan §F step 5 runs `git pull` then §F step 6 `compose pull && up -d`.
But `~/.local/share/0xone-assistant/` on VPS already exists (phase 5a
owns it). What about fresh-host scenario?

`~/.claude/` on a fresh VPS with no prior Claude CLI install doesn't exist
until first CLI run. Compose with `-v ${HOME}/.claude:/home/bot/.claude`
auto-creates the host dir but with ROOT ownership (because compose daemon
runs as root in rootful mode, per §Q11). Container `user: 1000:1000` can't
write. Silent permission-denied.

Additionally, phase 5b subdirs like `~/.local/share/0xone-assistant/run/`
(if any — coder to verify) must exist before daemon starts.

**Impact:** fresh-host first boot fails with `Permission denied` writing
`.daemon.pid` or `.credentials.json`. Error trail is confusing.

**Fix:** install script in `deploy/docker/README.md` must explicitly:
```bash
mkdir -p ~/.claude ~/.local/share/0xone-assistant ~/.config/0xone-assistant
chown -R 1000:1000 ~/.claude ~/.local/share/0xone-assistant ~/.config/0xone-assistant
```
AND first-run must check paths at container startup and fail loud, not
silent. This is owner-facing; plan §F doesn't mention it.

### C6. `~/.claude/projects/*.jsonl` bind-mounted to container — data leak surface
`~/.claude` contains `projects/*.jsonl` — full conversation transcripts
for Claude Code sessions on that user account. Current Mac size: 155 MB.
VPS grows similarly over time. Plan bind-mounts the WHOLE `~/.claude`
read-write.

**Impact:** any code execution inside the container (via a compromised
`@tool`, CVE in `aiogram`/`claude-agent-sdk`/etc.) can read and exfil
months of transcripts — which may include credentials pasted into
sessions, API keys discussed in conversations, private repo file paths.

Only `.credentials.json` + `agents/` + `settings.json` need to be
accessible at runtime. `projects/`, `history.jsonl`, `cache/`,
`shell-snapshots/`, `file-history/` are all unrelated to the bot's
runtime.

**Fix:** bind-mount `${HOME}/.claude/.credentials.json` (file, not dir)
+ `${HOME}/.claude/settings.json` (ro), skip everything else. If claude
CLI refreshes tokens it only writes `.credentials.json`. If the CLI
unconditionally needs the whole `.claude/` tree, use a dedicated
`~/.claude-bot/` dir populated from a subset and point `HOME` /
`CLAUDE_CONFIG_DIR` at it inside the container. Needs RQ verification.

---

## HIGH (address before cutover; not blockers for Dockerfile drafting)

### H1. `docker-compose.yml` `env_file` with missing `secrets.env` errors out
§C lists TWO `env_file` entries. Compose v2 errors on missing env_file
(`open .../secrets.env: no such file or directory`). §J lists this not;
§F assumes both exist. If `secrets.env` is optional (phase 5a: leading
`-` in `EnvironmentFile=-...`), compose has no equivalent.

**Fix:** concatenate `.env` and `secrets.env` into a single env_file on
host (installer script), OR use `environment:` mapping with `${GH_TOKEN:-}`
fallback, OR commit that `secrets.env` is mandatory and install script
touches an empty one if missing.

### H2. `npm install -g @anthropic-ai/claude-code` path assumption
§B Stage 2: "Verify `claude --version`." COPY from nodejs → runtime stage
needs exact paths. On Debian slim with NodeSource install, global npm
root is `/usr/lib/node_modules` and bin is `/usr/bin/claude` (symlink).
COPY must include both AND the node binary AND any native modules
bundled in `/usr/lib/node_modules/@anthropic-ai/claude-code/node_modules/`.

Missing any one → `claude: command not found` or `Cannot find module X`.
Plan §B says "the claude CLI + node binary" — underspecified. Spike
RQ2 covers this but doesn't enumerate the exact COPY list.

**Fix:** RQ2 must produce the literal COPY lines, not just confirm
"installable".

### H3. PyStemmer arm64 wheel + postinstall on amd64-only buildx
§B Stage 4 + §L RQ3: wheel-first. Fine on amd64. For arm64 via `buildx`
+ qemu, `npm install -g` in stage 2 runs postinstall under emulation. If
the npm package ships a native binary downloaded by postinstall script,
it may download the amd64 one because the script reads `uname -m` which
under qemu returns the emulated arch — USUALLY correct, but some installers
detect emulation and fall back. RQ5 (multi-arch spike) doesn't cover
postinstall-arch-detection specifically.

**Fix:** RQ5 extended: run `docker run --rm --platform linux/arm64
<image> file /usr/lib/node_modules/@anthropic-ai/claude-code/<native-bin>`
to verify it's actually arm64. If wrong arch, drop arm64 for phase 5d
without arguing (§D manifest already allows this).

### H4. CI workflow permissions underspecified
§D says `packages: write` + `contents: read`. But:
- `actions/cache` (for `type=gha` buildx cache) needs `actions: write`.
- If workflow uses `softprops/action-gh-release` or any `gh release` step,
  needs `contents: write`.
- Trivy upload to GitHub security tab needs `security-events: write`.
- `docker/metadata-action` (common in docker workflows) reads tag
  references — needs `contents: read`, already there.

§E says `scan` job uses Trivy — if output is uploaded as SARIF to security
tab (common pattern), `security-events: write` missing.

**Fix:** enumerate ALL jobs + their minimum-required permissions in §E
or CI-workflow spec; don't just say "packages: write".

### H5. buildx cache `type=gha` evicts across tag+main
GHA cache has 10 GB per repo, LRU eviction. A full multi-arch Docker
build cache is 500 MB-1 GB. main push + tag push + PR build all write
caches concurrently. Layer cache hit rate drops; build time inflates.

**Fix:** use `scope=main-amd64` / `scope=pr-amd64` etc. in cache-from
/cache-to keys to prevent cross-branch eviction. Plan §D/E silent on
scoping.

### H6. Migration step 2 precedes step 3 (backup)
§F sequence: 1) Mac preconditions; 2) Install Docker on VPS; 3) Backup
state. Docker install pulls apt deps that can fail or leave dpkg in a
broken state. If step 2 half-breaks the VPS, owner has to fix it BEFORE
they can back up. Inversion: backup FIRST (read-only, can't fail badly),
then risky work (apt install).

**Fix:** swap steps 2 and 3. Trivial.

### H7. First boot has no OAuth/GH_TOKEN pre-flight
§F step 6: `export TAG=phase5d-rc1 && docker compose pull && docker
compose up -d`. No pre-flight that (a) `.env` exists and has required
keys, (b) `.claude/.credentials.json` exists and passes `claude --print
ping`, (c) `GH_TOKEN` in `secrets.env` hasn't expired.

If any is missing, container starts, hits `_preflight_claude_auth`, exits
non-zero, `restart: unless-stopped` respawns it, infinite log spam
drowning out the actual error. Same thing phase 5a solved with systemd
`Restart=on-failure`.

**Fix:** `deploy/docker/README.md` install script must include a
"dry-run" step: run a throwaway `docker run --rm --entrypoint
/opt/venv/bin/python ... -c "from assistant.bridge import
preflight_claude_auth; preflight_claude_auth(...)"` before `compose up`.

### H8. `kill -0 $(cat .daemon.pid)` healthcheck false-positive on container restart
§C healthcheck: `kill -0 $(cat .daemon.pid)`. On container restart, new
PID namespace. `.daemon.pid` from old run has some PID like 7 which,
in new namespace, is pid 7 too — but it may be a different process (e.g.
`tini` if entrypoint changes). `kill -0` returns true → healthcheck
green even though daemon never started.

Phase 5a singleton lock catches this via `fcntl.flock` (file-level, not
pid-level) — §B states this is preserved. So the race is narrow (first
~20s of start_period before daemon writes new pid + re-locks). Still:
healthcheck is lying during that window.

**Fix:** combine pid-file + fcntl probe in healthcheck, or rely on fcntl
only. Or accept — but document explicitly. Plan §J item 10 says
"accepted"; not clear the owner signed off on the specific 20s window.

### H9. `stop_grace_period: 35s` vs scheduler dispatcher shutdown
§C sets `stop_grace_period: 35s` (= systemd 30s + 5s margin). Phase 5b
scheduler dispatcher runs scheduled prompts with UP TO 30s configurable
timeout per shot (Phase 5b TimeoutStopSec=30s rationale). If a
scheduler-origin turn is mid-flight when SIGTERM arrives, Claude-SDK call
may not respect SIGTERM promptly (SDK uses its own subprocess; SIGTERM
propagation through subprocess tree is not guaranteed).

**Impact:** Docker SIGKILLs after 35s; `.last_clean_exit` write may be
pre-empted (the problem phase 5b fix-14 already solved for systemd).

**Fix:** verify `stop_grace_period: 35s` in Docker actually waits 35s
before SIGKILL (compose v2 behavior), and that daemon's shutdown path
calls `os.rename(tmp, .last_clean_exit)` EARLY in stop(), not at end.
RQ4 or dedicated spike.

### H10. CLAUDE.md patch scope leaks into future phases
Plan §K: "CLAUDE.md patch — Deployment section Docker + Fallback
systemd." CLAUDE.md is shared by all agent types. If the patch hard-codes
"build via `docker compose up`", future phases 6 (media sidecar) and 7
(vault git push with SSH deploy key mount) have to unpatch or negotiate.

**Fix:** CLAUDE.md should say "Deploy method: Docker compose; see
`deploy/docker/README.md`" and link out. Don't inline specifics that
future phases will want to amend.

---

## MEDIUM (tidy-up; not deployment blockers)

### M1. `docker-compose-plugin` vs `docker.io` version drift on Ubuntu 24.04
§F step 2 installs both. Ubuntu 24.04 ships `docker.io` 24.x and
`docker-compose-plugin` from the same repo. Fine today. But VPS may
already have docker.io from earlier phase (reference_vps_deployment.md:
"docker/containerd" listed as pre-installed). Running `apt install -y`
blindly upgrades docker; if a container is running on the VPS under an
older docker, apt upgrade may restart the daemon. RQ10 covers version
but not "is docker already installed, should we reuse".

**Fix:** install script: `dpkg -s docker.io >/dev/null 2>&1 || sudo apt
install -y docker.io`. Idempotent.

### M2. `docker compose` (hyphen) hard dependency in ops docs
Phase 5a ops docs + phase 5b runbook may reference `docker-compose` with
hyphen. Compose v2 dropped hyphenated binary by default (though Debian's
compatibility shim sometimes still provides it). Plan §K mentions updating
runbooks but doesn't spell out s/docker-compose/docker compose/.

**Fix:** explicit grep-and-replace pass across all plan + runbook files.

### M3. `.dockerignore` missing `plan/`
§K lists `.dockerignore` entries but is incomplete. `plan/` (hundreds of
MB of research + logs) is listed. `daemon/` (top-level dir, purpose
unclear) is not. `tools/` top-level dir is not. `justfile` is not.
Image-size audit (RQ9) may surface these.

**Fix:** complete enumeration after `ls /Users/agent2/Documents/0xone-assistant/`
(top-level dirs: `daemon`, `deploy`, `plan`, `skills`, `src`, `tests`,
`tools`, plus `justfile`, `pyproject.toml`, `uv.lock`, `README.md`,
`CLAUDE.md`, `uv.lock`). Whitelist approach safer than blacklist.

### M4. Log rotation: 30 MB ceiling per container is low
§C sets `max-size: 10m, max-file: 3` → 30 MB total. Phase 5b scheduler
emits `scheduler-audit.log` OUTSIDE docker logging (file in
`~/.local/share/0xone-assistant/`), but main daemon logs go to stdout →
json-file driver → 30 MB ring. At DEBUG level an hour of verbose logs
easily exceeds 10 MB; rotation loses history.

**Fix:** bump to `max-size: 50m, max-file: 5` (250 MB, still small), or
document that DEBUG logging should use file handler not stdout.

### M5. `TAG=latest` default in `image:` is a footgun
§C: `image: ghcr.io/c0manch3/0xone-assistant:${TAG:-latest}`. If owner
forgets to set TAG in VPS `.env`, `compose pull` grabs `:latest` — which
is whatever last main-push built. Phase 5d design wants `sha-<hash>` pin.

**Fix:** drop the `:-latest` default; force `TAG` unset → compose errors
loudly. OR document clearly in `deploy/docker/README.md` that TAG must
be set in `.env` for prod hosts.

### M6. Retention policy auto-pruning interacts badly with old rollback
§D: "CI prunes `sha-*` tags older than 30 days (keep last 10 regardless)."
If owner ships phase 5d, then phase 6 lands 45 days later with a bug that
surfaces 2 days post-cutover, owner wants to roll back to last phase-5d
tag. `:phase5d` tag exists (never pruned). But if owner wants a `sha-*`
from THAT phase (e.g. a fix-pack in-between phase 5d and 6), it may have
been pruned.

**Fix:** keep `sha-*` tags older than 30 days if they correspond to a
`phaseN` or `vX.Y.z` tag (common pattern: don't prune SHAs referenced by
a protected tag). Or accept and document.

### M7. `fcntl.flock` on ZFS/btrfs not verified for VPS
§J risk 1 + RQ4 cover flock on Linux bind-mount generically. VPS file-
system is not specified. If provider uses ZFS or btrfs for /home, flock
semantics differ (documented in kernel docs: POSIX locks are VFS-level,
generally work, but BTRFS subvolumes have quirks). Plan doesn't record
VPS fs type.

**Fix:** add `df -T ~/.local/share/0xone-assistant/` output to the
deployment research doc. Owner-visible.

### M8. Rollback path not validated against pruned images
§E rollback: `export TAG=sha-<oldhash> && compose pull && up -d
--force-recreate`. If sha was pruned, `compose pull` fails with
manifest-unknown. No pre-flight check. Owner stuck.

**Fix:** `docker manifest inspect ghcr.io/c0manch3/0xone-assistant:$TAG`
as pre-flight before `compose pull`; if manifest missing, refuse and
point owner at `:phase5d` / `:v0.5.d` non-pruned tags.

---

## LOW (nice-to-have)

### L1. `stop_grace_period` 35s not tested across host reboot
§F step 8 reboots VPS. systemd (the host init) sends SIGTERM to docker
daemon; docker cascades SIGTERM to containers with default 10s grace
UNLESS configured at docker daemon level (`/etc/docker/daemon.json`
`shutdown-timeout`). `stop_grace_period` in compose is for `docker stop`
/ `compose down`, NOT host-reboot flow.

**Fix:** set `shutdown-timeout` in `/etc/docker/daemon.json` to 40s in
install script.

### L2. Healthcheck runs shell inside container; BusyBox vs bash
§C `CMD-SHELL` uses `/bin/sh`. python:3.12-slim-bookworm has `dash`. `$$`
escaping in compose string should still work with dash. Test.

### L3. GHCR retention requires a separate action
§D prune needs another job (`snok/container-retention-policy` or similar).
Plan doesn't name an implementation.

### L4. Image-size estimate range 500-900 MB → 1 GB cap gives no safety margin
§B expected size upper bound (900 MB) is the same as §Q12 red line (1 GB).
If node + apt + venv all bloat, single dep addition breaks the cap. Target
should be "900 MB with 100 MB headroom before alarm".

---

## Assumptions the plan relies on

1. **VPS filesystem is ext4** — not verified. ZFS/btrfs changes flock
   semantics (M7).
2. **`claude-agent-sdk` spawns subprocess with env inheritance** — phase
   5a architecture depends on this; §G assumes the chain
   compose → container → daemon → `gh api` subprocess works identically
   to systemd EnvironmentFile. Plausible, not explicitly re-verified.
3. **Node 20 + `@anthropic-ai/claude-code@2.1.116` works on slim-bookworm**
   — RQ2 spike tests on buildx, not on VPS Ubuntu-24. Should be fine
   (debian slim on both), but VPS runs Ubuntu 24.04 which differs in
   some apt-repo metadata.
4. **PyStemmer has cp312 manylinux_2_34 wheel for both amd64 and arm64**
   — RQ3 verifies.
5. **GitHub Actions `GITHUB_TOKEN` has `packages:write` scope when
   explicitly granted** — yes, but default is read-only; plan correctly
   calls out.
6. **Owner's GHCR account is personal `c0manch3`, not an org** — affects
   package visibility defaults and PAT scoping. Verified from `git remote
   -v`.
7. **Bind-mount into `/home/bot/.claude` works transparently across
   `~/.claude/.credentials.json` atomic rename** — plan §G claims
   "atomic on Linux bind mounts". Verified by RQ4. Mac Docker Desktop
   may differ (acknowledged).
8. **VPS user `0xone` has uid 1000** — confirmed by reference memory.
   Mac Docker Desktop user-namespace remap uses `1000:1000` regardless
   of host uid — acceptable for dev.
9. **Retain `deploy/systemd/` fallback means UNIT FILE only, not
   installed** — §Q6 says "doesn't install by default". §F step 9 removes
   `~/.config/systemd/user/0xone-assistant.service`. Correct semantic.
10. **`platformdirs` respects `HOME` env var in container** — yes, and
    `HOME=/home/bot` is set in Stage 5. Implicit in the plan, should be
    explicit in Dockerfile.

---

## Scope creep vectors

- **Dev compose override** (§C last para): "defer to phase 5d backlog."
  If coder writes both prod + dev compose because "why not, it's small",
  phase 5d slips. Hard-no.
- **Image signing / SBOM** (§J non-goals): if Trivy output is ugly,
  temptation to fix NOW. Phase 9.
- **Rootless Docker**: §Q11 defers. If first-cutover permission issues
  surface, tempting to "just use rootless". Don't.
- **Multi-container (scheduler sibling)**: §M phase 8. If coder thinks
  "I should at least shape `docker-compose.yml` for two services now,"
  they add a `profiles:` structure that's untested.
- **Migrating runbook-style docs to `deploy/docker/README.md`**: phase
  5b/5a runbooks live in `plan/`. Copying them verbatim adds 500-1000
  lines of README.

---

## Unknown unknowns

1. **Does `docker compose logs -f` preserve color/structured-event
   formatting** the same way `journalctl` did? Phase 5b ops relied on
   reading structured JSON events from journal; if docker json-file
   driver wraps each line in extra JSON, log-tail readability degrades.
2. **What happens to `.credentials.json` when host Mac's claude CLI AND
   VPS container's claude CLI both refresh simultaneously?** Refresh
   tokens are one-shot in some OAuth flows — second refresh fails, one
   host loses session. Not a Docker-specific bug, but Docker makes
   concurrent dev/prod easier, increasing probability.
3. **GHCR rate limits on anonymous pulls**: 60 req/h per IP for
   anonymous. VPS-reboot-loop in a bad deploy could exceed. Authenticated
   pull is 5000/h. Plan prefers anonymous.
4. **Container restart under memory pressure**: VPS has finite RAM
   (unknown — not in reference). PyStemmer compilation fallback +
   Python venv + node + claude CLI in resident memory could be 400 MB
   rss. If VPS has 512 MB, OOM kill under peak.
5. **Docker pull interrupted mid-stream**: VPS bandwidth ~2 min for
   400 MB compressed. Partial pull leaves container images in
   inconsistent state; compose retries. But what if the partial image
   is `latest`-tag poisoning? Low probability but no rollback for a
   poisoned `latest`.
6. **`xfs_quota` or per-user disk quotas on VPS**: `~/.claude/projects/`
   grows unbounded (conversation transcripts). If VPS has a 10 GB
   `/home` quota for user `0xone`, in 6 months the bot's own Claude
   history eats it.

---

## Verdict

**YELLOW — Reconsider specific aspects before coder.**

Coder is NOT blocked from drafting the Dockerfile (stages 1-5 can proceed
on spikes RQ1-RQ3 alone). Coder IS blocked from authoring
`docker-compose.yml` until C1 (second flock), C4 (healthcheck semantics),
and C5 (host-dir pre-creation) are patched into `plan/phase5d/description.md`.

Coder is blocked from writing `deploy/docker/README.md` and §F migration
runbook until C2 (GHCR visibility), C3 (smoke job realistic test), C6
(bind-mount scope) are patched.

**Top 3 pre-coder fixes (in order):**

1. **Patch §C healthcheck section** to acknowledge `unless-stopped`
   doesn't restart-on-unhealthy; add external watchdog spec or accept
   status-only semantics. (C4)
2. **Patch §D with GHCR default-private workaround** (manual visibility
   flip OR PAT-based pull) AND add explicit package-visibility step to
   §F migration. (C2)
3. **Patch §B + §J to enumerate BOTH flocks** (pid-file AND vault);
   extend RQ4 spike to cover both. (C1)

After those three, the coder can start; C3/C5/C6 can land as fixes in
same PR before owner-smoke.

---

**Severity distribution:** 6 CRITICAL, 10 HIGH, 8 MEDIUM, 4 LOW = 28 items.
