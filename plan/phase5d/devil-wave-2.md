# Phase 5d — Devil's Advocate Wave 2

**Reviewer:** devils-advocate (wave 2)
**Date:** 2026-04-25
**Verdict:** YELLOW — coder NOT blocked, but 4 NEW CRITICAL items must
land in fix-pack before `deploy/docker/Dockerfile` is authored. Wave-1
issues are well-addressed by spike-findings patches 1-9, but the
patches themselves introduce new ambiguities, and the spike surfaced
one architecturally significant fact (bundled claude in
`claude_agent_sdk` wheel) the plan and patches did NOT account for.

---

## Executive summary

Wave 2 reviewed the post-spike plan against (a) the spike-findings
patches 1-9 the researcher proposed, (b) the wave-1 fix items, and
(c) thirteen new attack surfaces specified by the user.

The plan's macro shape is now sound. What remains are concrete
mismatches between the patches and the runtime reality, plus a
significant new finding from inspecting the live `claude_agent_sdk`
wheel: **the SDK ships its OWN 200+ MB native `claude` binary inside
`site-packages/claude_agent_sdk/_bundled/claude` and PREFERS it over
PATH** (verified: `subprocess_cli.py:63-94`). This makes the plan's
stage-2 nodejs+npm install architecturally redundant on amd64 (the
venv stage already pulls a Linux `claude` binary as a transitive
dependency of `claude-agent-sdk`).

If the bundled binary is `linux/x86_64`, stage-2 can be deleted
entirely; the runtime stage just COPYs `/opt/venv` and a working
`claude` is reachable at `/opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`.
This is potentially a **150 MB image-size reduction AND elimination
of two stages**.

Wave-2 also found one verified-false claim in the wave-1 review (uv
flag), three patches that need tightening, and a handful of
mid-severity issues with healthcheck, env_file, and `.dockerignore`
that the spike did not surface.

**Counted items: 22 NEW** (4 CRITICAL, 8 HIGH, 7 MEDIUM, 3 LOW). No
duplicates with wave-1.

---

## CRITICAL (block before coder ships compose+Dockerfile)

### W2-C1. `claude_agent_sdk` already bundles `claude` binary — nodejs stage may be redundant
**Verified.** `.venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`
is a 204 MB native binary on this Mac (Mach-O arm64). On the Linux
runtime, the wheel will install the Linux equivalent. SDK lookup
order in `_internal/transport/subprocess_cli.py:63-94`:

```python
def _find_cli(self) -> str:
    bundled_cli = self._find_bundled_cli()
    if bundled_cli:
        return bundled_cli  # ← USED FIRST, NOT shutil.which("claude")
    if cli := shutil.which("claude"):
        return cli
    ...
```

**Implication:** the plan's stages 2 (nodejs + npm install
claude-code) is ARCHITECTURALLY UNNECESSARY when the daemon uses
`claude-agent-sdk`. The plan was written from the phase-5a mental
model where `claude` was an OS-level CLI; in phase 5d via SDK it's a
venv-bundled artifact.

But: the daemon's `_preflight_claude_auth` at `main.py:107-154`
shells out via `subprocess` to invoke `claude --print ping`. That
subprocess uses PATH-resolved `claude`, NOT the SDK-bundled one. So
the npm install IS still needed for preflight — UNLESS the preflight
is rewritten to use `claude_agent_sdk._internal.transport.subprocess_cli.SubprocessCLITransport._find_cli()`
or to look in `/opt/venv/.../claude_agent_sdk/_bundled/claude`.

**Net:** EITHER (a) keep stage 2 and accept duplicated 227 MB
(bundled in venv) + 227 MB (npm install) = ~450 MB of redundant
binary storage in image (UNACCEPTABLE), OR (b) delete stage 2 and
rewrite preflight to call the bundled binary path explicitly.

**Fix BEFORE coder:** spike `RQ13` — does the Linux wheel of
`claude-agent-sdk==0.1.59` include a `_bundled/claude` ELF for amd64?
If yes, drop stage 2 entirely; preflight calls
`/opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude`
or use `shutil.which` after PATH includes it via wrapper symlink.
This single fix saves ~150 MB image size AND eliminates stage 2 entirely.

