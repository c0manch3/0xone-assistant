# Phase 5d — Code Review (Docker migration)

**Reviewer:** code-reviewer
**Date:** 2026-04-25
**Scope:** Docker infra only (Dockerfile + compose + CI workflow + README + dockerignore + ancillary doc edits). No `src/` changes.
**Verdict:** YELLOW — ready-for-CI **CONDITIONAL** on fixing 1 CRITICAL and 2 HIGH items. Critical-1 will break the `test` job at first CI run; high-1/2 are reliability holes that surface only post-deploy.

---

## Executive summary

Implementation is, on balance, very high quality. Wave-1/wave-2 devil's advocate findings have been substantively addressed in the right places (W2-C1 dropped stage 2, W2-C2 env_file object form present, W2-C3 healthcheck pid+exe readlink, W2-C4 full `~/.claude` mount, W2-H6 backup-before-install ordering, W2-H7 `.dockerignore` enumeration). The Dockerfile is stage-clean, comments are dense and load-bearing, build-time sanity asserts (`test -d`, `test -x`, `claude --version`) catch the right regression classes loudly. Compose YAML uses every relevant compose v2.20+ feature correctly. CI workflow has explicit permissions, GHA cache scoping (W2-H5), digest-pull smoke (W2-H2 Option B), and split Trivy passes. README is owner-grade.

What remains: a **dockerignore-vs-test-target conflict** (CRITICAL), a **Dockerfile/compose mismatch around tini** (HIGH — Dockerfile uses tini but compose stop semantics docs claim systemd 30s match, also potential PID concerns), and an **autoheal sidecar restart-policy gap** (HIGH — `restart: unless-stopped` on autoheal itself but autoheal-side container failure uncaught).

Counted items: **1 CRITICAL, 3 HIGH, 6 MEDIUM, 4 LOW + 5 commendations.**

---

## CRITICAL (must fix before merge)

### CR-1. `.dockerignore` excludes `tests/` from build context — `test` target's `COPY tests/ ./tests/` will fail
**Files:**
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/.dockerignore:60`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile:200`

**Problem.** `.dockerignore:60` lists `tests/` as excluded with a comment "test target re-COPYs tests/ explicitly via its own COPY directives." This is false. `.dockerignore` filters the build context BEFORE any Dockerfile `COPY` runs. Once excluded, no COPY can resurrect those files. `Dockerfile:200` (`COPY tests/ ./tests/` in the `test` target) will either fail with "no source files were found" (BuildKit) or silently produce an empty `/app/tests/` (legacy builder).

**Why it's a problem.** The CI `test` job (`docker.yml:79-101`) builds `--target test` then runs `docker run --rm 0xone-test:ci` which executes `pytest -q tests/`. With an empty `tests/` directory, pytest exits 5 ("no tests collected") — not zero — failing the CI test job on the first run.

**Fix.** Two options:

Option A (recommended) — remove `tests/` from `.dockerignore`. The `tests/` directory is small (~1-2 MB) and including it in the build context is harmless. The runtime stage doesn't COPY `tests/`, so the runtime image stays lean.

```diff
--- a/deploy/docker/.dockerignore
+++ b/deploy/docker/.dockerignore
@@ -55,7 +55,3 @@
 # Phase-irrelevant
 node_modules/
-
-# Test suite excluded from the runtime build context. The Dockerfile's
-# `test` target re-COPYs tests/ explicitly via its own COPY directives.
-tests/
```

Option B — use a separate Dockerfile for the test target (dual-Dockerfile pattern). More complex; not recommended for phase 5d.

**Severity rationale.** Hard CI break on first `docker.yml` run. Caught by integration but turns the green-on-first-push expectation red.

---

## HIGH (fix before owner cutover)

### HI-1. `tini` as PID 1 invalidates `.daemon.pid` content for the healthcheck — but compose healthcheck reads it anyway
**Files:**
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile:181`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/docker-compose.yml:70-82`
- `/Users/agent2/Documents/0xone-assistant/src/assistant/main.py` (singleton-lock path; not modified, but consumed)

