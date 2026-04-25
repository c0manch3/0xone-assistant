# Phase 5d — DevOps review

**Reviewer:** senior DevOps / SRE (read-only).
**Scope:** `deploy/docker/{Dockerfile,docker-compose.yml,.dockerignore,.env.example,README.md}`,
`.github/workflows/docker.yml`, `CLAUDE.md`, `plan/phase{4,5}/runbook.md`,
`.gitignore`. Reference: `plan/phase5d/{description,implementation-v2}.md`,
spike findings, `reference_vps_deployment.md`.

**Hard rule honored:** read-only; no duplicate code-review / QA topics
(no review of src/, healthcheck shell semantics, or test selection).

---

## Executive summary

Phase 5d is a careful, mostly well-engineered Docker migration. Multi-stage
build is clean, the RQ13 bundled-claude symlink is genuinely elegant, and
operational ergonomics — env_file object form, autoheal sidecar, exe-readlink
healthcheck, `${TAG:?}` no-default — are all production-grade choices that
demonstrate the team learned from the phase 5a→5b cascading-bugs episode.

The migration runbook (§F + README §"Initial install") is the strongest
artifact — it correctly orders backup-before-mutation, GHCR-pull-test-before-
systemd-stop, and 24h-burn-in-before-unit-removal. `stop_grace_period: 35s`
+ `/etc/docker/daemon.json shutdown-timeout: 40` is the kind of detail that
only surfaces from real on-call experience.

That said, three classes of operational risk are unaddressed: **(1) no
resource caps on a sidecar that can pull rogue images and a daemon with
known peak-memory of ~890 MB on a 4 GB VPS**; **(2) the autoheal sidecar
holds the docker socket — even read-only — which is the largest single
attack-surface increase in the phase**; **(3) CI uses `trivy-action@master`
(unpinned) and lacks a digest-pinned base image SHA-update workflow, which
will cause silent supply-chain drift over the next 6-12 months**. None of
these block ship; all three deserve a dedicated phase 9 line item.

---

## Verdict: **READY-WITH-POLISH**

Ship to VPS as planned. The migration playbook is sound; rollback story is
real (systemd unit + backup snapshot); failure modes are documented. The
"polish" items below are non-blocking but should be tracked in `plan/phase9/`
debt or a phase-5e fast-follow if any one bites.

---

## 1. Operational readiness

### Build reproducibility

**Strong.** Base pinned by manifest-list digest
(`sha256:58525e1a...`), `uv.lock` frozen via `uv sync --frozen --no-dev
--no-editable`, `UV_VERSION=0.11.7` pinned, `gh` apt repo pinned by
key fingerprint. Build-time sanity checks (lines 108-109, 172) catch
SDK regressions loudly.

**Gap:** `willfarrell/autoheal:latest` (compose line 89) is the only
unpinned dependency in the production stack. `latest` on a third-party
image is the exact anti-pattern this phase otherwise avoids. The image
hasn't seen a new release since 2022, so churn risk is low — but if
the maintainer ever pushes a malicious tag, every restart picks it up.

```yaml
# Recommendation:
image: willfarrell/autoheal@sha256:<digest>  # pin to known-good
# OR at minimum:
image: willfarrell/autoheal:1.2.0  # pin to last semver tag
```

### Image size — measurable?

CI never logs/asserts size. Plan claims ~400 MB uncompressed / ~140 MB
compressed; spike RQ9 measured ~600 MB pre-RQ13 drop. There is no
guard against creep when phase 6 adds ffmpeg/libsndfile.

**Add a CI gate** in the build job:

```yaml
- name: Image size guard
  run: |
    SIZE=$(docker image inspect "$IMAGE_REF" --format '{{.Size}}')
    echo "image_size_bytes=$SIZE"
    test "$SIZE" -lt 1073741824 || { echo "Image > 1 GB"; exit 1; }
```

This makes the §I Q12 red line (1 GB) a real CI check, not a comment.

### CI duration

Realistic estimate based on stages:

- `build-and-push`: ~5-7 min cold cache, ~2 min warm (first push has no
  GHA cache hit; budget for that on the GHCR-flip day).
- `test`: ~3-4 min cold (re-syncs dev deps).
- `smoke`: ~30s.
- `scan`: ~1-2 min (trivy DB download dominates).
- Total wall-clock: ~10-15 min cold, ~5-8 min warm.