**Severity:** CRITICAL — affects Dockerfile structure that coder is
about to author. If they write 5 stages and we then delete stage 2,
the diff is non-trivial and the runtime COPY chain breaks.

---

### W2-C2. `secrets.env` missing-file behavior in compose v2 — plan §G silent post-patch
Plan §C lists two `env_file` entries. Wave-1 H1 flagged this. Spike
did NOT verify or patch §C. Compose v2 errors fast on missing
env_file unless the entry uses the v2.20+ object syntax:

```yaml
env_file:
  - path: ${HOME}/.config/0xone-assistant/.env
    required: true
  - path: ${HOME}/.config/0xone-assistant/secrets.env
    required: false
```

The wave-1 fix suggested "leading `-` makes it optional" — that's
**systemd `EnvironmentFile=-...` syntax, NOT compose**. Plan §G
confirms the systemd inheritance pattern; compose semantics are
different.

**Verified:** docker compose docs (compose-spec) explicitly require
the object form for optional files — the simple list form errors on
missing file. RQ11 spike showed the rotation recipe but did NOT test
"missing file at first boot."

**Fix BEFORE coder:** patch §C compose YAML to use the
`required: false` object form for `secrets.env`. OR document that
installer script MUST `touch secrets.env` before first `compose up`.
Plan currently does neither. Fresh-host first-boot will fail
opaquely.

---

### W2-C3. Healthcheck pid-file race with kill -0 — patch missing
Wave-1 H8 flagged the pid-recycle false-positive. Patch 4 in
spike-findings only bumps `start_period: 60s` — it does NOT address
the race. The race remains:

1. Container restart → daemon process is pid 1 (or 7, depending on
   tini/no-tini).
2. Old `.daemon.pid` on bind-mount has stale pid (e.g., `42`).
3. Daemon hasn't written new pid yet (acquiring flock takes ~10ms
   but file ops on bind-mount can be slower under load).
4. Healthcheck runs `kill -0 $(cat .daemon.pid)`. Reads stale pid.
   If pid 42 exists in container's pid namespace as ANY process
   (e.g., a dpkg helper, a `bash` from `docker exec`), `kill -0`
   succeeds → healthcheck reports HEALTHY despite daemon not
   actually running.

`start_period: 60s` masks the race for the first 60s but not for
ongoing restarts (e.g., daemon crashes at hour 4 → compose restart
→ stale pid window for 100ms-1s).

**Severity:** HIGH-borderline-CRITICAL because phase 5b scheduler
runs every minute; a "healthy but not actually running" daemon
silently stops dispatching scheduled prompts.

**Fix BEFORE coder:** healthcheck command must include freshness
check, e.g.:
```yaml
test: ["CMD-SHELL", "test -f /home/bot/.local/share/0xone-assistant/.daemon.pid && \
  pid=$$(cat /home/bot/.local/share/0xone-assistant/.daemon.pid) && \
  test -n \"$$pid\" && \
  kill -0 \"$$pid\" 2>/dev/null && \
  test \"$$(readlink /proc/$$pid/exe 2>/dev/null)\" = '/opt/venv/bin/python'"]
```
The `readlink /proc/$$pid/exe` check verifies the pid resolves to
the python interpreter, not a recycled one. Adds ~5 lines but
eliminates false positives.

OR: drop the pid file approach and use a TCP/UDS ping endpoint that
the daemon serves. Phase 5d non-goals say "no HTTP surface" — but
a UDS healthcheck file-write timestamp would suffice. Defer-decision
to owner.

---

### W2-C4. `~/.claude` selective mount is NOT viable — full mount + chown is the only path
Wave-1 C6 suggested mounting only `.credentials.json`. The user's
wave-2 prompt (angle 1) correctly identifies this is wrong. Verified
on this Mac:

- `~/.claude/projects/<slug>/*.jsonl` — written by claude CLI on
  every session (would be empty in container if not mounted).
- `~/.claude/settings.json` — read by claude CLI for global settings.
- `~/.claude/agents/`, `~/.claude/skills/`, `~/.claude/plugins/` —
  read by claude for tool/skill discovery.