**Problem.** Dockerfile sets `ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-m", "assistant"]`. Inside the container, `tini` is PID 1 and the python interpreter is PID 7 (or whatever the next PID is). The daemon's singleton lock writes the python interpreter's container-namespace PID into `.daemon.pid`. The compose healthcheck `readlink /proc/$$pid/exe` reads that PID and resolves it via `/proc/<pid>/exe`. This is **correct** in steady state.

The hidden risk is during the start window: if `_preflight_claude_auth` (`main.py:107-154`) hangs near the 45 s timeout, the `start_period: 60s` window narrows to ~15 s of healthcheck retries. Combined with `interval: 30s + retries: 3`, in the worst case the first failing healthcheck fires at `start_period+interval = 90 s`, the second at 120 s, the third at 150 s — autoheal restarts at 150 s. That is fine; the concern is documentation, not correctness.

A second, sharper concern: the README at `README.md:217-221` says "the pid in `.daemon.pid` may have been written then the daemon swapped python interpreters (rare)". The daemon does **not** swap interpreters; the line is misleading. The actual edge case is the W2-M5 race (mid-write empty pid file), which the healthcheck `[ -n "$$pid" ]` already guards against.

**Why it's a problem.** Misleading docs in a runbook surface; owner debugging an unhealthy container in 6 months will chase phantom interpreter swaps.

**Fix.**
1. Edit `README.md:295` row to drop the "swapped python interpreters" phrasing; replace with "stale pid (very rare; happens only if the healthcheck fires inside the truncate-then-write window of `_acquire_singleton_lock`)".
2. Add a comment near `Dockerfile:181` noting that PID 1 is `tini`, daemon PID is typically 7, and `.daemon.pid` contains the **python**, not the tini, PID — so the healthcheck readlink-grep-python check is correct.

```dockerfile
# tini is PID 1; the python interpreter is the immediate child (PID 7
# in steady state). The daemon's singleton-lock path writes its own
# container-namespace PID — i.e. the python PID, NOT 1 — into
# .daemon.pid, so the compose healthcheck's `readlink /proc/$pid/exe`
# step grep'ing for "python" resolves correctly.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-m", "assistant"]
```

---

### HI-2. autoheal sidecar self-failure is uncaught — no watchdog-of-watchdog
**Files:**
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/docker-compose.yml:88-98`

**Problem.** The `autoheal` sidecar has `restart: unless-stopped` (line 91), which restarts it on **process exit** (parity with `0xone-assistant`). However, autoheal itself has no healthcheck. If `willfarrell/autoheal` enters a zombie state (rare but documented in the upstream issue tracker: shell-loop hang on a stuck `docker.sock` poll), it stays "Up" but stops monitoring. The bot becomes unhealthy → autoheal does nothing → owner sees nothing in Telegram (by design — bot is wedged).

**Why it's a problem.** Phase 5d's restart-on-unhealthy story is a 2-tier system (bot → autoheal). When tier 2 fails silently, tier 1 has no recovery. This is the exact "two-watchdog" problem phase 9 is supposed to address with rootful-systemd-timer on the host, but phase 5d ships without acknowledging the residual risk.

**Fix.**
1. Add a comment in the compose file's autoheal block explicitly listing this as a residual risk, deferring full mitigation to phase 9 (host-systemd-watchdog or external uptime monitoring like UptimeRobot pinging a `/health` endpoint that phase 9 introduces).
2. (Optional) add `healthcheck:` to the autoheal service. The image doesn't ship one, but `pgrep -f autoheal.sh` would catch a bash crash.

```yaml
autoheal:
  image: willfarrell/autoheal:latest
  # ... existing config ...
  # NOTE: autoheal has no healthcheck; if autoheal itself zombies,
  # nothing else watches the bot. Phase 9 adds host-systemd-timer
  # as tier-2 watchdog. Owner: monitor `docker compose ps` weekly
  # for autoheal status drift until phase 9 ships.
  healthcheck:
    test: ["CMD-SHELL", "pgrep -f autoheal || exit 1"]
    interval: 60s
    timeout: 5s
    retries: 3