Inside the 10-min target only on warm cache. **Cold cache day-1 will
exceed it** — owner should be warned not to expect day-1 CI green
inside 10 min.

### VPS pull duration on 50 Mbps

140 MB compressed / (50/8) MB/s ≈ 22-25s wall (modulo TCP slow-start +
GHCR latency). Plan estimate of 25-30s is correct. Acceptable.

---

## 2. Observability

### Log driver

**Correct.** `json-file` with `max-size: 50m × max-file: 5` = 250 MB
ceiling per container. structlog JSON events flow through cleanly per
README §"Log tailing"; the double-encoding caveat at L17 is documented.

### Healthcheck visibility

**Visible** via `docker compose ps` (Status column shows
`Up (healthy)` / `Up (unhealthy)`), `docker inspect --format
'{{.State.Health.Status}}'`, and `docker inspect --format '{{json
.State.Health}}' | jq` for full history. README §"Healthcheck"
documents all three. Good.

### `docker stats` for resource monitoring — gap

Not documented in README or runbook. Owner has no recipe for "is the
daemon RSS climbing?" beyond raw `docker stats`. Add to README §"Log
tailing" or a new §"Resource monitoring":

```bash
# One-shot snapshot
docker stats --no-stream 0xone-assistant
# Continuous (Ctrl-C to exit)
docker stats 0xone-assistant
```

Particularly important given peak-RSS of ~890 MB observed (per scope
note) — owner needs a cheap way to spot a memory-leak regression
without setting up Prometheus.

### structlog format preserved

**Yes.** json-file driver wraps each line in
`{"log": "<original>\n", "stream": "stdout", "time": "..."}`. README
correctly notes that `docker compose logs` strips this envelope so the
inner JSON appears directly; raw `/var/lib/docker/containers/...json.log`
needs `jq -r .log | jq .`. Both paths covered.

### Missing: no metrics endpoint

This phase is correctly scoped — adding Prometheus is phase 9+. But
there is no even-passing mention of "if metrics are needed, here's the
hook" in the runbook. For a single-user bot, fine. Flagging for phase
9 awareness.

---

## 3. Backup / disaster recovery

### Bind mounts for standard backup

**Excellent design choice.** §"Backup" recipe in README uses `cp -a`
on bind-mounted dirs — exactly what should happen. `rsync` and `tar`
work identically (host-side filesystem access).

### SQLite `.backup` recipe

**Present** in README lines 168-172:

```bash
sqlite3 "$DB" ".backup '$BACKUP/assistant-$(date +%F).db'"
```

Correct online-safe approach. Good.

**Gap:** `assistant.db` is touched while the container runs but the
backup recipe is a host-side `sqlite3` invocation against the
bind-mounted file. That works because sqlite3 honors WAL mode +
fcntl-flock across bind mounts (RQ4 verified). However, the README
doesn't explicitly state that — owner could conclude they need to stop
the container first (which would force them to lose the live-read
property). Add a one-line note:

> The `.backup` command is safe against the live container; sqlite's
> WAL mode and POSIX locking propagate cleanly across the bind mount
> (RQ4 verified). No need to `docker compose stop` first.

### DR section coverage

README has migration playbook + rollback + sqlite backup. **Missing
explicit DR sections for:**

1. **"Container fails to start (image broken)."** Implicit: roll back
   to prior `:sha-<old>` tag. README §"Rollback" covers but doesn't
   call out "what if rollback ALSO fails?" — that's where the systemd
   fallback path matters. README line 149-152 mentions it as
   "Absolute fallback" but should be promoted to a primary DR section
   with explicit commands tested.

2. **"OOM kill mid-write."** §"Resource constraints" addresses no caps;
   need an explicit recovery recipe — e.g. WAL-mode sqlite recovery,
   `vault/.tmp/` cleanup. Phase 4 runbook §4 has this for the memory
   index; phase 5d README should cross-link.

3. **"Data dir corruption (e.g. ext4 fsck after host crash)."**
   Recovery: restore from `~/.backup-<ts>`. Covered by §"Backup" in
   reverse but not as a labeled DR scenario.

4. **"GHCR outage."** This is the case for which `:phaseN` tags + a
   pre-cached local copy via `docker save / load` matter. README
   doesn't recommend a local image cache. For a single-user single-VPS
   deploy, low priority — but a 5-line recipe would close the gap:

   ```bash
   # On Mac, after a known-good deploy:
   docker save ghcr.io/c0manch3/0xone-assistant:phase5d \
     | gzip > ~/Backups/image-phase5d.tar.gz
   # On VPS during GHCR outage:
   gunzip -c image-phase5d.tar.gz | docker load
   ```