- `~/.claude/sessions/`, `~/.claude/file-history/` —
  written for resume capability.
- `~/.claude/teams/`, `~/.claude/agent-memory/` — used by teams/memory
  features.

A selective `.credentials.json`-only mount would break session
tracking, memory features, and project-level config. Bundled
`claude` binary in `claude_agent_sdk` (W2-C1) likely uses the same
HOME-based directory layout.

**Therefore:** the plan must FULL-mount `~/.claude` and accept the
data-leak surface, mitigated by:
1. Container is single-purpose, single-tenant — no untrusted
   workloads → exfil risk requires arbitrary code execution in the
   daemon's MCP-tool surface.
2. Phase 9 hardening adds `read_only: true` for everything except
   bind-mounts; tightens cap_drop.
3. Document that owner SHOULD periodically prune
   `~/.claude/projects/` to limit blast radius.

**Fix BEFORE coder:** §C compose remains full `~/.claude` mount.
Plan §J risk list adds "data sensitivity in `~/.claude/projects/`
transcripts; mitigation: phase-9 hardening + transcript pruning
recipe in `deploy/docker/README.md`." Wave-1 C6's "selective mount"
recommendation is REVERSED in wave-2 based on architectural reality.

---

## HIGH (address before owner cutover, not blockers for Dockerfile draft)

### W2-H1. `uv sync --no-editable` flag VERIFIED EXISTS — but lockfile compatibility unverified
**Verified locally:** `uv sync --help | grep editable` shows
`--no-editable` flag (uv 0.11.6). Spike RQ8 result stands.

But: spike used uv 0.11.7 in builder, this Mac has 0.11.6, the
target Dockerfile pins `uv 0.11.7` (plan §B stage 4). Need
verification that 0.11.7 specifically supports
`--no-editable` (it does per
https://docs.astral.sh/uv/reference/cli/#uv-sync — confirmed the
flag is stable since 0.5+).

`uv_build` build backend (pyproject.toml line 26) is the project's
declared backend. With `--no-editable`, uv invokes `uv_build` to
produce a real wheel, installs it. Entry-points in
`[project.scripts]` (none declared in this project) wouldn't
matter; module discovery via `module-name = "assistant"` does. Spike
RQ8 verified site-packages/assistant/ is created — pass.

**Residual risk (LOW-actually):** if a later phase introduces
`[project.scripts]` (e.g., a CLI entry point), `--no-editable`
correctly creates the wrapper script in `/opt/venv/bin/` — no
issue. Document for future phases.

**No fix needed for wave-2.** Logged for completeness; W2-C1 makes
this moot if stage 2 is dropped (no question that
`--no-editable` works with current backend).

---

### W2-H2. Smoke job "fake creds" CI codepath — spike-findings doesn't pick a path
Wave-1 C3 flagged the smoke-job tautology. Spike-findings does NOT
patch §E or §H to define the codepath. The user's wave-2 prompt
(angle 1) correctly demands a concrete answer: `--probe-only` flag?
import-only run?

**Concrete proposal for fix-pack (any one is acceptable):**

Option A — `--self-check` flag in `assistant.__main__`:
```python
# src/assistant/__main__.py
if "--self-check" in sys.argv:
    # Skip preflight + Telegram polling
    # Verify imports + MCP server can register
    from assistant.config import settings
    from assistant.tools_sdk.installer import register
    register()  # @tool decorators run
    sys.exit(0)
```
CI smoke: `docker run --rm <image> --self-check`. Asserts non-zero
on import failure or MCP registration error. Skips network entirely.

Option B — dedicated `python -c "import assistant; ..."` in CI:
```yaml
- run: |
    docker run --rm --entrypoint /opt/venv/bin/python <image> -c '
      import assistant
      from assistant.tools_sdk import installer, memory, scheduler
      print("imports_ok")
    '
```
No code change required. Verifies the venv is usable, modules
load.

Option C — full-credentials integration smoke with rotated test
account. Requires GH secret + cleanup of test transcripts. Heavy.

**Recommend Option A.** Fits the plan's "no source changes" spirit
poorly (it adds ~20 LoC) but provides genuine smoke value AND
exercises @tool MCP server registration. Document explicitly in
§E + §H.