```

---

### HI-3. `actions/checkout@v4` missing from the `smoke` job — but that job pulls by digest only
**Files:**
- `/Users/agent2/Documents/0xone-assistant/.github/workflows/docker.yml:103-149`

**Problem.** The `smoke` job (lines 103-149) does NOT use `actions/checkout@v4`. That's actually fine — it pulls the image by digest from GHCR and runs imports inline. No repo source needed. But the `scan` job (lines 151-187) uses `actions/checkout@v4` (line 158), and `scan` also doesn't read repo files (Trivy operates on image-ref). The asymmetry is harmless but confusing; `scan` doesn't actually need checkout either.

**Why it's a problem.** Minor code-smell: an unused action call costs ~3 s of CI time per run. If a future maintainer notices and deletes `actions/checkout@v4` from `scan`, they may also delete it from a hypothetical future `actions/upload-artifact` step that does need the checkout — silent break.

**Fix.** Drop `actions/checkout@v4` from `scan` (line 158) — it's dead code. Add a one-line comment explaining why `smoke` and `scan` don't need checkout.

```diff
 scan:
   needs: build-and-push
   if: github.event_name != 'pull_request'
   runs-on: ubuntu-24.04
   env:
     IMAGE_REF: ${{ env.IMAGE }}@${{ needs.build-and-push.outputs.digest }}
   steps:
-    - uses: actions/checkout@v4
+    # No actions/checkout — Trivy scans the registry image by ref;
+    # no repo files consumed by this job.
     - uses: docker/login-action@v3
```

---

## MEDIUM (tidy-up; not deployment blockers)

### MD-1. `.dockerignore` missing repo-root files
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/.dockerignore`

**Problem.** Repo root has `daemon/` (empty), `tools/` (contains `ping`), `skills/`, `justfile`, `README.md`, `CLAUDE.md`. None are needed at build time (builder COPYs only `pyproject.toml`, `uv.lock`, `src/`). They're not large, but `.dockerignore` is the canonical exclusion surface, and W2-H7 explicitly listed comprehensive enumeration as the goal.

**Fix.** Add to `.dockerignore`:
```
# Repo-root artifacts not consumed by any stage's COPY directive
daemon/
tools/
skills/
justfile
README.md
CLAUDE.md
.python-version
```

`README.md` is excluded because `pyproject.toml` does NOT declare `[project] readme =` (verified line 1-30 of pyproject.toml), so the build doesn't need it. If a future phase adds the readme reference, restore inclusion.

---