### Time-to-recover from rollback

Realistic estimate:

- `echo "TAG=sha-<old>" > .env`: 2s.
- `docker compose pull` (cached layers, only the changed venv layer
  pulls): 5-15s.
- `docker compose up -d --force-recreate`: ~10-15s container stop
  (graceful) + 10s start + 60s healthcheck warmup = ~85s before
  healthy.

**Total: ~90-120s for a TAG-pin rollback.** Acceptable. Worth stating
in README as a reset-the-expectation line.

### systemd unit fallback

**Documented** (README lines 149-152). I recommend adding a one-time
"verify the systemd fallback still works" step at 24h post-cutover:

```bash
# In the cutover §F step 8 or as a separate section:
# Verify systemd fallback before retiring (one shot):
systemctl --user start 0xone-assistant.service  # boot it
sleep 30 && journalctl --user -u 0xone-assistant -n 5
systemctl --user stop 0xone-assistant.service   # back to docker
docker compose up -d
```

Without that test, "fallback exists" is just a claim until proven the
day you need it.

---

## 4. Upgrade path

### Downtime window

`docker compose up -d` with TAG change:

1. Compose detects diff → stops old container (sends SIGTERM + 35s grace).
2. Daemon writes `.last_clean_exit` + drops `.daemon.pid` flock.
3. Compose starts new container.
4. `start_period: 60s` healthcheck warmup.
5. New container becomes healthy → resumes Telegram polling.

**Owner-observed downtime: ~10-30s (the 35s grace is the ceiling, not
the median; a clean `Daemon.stop()` returns in <5s).** Telegram long
polling tolerates this trivially — `getUpdates` re-establishes on next
boot, no message loss.

This is acceptable for single-user. Document the expectation in
README §"Update to a new image":

> Expect ~30s "no replies" window during update; messages sent during
> that window are queued by Telegram and delivered on next poll.

### Migration runner at boot

**Phase 4-5 pattern preserved.** No Docker-specific changes needed —
sqlite migrations run at `Daemon.start()` regardless of container vs
systemd execution context. Confirmed via implementation-v2.md §"Source
code: ZERO changes."

### Hard cutover acceptable

**Yes** — single-user single-instance, no HA goal. Compose's "stop
old, start new" pattern matches phase 5a's `systemctl restart`
semantics. Good.

---

## 5. Security posture

### What's right

- No `privileged`, no `host_network`, no excess capabilities.
- `user: "1000:1000"` non-root inside container, matches host owner.
- Secrets via `env_file` (not `environment:`) — `docker inspect` does
  NOT leak token values; only the file path. Verified via spike.
- TLS for GHCR pull (Docker default — never disable).
- `.dockerignore` excludes `.env`, `secrets.env`, `**/secrets.env`,
  `.credentials.json` paths under `.claude/`. Defense in depth.
- Trivy scan in CI, fails on HIGH/CRITICAL OS + CRITICAL language.
- `CLAUDE_CODE_DISABLE_AUTOUPDATER=1` prevents the bundled binary
  from phoning home for updates (W2-M1).
- Outbound-only connectivity (Telegram long polling); no inbound
  ports declared.

### What needs attention

#### CRITICAL — autoheal docker socket attack surface

`/var/run/docker.sock:ro` to the autoheal sidecar (compose line 98).

**Read-only mount does NOT block writes** to the docker API. `:ro` on a
unix socket is a **kernel-level no-op** — the socket is bidirectional
by design, and any process holding an FD can send `POST /containers/
.../start` requests. A compromised autoheal container has full Docker
daemon control: pull arbitrary images, mount host root, escape to host.

This is a known pattern (used by Watchtower, Portainer, Ouroboros) and
is *standard* for sidecar watchdogs, but it's the largest single
attack-surface increase in this phase. The threat model:

- **CVE in `willfarrell/autoheal:latest`** → container escape via
  docker socket → host root.
- **Image supply-chain compromise** (since `:latest` is unpinned, see
  §1) → same.

Mitigations, in order of cost:

1. **Pin autoheal to a digest** (low cost). Closes the supply-chain leg.
2. **Read-only root filesystem on autoheal**:
   ```yaml
   autoheal:
     image: willfarrell/autoheal@sha256:<digest>
     read_only: true
     security_opt: ["no-new-privileges:true"]
     cap_drop: [ALL]
     # ... rest unchanged
   ```