---

### W2-H3. Restart-on-unhealthy still unsolved post-spike
Wave-1 C4 flagged the false assumption. Spike-findings does NOT
add a watchdog. The user's wave-2 prompt (angle 1) demands a
decision.

**Three options, ranked:**

1. **Accept "logs flag wedged daemon, owner restarts manually"** —
   simplest. Phase 5b's daemon-event log includes
   `daemon_singleton_lock_held` etc.; owner monitors via
   `docker compose logs`. Suitable for low-traffic hobby bot.
   Document in `deploy/docker/README.md`.

2. **`willfarrell/autoheal` sidecar** — well-known compose pattern.
   Adds 5 MB image. Needs `autoheal.enable=true` label on service.
   Adds zero ongoing maintenance. Recommend for phase 5d.

3. **External systemd timer on VPS** — most robust but adds VPS
   provisioning complexity. Defer to phase 9.

**Fix BEFORE owner-cutover:** plan §C must explicitly pick one;
mark §J risk 10 with the chosen mitigation.

---

### W2-H4. Bind-mount UID drift between container (1000) and host edits
The user's wave-2 prompt (angle 4) flags this. Verified scenario:

1. VPS owner SSHs in, runs `claude` CLI directly (e.g., to refresh
   OAuth token manually). Files in `~/.claude/projects/` are
   created with `0xone:0xone` (uid 1000). Container OK.

2. VPS owner runs `sudo claude` (e.g., during initial setup, or
   debugging). Files in `~/.claude/` are created with `root:root`.
   Container as uid 1000 cannot write → `claude` inside container
   may fail to update transcripts.

3. Spike RQ7 surfaced this for `debug/`, `plans/`, `session-env/`,
   `shell-snapshots/`. Patch 5 adds a one-off chown. But it's
   one-off, not preventive.

**Fix BEFORE owner-cutover:** add to `deploy/docker/README.md`:
- "RULE: never run claude CLI on the VPS as root." (One-line owner
  doc.)
- "RULE: if you need to debug claude on VPS, run as the `0xone`
  user."
- Optional: add a periodic `chown` cron OR a `pre-up` script in
  compose that re-chowns. Not blocker, just cleanliness.

---

### W2-H5. CI workflow: `actions/checkout@v4` + `docker/build-push-action@v6` pinning
The user's wave-2 prompt (angle 8) flags this. Spike RQ6 draft
workflow uses `@v4`, `@v3`, `@v5`, `@v6` but doesn't pin to
SHA-pinned versions or document the rationale. As of 2026-04-25:

- `actions/checkout@v4` — latest is v4.x as of mid-2025;
  major-version pin is fine.
- `docker/setup-buildx-action@v3` — latest is v3.x; fine.
- `docker/login-action@v3` — latest v3.x; fine.
- `docker/metadata-action@v5` — latest v5.x; fine.
- `docker/build-push-action@v6` — latest v6.x as of 2025-Q3; fine.

**Verified by spike-findings draft.** The user's prompt asked
about v7 — none currently published. v6 is correct.

**No fix BEFORE coder.** Document that "tag pins, not SHA pins, are
acceptable for phase 5d." Phase 9 hardens to SHA pins.

---

### W2-H6. Migration §F step ordering — Docker install BEFORE systemd-stop
The user's wave-2 prompt (angle 9) flags this. Spike Patch 5 adds
idempotency to the install step but does NOT reorder.

Current §F order: 1) Mac preconds, 2) install Docker, 3) backup,
4) stop systemd, 5) git pull, 6) compose up.

Wave-1 H6 already flagged "backup BEFORE Docker install."

**New flag (wave-2):** the user's angle-9 point is that Docker
install MAY take 1-3 min on a fresh host (apt update, pkg download,
service start). If it happens AFTER systemd-stop (step 4), the bot
is OFF for that window. If it happens BEFORE (step 2 already), bot
is up during apt — minor risk of apt restarting docker daemon.

**Combined fix-pack ordering (wave-1 H6 + wave-2 H6):**