### MD-2. `--mount=type=cache` for apt not used — multi-stage rebuild slower than necessary
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile:35-41,59-68,130-135`

**Problem.** Three `apt-get install` blocks (base, ghcli, runtime). Each currently runs `apt-get update` + install + `rm -rf /var/lib/apt/lists/*`. Without BuildKit cache mounts, the apt index is re-downloaded every cache miss.

**Why it's a problem (mild).** CI `actions/setup-buildx-action@v3` enables BuildKit; cache mounts can be transparently used. Saves ~10-15 s per cache miss. Not blocking; phase 5d's GHA cache scoping (H5) already mitigates most rebuilds.

**Fix (optional).** Convert to:
```dockerfile
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git sudo
```
And drop `rm -rf /var/lib/apt/lists/*` (BuildKit handles cache scope). Apply to all three apt install blocks. Defer to phase 9 if owner wants minimal change today.

---

### MD-3. Compose healthcheck `start_period: 60s` matches preflight 45s + 15s — but documentation in README claims "45s + slack"
**Files:**
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/docker-compose.yml:81`
- `/Users/agent2/Documents/0xone-assistant/deploy/docker/README.md:223-224`

**Problem.** README says "covers worst-case claude preflight (45s timeout + slack — RQ12)." 60 s is 33% headroom over 45 s. Not "slack" — it's a generous margin. Wording inconsistency only.

**Fix.** README line 223 → "covers worst-case claude preflight (45 s timeout + 15 s margin)". Trivial.

---

### MD-4. CI Trivy uses `aquasecurity/trivy-action@master` — unpinned
**File:** `/Users/agent2/Documents/0xone-assistant/.github/workflows/docker.yml:170,180`

**Problem.** `aquasecurity/trivy-action@master` (lines 170, 180) is the only action in the workflow not pinned to a major version. Master can change without notice — supply-chain risk and potential silent behavior drift. Plan §J risk 8 says "currently major-version pinned"; this one slipped.

**Fix.** Pin to a major or minor version. As of 2026-04-25, latest major-equivalent is `0.28.x`. Use `@0.28.0` or the rolling major tag if upstream publishes one.

```diff
-        uses: aquasecurity/trivy-action@master
+        uses: aquasecurity/trivy-action@0.28.0
```

Apply to both Trivy steps (OS-pkgs + library).

---

### MD-5. README "GH_TOKEN rotation" tells owner to use `docker compose up -d` — but doesn't mention `--no-recreate-other` risk
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/README.md:194-202`

**Problem.** `docker compose up -d` (line 200) recreates ANY service whose config-hash changed since last up. If the owner edits `secrets.env`, only the `0xone-assistant` service should recreate. But if compose detects `autoheal` config drift (e.g., image tag movement on `willfarrell/autoheal:latest`), it ALSO recreates autoheal. Brief watchdog gap. Low risk; just observable.

**Fix.** Append to the rotation section:
```markdown
> **Note:** `compose up -d` may also recreate `autoheal` if the upstream
> `willfarrell/autoheal:latest` image moved. Brief (1-3 s) watchdog gap.
> Pin autoheal to a digest in phase 9.
```

---

### MD-6. `runbook.md` "Reload / restart recipe" mixes Docker and systemd contexts without prominent fork
**File:** `/Users/agent2/Documents/0xone-assistant/plan/phase5/runbook.md:53-100`

**Problem.** Section 3 starts with "## 3. Process manager (Linux / VPS)", then has subsection "### Docker compose (phase 5d, primary)" and "### systemd unit (phase 5a fallback, retained)". A new owner reading top-down may execute commands from both subsections by accident. The Docker subsection ends with a code block; the systemd subsection starts immediately after. Visually noisy.

**Fix (low priority).** Add a callout box at the top of section 3:
```markdown
> **Which subsection applies?** Run `docker compose ps -a` first.
> If `0xone-assistant` shows `Up`, you're on Docker — skip the systemd
> subsection. If `systemctl --user is-active 0xone-assistant.service`
> is active, you're on the fallback path — skip Docker.
```

---

## LOW (nice-to-have)

### LO-1. Dockerfile `ARG PYTHON_BASE=...` declared at top, used after each `FROM`, but stages don't redeclare `ARG`
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile:23,28,114`

Per BuildKit semantics, an `ARG` declared before the first `FROM` is global, BUT to be referenced INSIDE a stage (e.g., in another `FROM ${PYTHON_BASE}`) it doesn't need redeclaration. ✅ The current Dockerfile correctly leverages this. The plan §J risk 4 reminds future phases to remeasure size if a base bumps; the Dockerfile makes that easy via the single `ARG`.

No fix needed; commendation in disguise. Documenting here so future maintainers don't "fix" this by adding redundant `ARG` lines per stage.

---

### LO-2. `.env.example` only contains `TAG=` — does not document `TELEGRAM_BOT_TOKEN` etc.
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/.env.example`

**Problem.** This `.env` is the COMPOSE-level env, not the daemon-runtime env. The daemon's runtime env lives in `~/.config/0xone-assistant/.env` (read via `env_file:` in compose). Owner reading `.env.example` may confuse the two.

**Fix.** Add a comment:
```diff
+# Compose-level env. NOT the daemon's runtime env (which lives in
+# ~/.config/0xone-assistant/.env and is loaded via compose env_file).
+#
 # Copy this file to deploy/docker/.env and edit. ...
```

---

### LO-3. `Dockerfile:198` test target COPYs `pyproject.toml uv.lock ./` — same paths already exist from runtime stage's `WORKDIR /app`
**File:** `/Users/agent2/Documents/0xone-assistant/deploy/docker/Dockerfile:198-200`

**Problem.** The runtime stage sets `WORKDIR /app` but doesn't COPY `pyproject.toml` or `uv.lock`. The test target then COPYs them. OK semantically. But `COPY src/ ./src/` (line 199) overlays the runtime image — which doesn't have `/app/src` since the runtime relies on the venv's site-packages — so this is fine for the test stage.

No bug; cosmetic note. The COPYs are necessary for `uv sync --frozen` (it reads pyproject + lock).

---

### LO-4. CI build uses `target: runtime` (explicit) but test job omits target arg — relies on `target: test`
**File:** `/Users/agent2/Documents/0xone-assistant/.github/workflows/docker.yml:65,93`

Build-and-push uses `target: runtime` (line 65); test uses `target: test` (line 93). Both explicit ✅.

No fix; commendation.

---

## Commendations

1. **Dockerfile build-time sanity asserts (`Dockerfile:108-109,172`).** The `test -d /opt/venv/.../assistant` and `test -x /opt/venv/.../claude` and `claude --version` checks catch RQ8/RQ13 regression classes loudly at build time. Excellent fail-fast design.

2. **Compose healthcheck `[ -n "$$pid" ] && [ -L /proc/$$pid/exe ] && readlink ... | grep -q python` (W2-C3).** Three independent checks chained so any one failure trips the gate. The grep-q-python (vs strict equality) is a smart forward-compat choice.

3. **CI permissions block (`docker.yml:20-23`).** Explicit `contents: read`, `packages: write`, `actions: write`. No surprises. H4 properly addressed.

4. **Symlink + `claude --version` smoke at build time (`Dockerfile:166-172`).** The build will fail loudly if Anthropic ever drops `_bundled/claude` from a future SDK patch — exactly the right place to catch it (RQ13 caveat 4).

5. **README §"`.daemon.pid` is container-namespaced" (`README.md:250-265`).** Rare ops doc that explains a non-obvious gotcha BEFORE the owner hits it. W2-H8 properly addressed.

---

## Metrics

- **Files reviewed:** 7 (Dockerfile, compose, dockerignore, env.example, README, docker.yml workflow, plus .gitignore + CLAUDE.md + 2 runbooks).
- **LOC scanned:** Dockerfile 211, compose 98, dockerignore 60, README 343, workflow 187, .env.example 10. Plus ~150 LOC of plan-context-aware doc edits.
- **Issues found:** 1 CRITICAL, 3 HIGH, 6 MEDIUM, 4 LOW = **14 actionable**.
- **Wave-1/wave-2 items already addressed (NOT re-flagged):** W2-C1 (stage 2 dropped), W2-C2 (env_file object form), W2-C3 (healthcheck pid+exe), W2-C4 (full `~/.claude` mount), W2-H2 (smoke imports-only), W2-H3 (autoheal sidecar), W2-H4 (no-sudo-claude doc rule), W2-H5 (GHA cache scoping), W2-H6 (backup-before-install order), W2-H7 (.dockerignore enumeration partial — see MD-1), W2-H8 (PID namespace doc), W2-M1 (`CLAUDE_CODE_DISABLE_AUTOUPDATER` env), W2-M2 (env_file standardized object form), W2-M3 (no README in builder COPY), W2-M5 (empty-pid-file race covered by `[ -n "$$pid" ]`), W2-M7 (chown safety wrap), W1 backup ordering, W1 GHCR public-flip docs.
- **Plan §J risks status:** 14/20 fully mitigated by code; 5/20 deferred to phase 9 (read_only, cap_drop, SBOM/Cosign, rootless, GHCR retention); 1/20 left as documented gotcha (host-side `kill` confusion — mitigated by README §`.daemon.pid` is container-namespaced).
- **Ready for CI:** YES, AFTER fixing CR-1 (`tests/` exclusion). HI-1/2/3 can land as in-PR follow-ups before owner cutover.

---

## Top-3 risks the coder accepted but should re-validate post-deploy

1. **Bundled-binary version drift.** Dockerfile pins SDK via `pyproject.toml >=0.1.59,<0.2`. A future 0.1.x patch that strips `_bundled/claude` will break the build (intentionally — `claude --version` step at line 172 fails). Make sure `pyproject.toml` is bumped intentionally, not auto.

2. **GHA cache 10 GB ceiling.** `cache-to: type=gha,mode=max,scope=<branch>-amd64` (workflow line 77) writes the full layer set per branch. With many feature branches, eviction begins. Phase 9 should add a periodic cache-prune workflow.

3. **`willfarrell/autoheal:latest` is unpinned.** A breaking image update silently lands on next `compose up -d`. Recommend pinning to a digest in phase 9 (or sooner if a regression is observed).

---

**End of review.**