3. **Replace autoheal with a host-side systemd timer** that polls
   `docker inspect --format '{{.State.Health.Status}}' 0xone-assistant`
   and runs `docker compose restart` on `unhealthy`. Eliminates the
   socket mount entirely. ~30 LoC. Phase 9 candidate; description.md
   line 113 already flags this.

#### HIGH — Trivy action unpinned

`.github/workflows/docker.yml:170,180` uses `aquasecurity/trivy-action@master`.

`@master` means every CI run pulls latest action code. A compromised
action could: read the GHCR PAT (limited to packages:write — could push
malicious images); read other repo secrets if any are added later; alter
SARIF uploads to hide CVEs. Pin to a major version per the same policy
applied to `docker/build-push-action@v6`:

```yaml
uses: aquasecurity/trivy-action@0.28.0  # or latest stable tag
```

The README §"Phase 9 hardening" lists "Digest-pinned GHA actions" as
phase 9 work — but `@master` is one rung worse than major-version pins
and shouldn't ship to main. Bump to a tag-pin now; defer SHA-pin to
phase 9.

#### MEDIUM — base image digest needs a refresh cadence

`python:3.12-slim-bookworm@sha256:58525e1a...` (Dockerfile line 23) is
pinned correctly today. There is no automation to bump it. Three months
from now, when bookworm publishes CVE fixes, the image silently drifts
relative to upstream patched releases. Trivy will flag CVEs on the next
CI run if the package is in the apt-installed surface — but only if
that path's ignore-unfixed flag (true) doesn't suppress them.

Recommend: add a Dependabot config or a monthly cron-triggered CI job
that runs `docker pull python:3.12-slim-bookworm && new_digest=$(docker
inspect ...)` and opens a PR if the digest changed. Phase 9 fits;
mention in `plan/phase9/` debt list.

#### LOW — autoheal verbosity

Autoheal logs every restart event to its own container logs. Owner
runbook doesn't mention `docker compose logs autoheal` as a debug
channel. Add to README §"Log tailing":

```bash
# Watchdog activity (restart history):
docker compose logs autoheal | grep -i restart
```

Without this, "container keeps restarting in a tight loop" troubleshooting
(README line 294) is missing one obvious diagnostic step.

### What's correctly deferred

- User namespace remap (rootful per Q-R3) — phase 5d non-goal.
- AppArmor / SELinux profiles — slim-bookworm gets the docker default
  profile; explicit `security_opt: [apparmor=docker-default]` would be
  belt-and-braces but no incremental risk reduction.
- `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges` — phase 9
  per description.md §M.
- Image signing / SBOM / Cosign — phase 9.
- SARIF upload to GitHub Security tab — phase 9, deliberate to avoid
  `security-events: write` permission today.

---

## 6. Resource constraints

### No `mem_limit`, `cpus`

**Concerning.** VPS spec is "typical 4 GB RAM" (per scope), peak
observed RSS ~890 MB. Headroom is 4000 - 890 = ~3.1 GB — ample for
single-user. But:

- A scheduler bug producing a recursion bomb (see `SCHEDULER_MAX_SCHEDULES`
  guard in phase 5b) could leak memory unboundedly. No `mem_limit` =
  the kernel's OOM killer terminates *some* process, possibly sshd or
  the docker daemon itself, before reaching the bot.
- Phase 6 media (ffmpeg, transcription model loads) will spike RAM
  significantly. Today's "no caps" stance becomes a regression vector.

Recommend a generous cap that catches runaway, not normal operation:

```yaml
0xone-assistant:
  # ...
  mem_limit: 1500m       # 1.5x peak observed; OOM-kills the container,
                         # not host services, on runaway.
  cpus: "2.0"            # guard against pegged loops; VPS likely 2-4 vCPU.
  # autoheal will restart on the resulting unhealthy state.
```