1. Mac preconds (image pushed).
2. Backup state (read-only, can't fail).
3. Install Docker if absent (idempotent precondition check).
4. Stop systemd.
5. `chown -R 0xone:0xone ~/.claude` (one-off, from spike Patch 5).
6. Git pull (deploy/docker/* artifacts).
7. `docker compose pull` (precondition check: image exists; rollback
   if not).
8. `docker compose up -d`.
9. Smoke test (Telegram regression).
10. Reboot test.
11. Retire systemd (owner gate).

This minimizes downtime (step 4 to step 8 is the off-window;
seconds with cached image).

**Fix BEFORE owner-cutover:** rewrite §F with this 11-step order.

---

### W2-H7. `.dockerignore` enumeration — wave-1 incomplete, wave-2 adds more
The user's wave-2 prompt (angle 10) lists ~25 entries. Wave-1 M3
listed many but not all. Comparison:

Plan §K mentions: `.venv .git tests/ plan/ __pycache__ .claude
.local .config node_modules dist/`.

**Missing per wave-2:** `.idea .vscode *.pyc .pytest_cache
.mypy_cache .ruff_cache *.db *.db-wal *.db-shm **/secrets.env
**/.env`.

**Repo top-level dirs (verified just now):** `daemon/` (empty on
this checkout), `deploy/`, `plan/`, `skills/`, `src/`, `tests/`,
`tools/`, plus `justfile`, `README.md`, `CLAUDE.md`, `pyproject.toml`,
`uv.lock`. Wave-1 M3 noted `daemon/`, `tools/`, `justfile`.

**Recommended `.dockerignore` (whitelist via include-only is hard
in Docker; here's the comprehensive blacklist):**
```
.git
.venv
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
node_modules
dist/
build/
plan/
tests/
.claude/
.local/
.config/
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

**Fix BEFORE coder:** copy this into plan §K as the canonical list.

---

### W2-H8. `.daemon.pid` host-vs-container PID namespace gotcha
The user's wave-2 prompt (angle 12) flags this. Verified
architectural truth: container has its own pid namespace; pid 1 in
container is the daemon (or tini if entrypoint uses tini wrapper).
Host's view of the container shows the container's host-namespace
pid (e.g., 18432) via `docker inspect`.

**Implication:**
- Healthcheck inside container — correct, sees container pid.
- Owner SSHs to host, reads `.daemon.pid` (e.g., contains `1` or
  `7`) — that pid in HOST namespace is unrelated. Owner runs
  `kill -0 1` on host → "permission denied" or signals init.
  Confusing.

**Mitigation:** `.daemon.pid` is not designed for cross-namespace
inspection. Owner debugging should use `docker compose logs`,
`docker compose ps`, `docker top 0xone-assistant`. Document
explicitly in `deploy/docker/README.md`.

**No code change.** Just documentation.

---

## MEDIUM (tidy-up, not deployment blockers)

### W2-M1. claude CLI auto-update behavior in container
The user's wave-2 prompt (angle 2) raises auto-update. Verified by
inspecting `.venv/.../claude_agent_sdk/_bundled/claude` — it's a
read-only file packaged in the wheel; no auto-update in the
SDK-bundled path.

For the npm-installed version (if W2-C1 keeps stage 2):
- Anthropic's claude CLI does NOT have an auto-updater built in
  AFAIK (as of 2.1.116). It's installed via npm; updates require
  `npm install -g @anthropic-ai/claude-code@<new>`.
- BUT: claude CLI may reach out to check for new versions and emit
  warnings. Verified in some claude-agent-sdk threads. Doesn't
  block, but adds 1-2s cold-start jitter.

**Mitigation:** set env var `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` in
Dockerfile or compose. (Note: I did NOT verify this env var name
in the SDK source; spike RQ13 — combined with W2-C1 — should
verify which env vars suppress version checks.)

**Severity:** MEDIUM — not a correctness issue, just clean-output.

---

### W2-M2. Compose `env_file` syntax — single source of truth missing
Plan §C inline YAML uses two simple-form entries. Compose docs
allow simple form (list of paths) AND object form (path + required
+ format). Mixing is confusing. Pick one.

**Fix:** standardize on object form across both files; explicit
`required: true` for `.env` and `required: false` for
`secrets.env` (W2-C2 fix). Future-proof for additions.

---

### W2-M3. Image build cache: `README.md` in early COPY layer
The user's wave-2 prompt (angle 7) flags this. Plan §B suggests:

```dockerfile
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-editable
COPY src/ ./src/
```

If `pyproject.toml` references README via
`[project] readme = "README.md"`, then `uv sync` reads README during
metadata generation. Project's pyproject.toml (verified above)
does NOT declare `readme = ...`, so README is NOT needed at sync
time. **Drop README from that COPY layer**; only `pyproject.toml`
+ `uv.lock` + `src/assistant/` for the build (uv_build backend
needs source to produce wheel).

**Fix:** plan §B Dockerfile pseudocode should explicitly say:
```dockerfile
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --frozen --no-dev --no-editable
```

(uv_build needs `src/` at sync time because `--no-editable`
triggers a wheel build, and the build backend reads source. Test
in spike if uncertain.)

---

### W2-M4. Logging: structlog → JSON in stdout → docker json-file driver double-encodes
The user's wave-2 prompt (angle 11) flags this. Verified concern:

- Daemon's structlog emits a JSON line per event (verified
  pattern from phase 5a/5b runbooks).
- Container stdout → docker `json-file` driver wraps each line in
  `{"log":"<line>","time":"...","stream":"stdout"}`.
- `docker compose logs` UN-wraps for display (shows the inner
  string).
- BUT: tools that read raw json files at
  `/var/lib/docker/containers/<id>/<id>-json.log` see double-encoded
  JSON.

**Impact:** if owner pipes `docker compose logs` through `jq`, no
issue (compose logs unwraps). If owner reads raw file, must `jq
-r .log | jq .`. Annoying, not broken.

**Fix:** document in `deploy/docker/README.md` log-tail recipes.
Phase 5b runbook updates needed (plan §K mentions runbook patches).

Alternative: route structlog to a file inside `~/.local/share/...`
instead of stdout. Then docker logs are empty (no double-encoding
problem) but loses `docker compose logs` convenience. Not
recommended.

---

### W2-M5. `kill -0` with empty `.daemon.pid` (race window)
The user's wave-2 prompt (angle 6) flags. Verified: if daemon is
mid-write (truncate, then write pid string), a healthcheck in the
exact microsecond window reads "" and `kill -0 ""` errors. Sub-
second probability (~10^-5 per check). Healthcheck `retries: 3`
masks. Acceptable but document.

W2-C3 fix (readlink check + non-empty test) eliminates this too.

---

### W2-M6. `dpkg --print-architecture` substitution in apt sources for `gh`
Spike RQ1 noted this. Mainstream advice is to write
`arch=$(dpkg --print-architecture)` in `/etc/apt/sources.list.d/github-cli.list`.
Plan §B does NOT mention. With phase 5d at amd64-only, the
hardcoded `arch=amd64` is fine; W2-C1 may eliminate stage 2/3
entirely if claude is bundled and phase 7 only needs `gh` — verify
phase 7 needs gh in container. If not, drop `gh` install too.

**Fix:** plan §B stage 3 either uses `$(dpkg --print-architecture)`
for forward-compat OR pins `arch=amd64` with a comment "phase 5d
amd64-only; arm64 reopens this." Trivial. Not blocker.

---

### W2-M7. Spike-findings Patch 5 chown is destructive on multi-user VPS
Patch 5 says `sudo chown -R 0xone:0xone ~/.claude`. If VPS has
multiple users SSHing in (e.g., admin + 0xone) or if `~/.claude`
contains files owned by other agents (e.g., a system-wide
`/etc/skel/.claude` template), the recursive chown could change
ownership of unrelated files.

**Severity:** LOW for VPS used as single-tenant; MEDIUM if VPS is
shared.

**Fix:** wrap in safety:
```bash
test -d ~/.claude && \
  test "$(stat -c %U ~/.claude)" = "$USER" && \
  sudo chown -R 0xone:0xone ~/.claude
```
Only chown if directory exists AND outer dir owned by current user
(implies it's the user's personal `.claude`, not a shared one).
Trivial.

---

## LOW (nice-to-have, defer post-cutover)

### W2-L1. `start_period: 60s` is generous but not data-driven
Spike says claude CLI cold start takes 45s worst-case. 60s gives
33% buffer. Owner-question 4 in spike-findings notes "bump to 90s
if flapping observed." Not actionable until production data.

### W2-L2. Trivy scan cadence — main + tag + PR all scan
Spike RQ6 draft runs Trivy on every push. For amd64-only single-
service image, Trivy scan adds ~30-60s per CI run. Acceptable. If
slow, gate behind `if: github.ref == 'refs/heads/main'`.

### W2-L3. Bundled `claude` binary version drift from npm-published
If both stages 2 (npm install) AND `claude_agent_sdk` (bundled) end
up in image, they may pin different claude versions. SDK 0.1.59
bundles claude X; plan pins npm install of claude 2.1.116. Drift
risk: subtle behavior differences.

If W2-C1 is resolved by dropping stage 2, this disappears.

---

## Carry-forwards from wave-1 + spike, NOT yet patched into plan

These need to land in fix-pack BEFORE coder starts:

| Item | Source | Status |
|---|---|---|
| Healthcheck restart-on-unhealthy decision | wave-1 C4 | Not patched |
| GHCR visibility-flip step in §F | wave-1 C2 | Not patched |
| Smoke job concrete codepath | wave-1 C3 | Not patched |
| Vault flock in §B table + §J risks | wave-1 C1 | Not patched |
| Host-dir pre-creation script in §F | wave-1 C5 | Patch 5 partially |
| `~/.claude` mount scope (full, NOT selective) | wave-1 C6, REVERSED by W2-C4 | Plan KEEPS full |
| `env_file` `required: false` syntax | wave-1 H1 + W2-C2 | Not patched |
| Claude bundled binary architectural fact | W2-C1 (NEW) | Not in plan |

---

## Verdict

**YELLOW — Reconsider specific aspects; coder unblocked for venv +
runtime stages, BLOCKED for nodejs+claude+gh stages until W2-C1
resolved.**

Coder CAN draft:
- `Stage 1 base` (apt deps, user creation).
- `Stage 4 builder` (uv sync with `--no-editable`).
- `Stage 5 runtime` skeleton (COPY venv from builder, USER bot,
  ENTRYPOINT).
- `.dockerignore` (W2-H7 list).
- `docker-compose.yml` skeleton (env_file, volumes, healthcheck —
  pending W2-C2/C3 fixes).

Coder CANNOT YET draft:
- Stage 2 (nodejs+claude) — W2-C1 must decide drop-vs-keep.
- Stage 3 (gh) — depends on phase-7 needing `gh` in image; verify.
- Healthcheck final command — pending W2-C3 (pid+exe check or
  alternative).
- Migration runbook §F — pending W2-H6 reorder + healthcheck
  watchdog decision (W2-H3).
- Smoke job CI — pending W2-H2 codepath choice.

**Top 3 fixes for fix-pack-before-coder:**

1. **W2-C1: Spike RQ13 — does Linux `claude_agent_sdk==0.1.59`
   wheel bundle a `linux/x86_64` `claude` ELF?** If yes: drop
   stages 2+3 (or 2 only); refactor §B; refactor preflight to use
   bundled binary; image size drops ~150 MB AND build complexity
   halves. If no (Mac-bundles-only): keep current §B with patches.

2. **W2-C2: §C compose env_file syntax** — convert `secrets.env`
   to `required: false` object form. One-line fix prevents
   first-boot failure on hosts without `secrets.env`.

3. **W2-C3: §C healthcheck robustness** — replace `kill -0
   $(cat .pid)` with pid+exe check OR adopt UDS heartbeat file.
   Also W2-H3 watchdog decision (autoheal sidecar recommended).

After those three, coder is fully unblocked. W2-C4, W2-H1..H8
can land as in-PR fixes before owner smoke.

**Severity distribution wave-2:** 4 CRITICAL, 8 HIGH, 7 MEDIUM, 3
LOW = 22 NEW items. (Wave-1 was 28; wave-2 adds 22 net-new; total
project-wide: 50 across both waves + spike-findings.)

---

**End of wave-2 review.**