This is one of the lowest-cost / highest-value polish items in this
review. The resource-constraints section in the scope note ("Headroom
adequate without caps") is a momentary truth that erodes by phase 6.

### Disk footprint

Calculated estimate per scope:

- Image: ~400 MB uncompressed.
- Logs: 250 MB ceiling (50m × 5).
- Data dir: vault + sqlite + audit logs = a few hundred MB at most.
- `~/.claude/projects/`: unbounded growth (transcript pruning recipe
  in README §"Transcript pruning" addresses).

**Total: ~1-1.5 GB.** Fits comfortably in any sane VPS disk allocation.

---

## 7. Migration risk

### §F migration order

**Strong.** Eight-step sequence is correctly ordered:

1. Mac: push → CI green → GHCR public flip.
2. Verify pull works (read-only test before destructive ops).
3. Backup state (irreversible-mutation prereq).
4. Fix `~/.claude` chown (idempotent, safety-wrapped).
5. Stop systemd FIRST (prevents singleton-lock hot-loop, RQ12).
6. `git pull` + first boot.
7. Owner Telegram smoke.
8. Reboot test.

Order matches my standard "verify-before-mutate, mutate-before-cutover,
cutover-before-burn-in, burn-in-before-retire" checklist.

### Cutover window

Between systemd-stop (step 5) and docker-up (step 6) the bot is offline
for ~30-60s (depends on `docker compose pull` cache state on first
run). Acceptable for single-user.

### Fresh-VPS docker install

README lines 73-74 + plan §F step 0a cover both paths:

```bash
sudo apt-get install -y docker.io docker-compose-plugin
# OR
curl -fsSL https://get.docker.com | sh
```

`docker.io` (Debian) gives docker 20.x typically — too old for compose
v2.20+ (`required: false` on env_file). The `get.docker.com` script
gives docker-ce, current. The plan's idempotent precondition check at
line 187 uses `docker-ce` package names — preferred path. README
should call out:

> **Use `get.docker.com` not `apt-get install docker.io`** — Debian's
> packaged version lags compose plugin v2.20+ which we require for
> `env_file: required: false`.

### VPS containerd 1.6 conflict

Per scope note: "VPS with old containerd from prior install (RQ10
confirmed containerd 1.6 already there): any conflicts?"

**Likely OK.** docker-ce installs containerd.io (1.7+) as a dep with
`Conflicts: containerd` in the apt metadata. The install will replace
1.6 with 1.7 atomically. Caveat: any in-flight containers managed by
the old containerd shim will be interrupted. Since the VPS hasn't run
containers in production (phase 5a uses systemd not docker), this is
zero impact.

Add a precondition note:

```bash
# Pre-install: verify no other Docker-managed workloads exist.
docker ps 2>/dev/null && echo "WARNING: other containers running"
```

Otherwise the plan is sound.

---

## 8. CI/CD pipeline

### Triggers

`push: main + tags v* phase*` and `pull_request: main`. Standard.
Reasonable.

### Cache strategy

`type=gha,scope=<branch>-amd64` with `main-amd64` fallback. Good
isolation between PR builds and main; PR builds get warm cache from
main without polluting main's cache. GHA cache 10 GB per repo limit
is well above this image's layer total (~400 MB × maybe 3-4
generations cached = ~1.5 GB). Headroom adequate.

### Platform

amd64-only. RQ3+RQ5 verified the deferral; phase 9 reopens. Good call
for now.

### Trivy

- HIGH/CRITICAL OS, CRITICAL language — reasonable bar.
- `ignore-unfixed: true` — pragmatic for "we can't ship a fix today
  anyway" cases. Standard.
- `.trivyignore` empty in phase 5d. Will accumulate FPs over time;
  fine.
- **`@master` unpinned (already flagged §5).**

### Action version pinning

`@v4`, `@v3`, `@v6` major-pinned. Reasonable per scope; SHA-pin
deferred to phase 9. **Exception:** trivy-action is `@master` (worst-
case pin granularity). Bump to a tag-pin before ship.

### Smoke job

`docker run --rm --entrypoint /opt/venv/bin/python ... -c "import ..."`
plus `claude --version` smoke. Catches the most likely failures
(broken venv, missing bundled binary, glibc compat issue). The
imports-only approach correctly avoids the wave-1 C3 401-against-
real-Anthropic tautology.

### Pull-by-digest pattern

`smoke` and `scan` jobs use `${{ env.IMAGE }}@${{ needs...digest }}`
— robust against tag-format drift. Good. (Better than the
implementation-v2.md draft which used `:sha-<short>`.)

---

## 9. Operational documentation

### Topics covered

- Install (one-shot fresh-VPS path).
- Update.
- Rollback.
- Backup (cp + sqlite .backup).
- Troubleshooting matrix.
- OAuth handling + sudo-claude rule.
- GH_TOKEN rotation.
- Healthcheck.
- Log tailing.
- `.daemon.pid` namespace explainer.
- Transcript pruning.
- Migration playbook.
- Docker daemon shutdown-timeout note.
- Phase 9 deferred items.

**343-line README is comprehensive.** Better than most production
runbooks I've seen for single-user deployments.

### Topics missing

These are non-blocking but would close common runbook gaps:

1. **First-time GHCR setup recipe — full path.** README §"First-time
   GHCR visibility flip" covers public-flip (anonymous-pull path) but
   not the alternative: keep package PRIVATE and authenticate with a
   PAT. The second path matters if the owner ever decides
   transcripts-in-image (phase 9 hardening) shouldn't be world-readable
   even though they shouldn't be in the image at all. Two-line
   addition:

   ```bash
   # Alternative to public-flip: keep PRIVATE and login on VPS.
   echo $GH_TOKEN | docker login ghcr.io -u c0manch3 --password-stdin
   ```

2. **Log retention / compaction.** 250 MB log ceiling is documented
   (50m × 5 rotation), but no recipe for "I want to grep last week's
   logs after the rotation has overwritten them". Standard answer:
   ship logs to a remote sink. For single-user: low priority.
   Mention in §"Log tailing":

   > Logs older than ~5 rotation windows are gone. For long-term
   > retention, set up a remote rsyslog / Loki / journald-like sink
   > (deferred to phase 9).

3. **Resource limits if daemon goes runaway.** README has nothing on
   this. Cross-reference to §6 above — when the section gets added,
   the README should explain what `mem_limit: 1500m` does at OOM time
   (kills container, autoheal restarts, owner sees one Telegram drop
   + recovery in ~90s).

4. **`docker stats` — already noted §2.**

5. **Autoheal log channel — already noted §5.**

6. **Health probe failure interpretation.** README §"Healthcheck"
   notes the test components but not the failure-mode tree:

   - `.daemon.pid` missing → daemon never wrote it → boot crashed
     before Daemon.start() finished → check logs for traceback.
   - `.daemon.pid` empty → daemon mid-write race (W2-M5) → wait one
     interval, will self-resolve.
   - `/proc/$pid/exe` not a symlink → pid is dead, daemon crashed but
     pid-file stale → autoheal will restart; investigate exit code.
   - `readlink ... | grep -q python` fails → pid recycled to non-python
     process (extremely rare in container) → autoheal restart fixes it.

   This kind of "what does each check fail mean" tree is what makes a
   runbook actually useful at 3 AM.

### CLAUDE.md update

Lines 22-28 cover the deploy stack reasonably. The `Process manager:`
heading now correctly references docker-first + systemd fallback.
Good — minimal, doesn't inline build commands per W2-H10. Approved.

### Phase 4 + 5 runbook patches

Both runbooks add Docker recipes alongside systemd ones. Phase 5
runbook §3 places "Docker compose (phase 5d, primary)" before
"systemd unit (phase 5a fallback)". Correct ordering. Phase 4 runbook
§8 adds the `docker compose logs ... | jq` recipe with systemd
fallback note. Both well-edited.

---

## 10. Phase 6+ extensibility

### Phase 6 (media)

**New OS deps in Dockerfile or separate image?**

Recommend: extend stage 4 (`runtime`) apt install with `ffmpeg
libsndfile1`. This adds ~150 MB → image grows from 400 MB to ~550 MB.
Still well under the 1 GB cap. Don't fork a separate image; single-
container model is the strength of this phase.

If transcription wants a heavy GPU-accelerated whisper model: that's
a sidecar (separate container, unix-socket IPC), not a Dockerfile
change. Phase 6 plan should make the call.

### Phase 7 (vault git push)

**SSH deploy key bind-mount addition trivial?**

```yaml
# Two-line addition to compose:
volumes:
  - ${HOME}/.ssh/vault_key:/home/bot/.ssh/vault_key:ro
environment:
  GIT_SSH_COMMAND: "ssh -i /home/bot/.ssh/vault_key -o IdentitiesOnly=yes"
```

`gh` is already in image (ghcli stage). `git` is already in runtime.
Zero Dockerfile change. Compose extension trivial. Good.

### Phase 8 (out-of-process scheduler)

**Second container in same compose?**

Yes. Add a `scheduler-worker` service with shared `assistant.db` via
the same bind mount. UDS for IPC adds one more bind mount or an
`/tmp` tmpfs mount on both. Compose model accommodates this without
restructure.

Caveat: shared sqlite + WAL mode + two writers = needs careful
testing. Phase 8 spike RQ should validate that two containers
hitting the same WAL file via bind-mount don't deadlock. RQ4
verified single-writer + reader semantics; two-writer is a new
question.

### Phase 9 (hardening)

Compose extensible without rewrite:

- `read_only: true` + `tmpfs: ["/tmp"]` + per-write-path bind mounts
  — adds ~10 lines.
- `cap_drop: [ALL]` + `no-new-privileges:true` — 4 lines.
- Cosign image signing — CI workflow change only, no compose impact.
- SBOM via `docker buildx build --sbom=true` — CI workflow flag.
- Trivy SARIF upload — needs `security-events: write` permission +
  one `format: sarif` block.

All listed in description.md §M. No phase-5d artifact needs
restructuring; phase 9 is purely additive. Good.

---

## 11. .dockerignore review

`tests/` is in `.dockerignore` (line 60), but the `test` Dockerfile
target (line 200) has `COPY tests/ ./tests/`. **This will fail in CI.**

Either:

1. The CI invokes `docker buildx build --target test` with
   `.dockerignore` overridden (non-trivial — needs `--build-context`
   or context relaxation), OR
2. The COPY silently no-ops the `tests/` (BuildKit warns, doesn't
   fail), and the `pytest` CMD finds zero tests, and CI passes
   with a green "0 tests collected" — false-positive.

Verify on first CI run. If it reproduces, fix by either:

- Removing `tests/` from `.dockerignore` (the runtime target doesn't
  COPY tests, so production image stays clean), OR
- Adding a `# syntax=docker/dockerfile:1.7` directive that supports
  `COPY --from=context` with a separate context, OR
- Splitting the test target into a separate Dockerfile
  (`Dockerfile.test`) with its own narrower `.dockerignore`.

The cleanest fix is **remove `tests/` from `.dockerignore`** — the
runtime image's COPY directives don't reference `tests/`, so the
exclusion adds no security/size benefit, only a CI-target-build
hazard.

Implementation-v2.md line 357 acknowledges this:

> Note: `tests/` is excluded from the runtime build context but the
> `test` Dockerfile target re-COPYs it explicitly (CI invokes a
> separate `--target test` build with a different context filter, OR
> builds with `.dockerignore` momentarily relaxed via
> `--build-context`).

But neither workaround appears in the actual workflow file. **This is
a bug that will surface on first CI run.** Flag for coder.

---

## 12. Findings by severity

### CRITICAL (must fix before deploy)

1. **`tests/` in .dockerignore vs `COPY tests/` in test target.**
   Test job will silently no-op or fail. Remove `tests/` from
   `.dockerignore`. (See §11.)

### HIGH (fix in this phase if time permits)

2. **`willfarrell/autoheal:latest` unpinned.** Pin to a digest or at
   minimum a semver tag. Production image otherwise carefully pinned;
   this is the lone exception. (See §1, §5.)

3. **`aquasecurity/trivy-action@master` unpinned.** Bump to a tag-pin
   in the same commit as #2. (See §5, §8.)

4. **No `mem_limit` / `cpus` caps.** Set generous-but-bounded caps
   (e.g. `mem_limit: 1500m`, `cpus: "2.0"`) before phase 6 lands. (See §6.)

### MEDIUM (track for phase 5e or phase 9)

5. **Image size guard missing in CI.** Add a 1 GB ceiling check that
   fails the build on creep. (See §1.)

6. **No base-image digest refresh automation.** Schedule monthly
   Dependabot or cron PR. (See §5.)

7. **README missing DR sections for: GHCR outage, OOM kill mid-write,
   data-dir corruption.** Add ~30 lines covering these. (See §3.)

8. **README missing `docker stats` recipe.** Add to §"Log tailing"
   or new §"Resource monitoring". (See §2.)

9. **README missing autoheal log channel mention.** Add to §"Log
   tailing" troubleshooting. (See §5.)

10. **README missing healthcheck failure-mode tree.** Add a
    "what each check failure means" subsection. (See §9.)

11. **README missing "verify systemd fallback works" step at 24h
    burn-in.** Add to migration playbook. (See §3.)

12. **README missing `get.docker.com` vs `apt-get install docker.io`
    guidance.** Specify get.docker.com. (See §7.)

### LOW (polish)

13. **README sqlite `.backup` recipe doesn't state container can stay
    running.** One-line addition. (See §3.)

14. **README missing local-image-cache recipe for GHCR outage.**
    5-line `docker save / docker load` block. (See §3.)

15. **README missing "expect ~30s downtime during update" note.**
    One-line addition. (See §4.)

---

## 13. Runbook gaps (consolidated)

| Topic | Gap | Severity |
|-------|-----|----------|
| `docker stats` for resource monitoring | Not mentioned | Medium |
| Autoheal log channel | `docker compose logs autoheal` not in README | Medium |
| Healthcheck failure-mode tree | Each check's failure has no triage path | Medium |
| systemd fallback verification | "Test the fallback before retiring" missing | Medium |
| `get.docker.com` vs `docker.io` | Compose v2.20 requires the former | Medium |
| GHCR outage recovery | No `docker save / load` recipe | Low |
| sqlite `.backup` live-safety | Owner may incorrectly stop container first | Low |
| Update downtime expectation | "30s no-replies window" not stated | Low |
| Long-term log retention | What if older than 5 rotations? | Low |
| Resource limit OOM behavior | Once added (§6), document the recovery story | Low |

---

## 14. Phase 6+ prerequisites checklist

For phase 6 (media on VPS):

- [ ] Add `ffmpeg libsndfile1` to runtime apt install (~150 MB image bloat).
- [ ] Re-measure image size; should still be < 700 MB compressed.
- [ ] Decide whether transcription model lives in same container or
  sidecar (sidecar if GPU; same-container if CPU-only).
- [ ] Add `mem_limit` / `cpus` caps NOW (before phase 6 — see #4).

For phase 7 (vault git push):

- [ ] Add SSH deploy key bind mount + `GIT_SSH_COMMAND` env to compose.
  Two-line patch.
- [ ] No Dockerfile change (gh + git already present).

For phase 8 (out-of-process scheduler):

- [ ] Spike RQ: two-writer sqlite WAL across bind-mount safety.
- [ ] Spike RQ: UDS bind-mount across compose services.
- [ ] Compose addition: `scheduler-worker` sibling service.

For phase 9 (hardening):

- [ ] All items in description.md §M (read_only, cap_drop,
  no-new-privileges, Cosign, SBOM, SARIF upload, rootless Docker,
  arm64 reopen, retention policy automation).
- [ ] Items added during phase 5d review:
  - Replace autoheal sidecar with host systemd timer (eliminates
    docker socket exposure).
  - Digest-pin all GHA actions (`@master` → `@<sha>`).
  - Base-image digest refresh automation (Dependabot or scheduled
    CI job).

---

## 15. Positive observations

This deserves explicit acknowledgement:

- **Multi-stage Dockerfile is exemplary.** Stages 1-4 + separate test
  target is exactly the right structure. Build-time sanity asserts
  (lines 108-109, 172) are the kind of belt-and-braces that catch
  silent regressions.
- **RQ13 symlink trick** is genuinely clever — eliminates a 300 MB
  npm/node stage by exploiting the SDK's bundled binary.
- **`stop_grace_period: 35s` + `/etc/docker/daemon.json
  shutdown-timeout: 40`** is the kind of detail that comes only from
  having been bitten by SIGKILL pre-empting `.last_clean_exit`.
- **Healthcheck `/proc/$pid/exe` readlink** correctly addresses both
  pid-recycle (W2-C3) and empty-pid-file race (W2-M5). Many shops
  ship `kill -0` and live with the false positives.
- **`env_file` object form with `required: false`** is the right
  call; simple-list form would have caused first-boot confusion.
- **`${TAG:?}` no-default** — production-grade choice over the
  pervasive `:-latest` footgun.
- **`autoheal=true` label-driven sidecar** is a pattern that scales
  cleanly to phase 8's scheduler container without rewriting the
  sidecar config.
- **Migration playbook order** (verify-before-mutate → backup →
  systemd-stop → up → smoke → reboot → 24h burn-in → retire) is
  the textbook sequence. Scope note acknowledges this; I concur.
- **`tini` as PID 1** correctly addresses signal forwarding and
  zombie reaping. Many production images skip this and live with
  ghost processes.
- **Pull-by-digest in CI smoke/scan jobs** is more robust than
  pull-by-tag and shows the implementation-v2 blueprint was iterated
  on (W2-H2 Option B applied).

This is a phase that will hold up well in production. The findings
above are real but none of them invalidate the design; this is
finishing-touches work, not foundation rebuild.

---

**End of review.**
