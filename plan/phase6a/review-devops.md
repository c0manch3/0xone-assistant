# Phase 6a — DevOps review

**Reviewer:** senior DevOps / SRE (read-only).
**Scope:** ops impact of the file-upload feature on the phase-5d Docker
stack on VPS `193.233.87.118`. Files in scope: `deploy/docker/Dockerfile`
(+8 LOC `mkdir+chown /app/.uploads`), `pyproject.toml` (+3 runtime + 1
dev dep), `src/assistant/main.py` (+112 LOC `_boot_sweep_uploads`),
`src/assistant/config.py` (+18 LOC `uploads_dir` property). Reference:
`plan/phase6a/{description,implementation-v2,devil-wave-1,spike-findings}.md`,
`plan/phase5d/review-devops.md` (carried-forward findings).

**Hard rule honored:** read-only; no review of extractor correctness,
unit-test coverage, handler control flow, or any code-quality concern
already addressed by the code reviewer.

---

## Executive summary

Phase 6a is a small, well-bounded feature with a tightly-scoped ops
footprint: one new tmp directory inside `/app`, three pure-python wheels
(<2 MB), one synchronous boot-sweep, no new services, no new env vars,
no new ports, no new bind mounts, no schema migrations. The phase-5d
container layout absorbs all of this without restructuring — the design
was visibly forward-thinking on this front (peak RSS headroom, 1 GB
image cap, hook constraint already locking tmp inside `/app`).

The piece that deserves explicit acknowledgment is the **boot-sweep
policy**: the devil-wave H3 finding correctly drove the plan from a
1 h-bound sweep (which crash-loops with disk fill) to UNCONDITIONAL on
top-level entries plus a 7-day age prune on `.failed/`. That's the kind
of ops-aware policy that catches real incidents in production. The
sweep is sync, fast (<10 ms typical), runs after `configure_memory` and
before adapter polling, and emits a single `boot_sweep_uploads_done`
event with `wiped_orphans` + `pruned_failed` counters — three of the
runbook gaps below stem from that telemetry being underused, not
missing.

Three classes of ops concern remain after the diff: **(1) the uploads
tmp dir lives in the container layer (no bind mount), so `docker
compose down` + recreate silently destroys quarantined `.failed/`
forensic data**; **(2) `mem_limit: 1500m` was set in phase 5d before
the 42 MB XLSX peak RSS data point existed — it remains correct but
the headroom should be re-asserted, not assumed**; **(3) the `.failed/`
quarantine directory has a 7-day age prune but no size cap, and devil
wave-1 H4 explicitly deferred the size cap to phase 6e — that deferral
is acceptable for the single-owner trust model but the runbook needs
one disk-watch recipe so the owner notices growth before phase 6e
ships**. None of these block phase 6a; all three are runbook /
phase-6e items.

---

## Verdict: **READY**

Ship to VPS as planned. No DevOps blockers. The `git pull && docker
compose pull && up -d --force-recreate` deploy procedure is unchanged;
rollback to `:phase5d` works cleanly because there are no schema
migrations and no new env vars; image size delta is well below the
1 GB CI red line; resource caps comfortably absorb the new peak RSS.
The polish items below should be tracked in `plan/phase9/` debt or
absorbed by phase 6b (which inherits the `/app/.uploads/` tmp-dir
contract).

---

## 1. Image / build impact

### Image size delta

Pure-python wheels:

| Wheel | Size | License |
|-------|------|---------|
| `python-docx>=1.1,<2` | ~244 KB | MIT |
| `openpyxl>=3.1,<4` | ~250 KB | MIT |
| `pypdf>=5.0,<6` | ~300 KB | BSD-3 |

**Delta on the runtime image: < 2 MB** (devil L1 corrected an earlier
~6 MB estimate; both python-docx and openpyxl pull `lxml`-free
implementations, and pypdf has no native deps). This is well below the
1 GB CI red line carried forward from phase 5d. No image-size guard
exists in CI today (phase-5d devops review §1 flagged it; still
unaddressed) — phase 6a does not change that gap, but the buffer is
ample.

`types-openpyxl>=3.1,<4` is dev-only, only enters the `test` Dockerfile
target. Zero impact on the production runtime image.

**Verdict:** acceptable, well within budget. No size guard regression.

### Build time impact

Three additional pure-python wheels in the `uv sync --frozen --no-dev
--no-editable` line. Empirically each pure-python wheel adds ~1-2 s in
the builder stage; total additional build time **~3-5 s** (in line with
the scope estimate). With BuildKit GHA cache hits on `uv.lock`-stable
runs, the impact is closer to zero (the wheels are cached layers).
Cold-cache CI on the lock-file change commit will see one ~5 s bump;
warm-cache subsequent commits see none.

**Verdict:** trivial. No CI duration-budget regression.

### `uv.lock` regeneration

The plan's pre-coder checklist requires `uv lock` regenerated alongside
`pyproject.toml`. The lock file's diff should be purely the three new
deps + transitive (openpyxl pulls `et_xmlfile`, python-docx pulls
`typing-extensions` already in tree). **Verify:** the `uv sync
--frozen` line in the builder will fail loudly if the lock file is out
of sync with pyproject — that's the safety net.

**Verdict:** no concerns; existing CI guards catch lock-drift.

### Reproducibility

Base image still pinned by manifest-list digest. `UV_VERSION=0.11.7`
still pinned. `gh` apt repo still pinned by key fingerprint. Phase 6a
adds no new floating dependencies. Reproducibility profile unchanged
from phase 5d.

---

## 2. Container layout / disk usage

### `/app/.uploads/` is in the container layer

This is the most ops-relevant property of phase 6a. Specifically:

| Path | Persistence on `docker compose down` | Bind-mounted? |
|------|---------------------------------------|---------------|
| `/app/.uploads/<uuid>__<stem>.<ext>` | LOST | No |
| `/app/.uploads/.failed/<uuid>__<stem>.<ext>` | LOST | No |
| `/app/.uploads/.failed/` (dir itself) | LOST | No |

**Per spec §A this is correct for INPUT-only attachments** — the file
is meant to be ephemeral; the model reads it, the handler unlinks it
in `finally`, the next turn never references it. The phase-7 vault git
push deliberately walks `<data_dir>/vault/` only, so quarantine data
is never pushed to GitHub. The boot-sweep is unconditional precisely
because every file present at boot is by definition stale.

But `down` + `up` is destructive in one specific way that the runbook
should call out: **`/app/.uploads/.failed/` forensic data is lost on
container recreate**. That's typically intended (the owner accepts a
7-day quarantine retention only while the daemon is alive), but the
following operations destroy it earlier:

1. `docker compose down && docker compose up -d` (the canonical "I
   want a fresh container" recipe) — destroys `.failed/`.
2. `docker compose pull && docker compose up -d --force-recreate` (the
   canonical update path) — destroys `.failed/`.
3. `docker rm 0xone-assistant && docker compose up -d` — destroys
   `.failed/`.
4. Image rebuild + push + pull — destroys `.failed/` on the next
   recreate.

This is **consistent with single-owner trust model and accepted per
description §A**, but the runbook needs one paragraph stating it
explicitly so the owner doesn't lose forensic evidence on a debug
session ("why did my XLSX fail extraction?" → `down` to apply a fix
→ evidence gone).

**Recommendation (runbook):**

> **Quarantine retention caveat.** `/app/.uploads/.failed/` lives in
> the container layer, not a bind mount. `docker compose down` /
> `up -d --force-recreate` destroys quarantined files. To preserve
> forensic data across a container restart:
> ```bash
> docker cp 0xone-assistant:/app/.uploads/.failed/ ~/quarantine-backup-$(date +%F)/
> ```
> Run BEFORE `down` / `pull && force-recreate`.

This costs zero LOC and closes the only foot-gun in the upload-data
lifecycle.

### Disk usage bound

Worst-case `/app/.uploads/` size:

- Top-level: 0 between handler turns (per-turn `finally` unlinks
  cleanly + UNCONDITIONAL boot-sweep). Maximum during a single in-flight
  turn: 1 × 20 MB = **20 MB**.
- `.failed/`: 7-day age horizon × 20 MB cap × adversarial spam rate.
  Devil H4's worst-case math: 30 corrupted-PDF debug session = 600 MB;
  realistic single-owner = essentially 0. Documented worst-case
  ceiling: **≤ 1.4 GB / 7 d** (single-owner + 20 MB cap + 7 d).

The 1.4 GB worst-case is pathological (adversarial owner spamming
quarantine targets); realistic ops should see `.failed/` at single-MB
or single-file scale. Phase-6e is the right place for a hard size cap
(e.g. 200 MB ceiling with oldest-first eviction).

**Verdict:** acceptable bound for the single-owner trust model. The
deferral of devil H4 to phase 6e is justifiable given the 7-day age
prune already in place, BUT the runbook must add a disk-watch recipe
so owner notices unexpected growth (see §6 below).

### Container-layer write semantics

Phase 6a writes to `/app/.uploads/` (image layer). Docker's overlayfs
copy-on-write semantics mean every new file is a new entry in the
container's R/W layer. Long-running containers see the layer grow
between restarts (per-turn unlink reclaims inode space immediately on
ext4/overlayfs, so the layer doesn't bloat). This is a non-issue at
single-owner traffic; flagging only because it would matter under
multi-tenant load (out of scope).

### `.dockerignore` impact

`.dockerignore` already excludes `.env*`, `secrets.env`, `**/secrets.env`,
`.local/`, `.config/`, `.claude/`. The `.uploads/` directory is added
to `.gitignore` (verified in the diff). No `.dockerignore` change is
needed — `/app/.uploads/` is created by the Dockerfile `mkdir`, not
COPY'd from build context, so a build-context-side `.uploads/` (only
present on Mac dev) would not pollute the image. Defense in depth
suggests adding `.uploads/` to `.dockerignore` anyway:

```dockerfile
# In deploy/docker/.dockerignore, group with .local/:
.uploads/
```

**Severity:** LOW. Strictly belt-and-suspenders; phase 6a build
correctness is unchanged either way.

---

## 3. Resource impact

### Memory headroom vs `mem_limit: 1500m`

Compose has `mem_limit: 1500m` from phase 5d (per phase-5d devops
review §6 recommendation, not yet shipped per phase-5d findings — but
the pasted compose at compose-line-70 has it set, so this is shipped).

| Scenario | Peak RSS (RQ2 spike) | Headroom vs 1500 MB |
|----------|----------------------|---------------------|
| Idle daemon (phase-5d baseline) | ~890 MB | ~610 MB |
| Idle + 20 MB XLSX extract | 890 + 42 = ~932 MB | ~568 MB |
| Idle + 308 MB adversarial XLSX | 890 + 42 = ~932 MB | ~568 MB |
| Idle + 20 MB DOCX extract | 890 + small (no spike data) | ~600 MB |
| Idle + concurrent memory write + XLSX | conservatively ~1.0 GB | ~500 MB |

`read_only=True, data_only=True` keeps peak RSS flat regardless of
input file size — RQ2 verified that `openpyxl`'s streaming mode does
not load the workbook into memory. The 42 MB increment is the
processing overhead, not file size.

**Verdict:** comfortable headroom. `mem_limit: 1500m` is right for
phase 6a. **DO NOT lower it** — phase 6b/6c will add image / voice
processing on top.

### CPU impact

Sync extract: 7 s wall-clock for a 20 MB XLSX with `ROW_CAP=50` (RQ2),
3 s with `ROW_CAP=20` (Q13 frozen). Single core, no GIL release. Phase
5b's per-chat lock serialises owner + scheduler turns on the same
chat_id, so a long extract delays scheduler ticks for OWNER_CHAT_ID
(devil L4 — accepted).

`cpus: 2.0` from phase 5d compose: ample. No change needed.

### Disk I/O impact

Boot-sweep: 1 × `iterdir()` + N × `stat()` + M × `unlink()`. On
ext4/overlayfs typical N is 0 after a clean shutdown, M ≈ orphaned
files from a SIGKILL'd previous boot. Single-digit milliseconds.

Per-turn: 1 × `download` (Telegram→local), 1-2 × file open/read for
extract, 1 × `unlink` in `finally`. All synchronous. No I/O budget
concern.

---

## 4. Ops surface — telemetry / observability

The implementation emits a coherent set of structlog events:

| Event | Source | Signal |
|-------|--------|--------|
| `document_received_without_handler` | telegram.py:174 | rare — handler not yet wired |
| `uploads_mkdir_failed` | telegram.py:213 | disk full / permissions issue |
| `document_download_failed` | telegram.py:237 | network blip / disk full / Telegram error |
| `attachment_unlink_failed` | message.py:402 | post-turn cleanup issue |
| `extraction_failed_quarantined` | message.py:432 | encrypted / corrupt input → `.failed/` |
| `quarantine_rename_failed` | message.py:439 | quarantine path not writable |
| `boot_sweep_uploads_done` | main.py:144 | counters: `wiped_orphans` + `pruned_failed` |
| `boot_sweep_iterdir_failed` | main.py:81 | `/app/.uploads/` permissions issue at boot |
| `boot_sweep_failed_path_not_dir` | main.py:99 | corruption: `.failed/` exists as a file |
| `boot_sweep_failed_iterdir_failed` | main.py:107 | `.failed/` permissions issue |
| `boot_sweep_failed_prune_error` | main.py:119 | per-file unlink permission issue |
| `boot_sweep_skipped_unexpected_dir` | main.py:133 | unexpected subdir under uploads (phase-6b/c forward-compat) |
| `boot_sweep_orphan_unlink_error` | main.py:139 | top-level orphan unlink permission issue |

**Strong points:**

- The `boot_sweep_uploads_done` counters give the owner an at-a-glance
  signal "did the sweep do anything?" — useful in a `docker compose
  logs --tail 100 | grep boot_sweep_uploads_done` post-restart check.
- `extraction_failed_quarantined` carries `turn_id`, `path`, `reason`
  — sufficient for forensic correlation.
- Every `OSError` path is logged-and-swallowed, not raised, so a
  flaky filesystem doesn't crash the daemon mid-flight.

**Gap — missing event:** there is no `attachment_received` /
`attachment_extracted` event marking the happy path. Today the owner
can grep `extraction_failed_quarantined` to count failures but cannot
count successful uploads from logs alone. For single-user this is fine
(the Telegram conversation history IS the audit log) — but a
`document_accepted` event with `kind`, `size_bytes`, `chat_id` would
make `docker compose logs | grep document_accepted | wc -l` a
one-liner. **Severity: LOW** — nice-to-have for phase 6e.

**Gap — extraction timing:** `extract_xlsx` can take 7 s on a 20 MB
input. There's no `extraction_completed` event with elapsed-time
telemetry. The `turn_started` / `turn_completed` envelope from phase 2
captures total turn latency but not the extract-specific component.
**Severity: LOW** — useful only if phase 6e considers an SLO; out of
scope today.

---

## 5. Deploy / rollback

### Deploy procedure (unchanged)

```bash
# On Mac:
git push origin main          # CI builds + pushes :sha-<short> + :phase6a tag
                              # CI test target runs full pytest suite

# On VPS:
cd /opt/0xone-assistant
echo "TAG=phase6a" > deploy/docker/.env  # or sha-<short>
git pull
docker compose pull
docker compose up -d --force-recreate
docker compose logs -f --tail 50  # watch for boot_sweep_uploads_done
```

**Verdict:** zero changes to the deploy recipe. Phase 6a does not
introduce a new env var, new bind mount, new service, or new schema
migration. The single Dockerfile addition (`mkdir -p /app/.uploads
/app/.uploads/.failed && chown -R 1000:1000 /app/.uploads`) takes
effect on container creation; no host-side prep required.

### Rollback to `:phase5d`

```bash
echo "TAG=phase5d" > deploy/docker/.env
docker compose pull
docker compose up -d --force-recreate
```

**Verdict:** clean rollback. No schema migrations in 6a (verified —
no `ALTER TABLE`, no new sqlite tables, no migration runner in start
order). The phase-5d image lacks `python-docx` / `openpyxl` / `pypdf`
imports, but those are loaded LAZILY inside the extractor functions
(`from docx import Document` is INSIDE `extract_docx`, not at module
level — verified). A turn arriving with an attachment after rollback
would route through `_on_document` only if the rollback image has the
handler — it does NOT (phase-5d telegram adapter has only `_on_text`
+ `_on_non_text`). The catch-all replies "Медиа пока не поддерживаю"
and the owner gets a graceful "feature gone" reply, not a crash.

`/app/.uploads/` directory presence after rollback: irrelevant. The
phase-5d Dockerfile doesn't create it; the runtime image lacks the
mkdir line. The phase-6a image's `/app/.uploads/` is destroyed when
its container is removed for the rollback recreate. Clean.

**Verdict:** rollback story is sound. Time-to-recover ~90-120 s per
phase 5d devops review §3 timing.

### Telegram in-flight messages during deploy

`stop_grace_period: 35s` from phase 5d gives the daemon time to (a)
finish in-flight extracts, (b) `unlink()` tmp files, (c) write
`.last_clean_exit`. A 7 s XLSX extract mid-deploy is well within the
grace window. SIGKILL leaves `.uploads/` orphans — boot-sweep cleans
them on next start.

**Verdict:** no new failure modes vs phase 5d.

---

## 6. Runbook gaps

The phase-5d README has the canonical operational-task recipes (log
tailing, healthcheck, backup, transcript pruning). Phase 6a adds three
ops surfaces that need ~10 lines of README addendum each:

### Gap 1: Quarantine inspection recipe

The README has no instructions for inspecting `.failed/`. Owner
debugging "why did my XLSX fail?" needs a one-liner:

```bash
# List quarantined attachments (most-recent first)
docker exec 0xone-assistant ls -lt /app/.uploads/.failed/

# Pull a failed file to host for inspection
docker cp 0xone-assistant:/app/.uploads/.failed/<uuid>__<name>.xlsx ~/

# Grep for the corresponding extraction error in logs
docker compose logs | grep extraction_failed_quarantined
```

**Severity: MEDIUM.** Without this, post-mortem on extraction failures
is awkward.

### Gap 2: Disk-watch for `.failed/` growth

Devil H4 deferred a hard size cap to phase 6e on the basis that the
single-owner trust model + 7-day prune bound growth at ≤ 1.4 GB
worst-case. Acceptable, but the owner needs a one-liner to spot
unexpected growth before phase 6e ships:

```bash
# Quarantine size + count (run weekly or on suspicion)
docker exec 0xone-assistant du -sh /app/.uploads/.failed/
docker exec 0xone-assistant ls -1 /app/.uploads/.failed/ | wc -l
```

If growth becomes a concern: `docker exec 0xone-assistant find
/app/.uploads/.failed/ -mtime +7 -delete` is a manual-prune escape
hatch (boot-sweep does this but only on restart).

**Severity: MEDIUM.** Closes the loop on the H4 deferral.

### Gap 3: Container-layer destruction on `down`

Per §2 above: `docker compose down` destroys `.failed/`. Owner needs
a documented "preserve quarantine before recreate" recipe:

```bash
# BEFORE docker compose down / force-recreate, if forensic data matters:
docker cp 0xone-assistant:/app/.uploads/.failed/ ~/quarantine-backup-$(date +%F)/
```

**Severity: MEDIUM.** Avoids accidental forensic data loss on routine
deploys.

### Gap 4: Healthcheck unaffected, but document why

The phase-5d healthcheck (pid + `/proc/<pid>/exe` readlink) is
untouched in phase 6a. It correctly does NOT check `/app/.uploads/`
writability — the daemon survives a temporarily-unwritable uploads dir
(returns the user-visible "не смог подготовить папку для файлов" reply
in the adapter), so a R/O uploads dir is not a process-health concern.
Document this explicitly so the owner doesn't add it to the
healthcheck on a misguided "make sure uploads work" instinct:

```
# In README §"Healthcheck", add note:
# Phase 6a does NOT add an upload-dir writability check to the
# healthcheck. A read-only /app/.uploads/ surfaces as a Russian reply
# to the user, not a daemon crash; treating it as a health failure
# would cause autoheal restart loops on transient filesystem issues.
```

**Severity: LOW.** Defensive against future runbook drift.

### Gap 5: Boot-sweep observability one-liner

Three new structlog events are emitted at boot
(`boot_sweep_uploads_done`, `boot_sweep_skipped_unexpected_dir`,
`boot_sweep_failed_path_not_dir`). Add a "what to look for after a
restart" line:

```bash
# After docker compose restart, confirm boot-sweep ran:
docker compose logs --tail 100 0xone-assistant | grep boot_sweep_uploads_done
# Expected output (clean restart):
# {... "event": "boot_sweep_uploads_done", "wiped_orphans": 0, "pruned_failed": 0, ...}
# Non-zero wiped_orphans = previous boot died mid-turn (informational, not an error).
# Non-zero pruned_failed = quarantine retention working (informational).
```

**Severity: LOW.**

---

## 7. Phase 6b/c/d prerequisites

Phase 6a establishes one cross-cutting ops contract: **`/app/.uploads/`
is the canonical tmp-dir convention for media subphases**. Each
follow-on subphase needs to make a clear policy choice up-front:

### Phase 6b (vision / photos)

**Decision needed pre-6b:** does 6b reuse `/app/.uploads/` for photos
or split into `/app/.photos/`?

**Recommendation: reuse `/app/.uploads/` with subdirectory split**.
Specifically:

```
/app/.uploads/             ← documents (PDF/DOCX/XLSX/TXT/MD) [current]
/app/.uploads/.failed/     ← quarantine [current]
/app/.uploads/photos/      ← phase 6b photos [proposed]
/app/.uploads/voices/      ← phase 6c voice OGGs [proposed]
```

Reasons:

1. **Single boot-sweep target.** `_boot_sweep_uploads()` already
   handles unexpected subdirs gracefully (`boot_sweep_skipped_unexpected_dir`).
   Extending it to recurse into `photos/` and `voices/` is a 5-line
   change, not a copy of the whole sweep.
2. **Single chown line in Dockerfile.** Adding `mkdir -p
   /app/.uploads/photos /app/.uploads/voices && chown -R 1000:1000
   /app/.uploads` keeps the chown footprint single-rooted.
3. **Single Settings property.** `settings.uploads_dir / "photos"`
   composes; a `settings.photos_dir` parallel property is bloat.
4. **Single `.gitignore` entry.** `.uploads/` already excludes the lot.

The existing `boot_sweep_skipped_unexpected_dir` handler is the
forward-compat marker — phase 6b will need to extend
`_boot_sweep_uploads` to recurse into known subdirs (`photos/`,
`voices/`) rather than skip them.

**Alternative (rejected): separate `/app/.photos/` and `/app/.voices/`.**
Each adds a Dockerfile chown line, a Settings property, a gitignore
entry, and a sweep target. Three subdirs × four artifacts = 12 LOC of
overhead vs the unified approach's ~3.

### Phase 6c (voice / whisper sidecar)

Same unified-tmp-dir argument applies. Additionally:

- The transcription sidecar (Mac box, per phase 6c plan) needs to
  receive the OGG bytes — either via shared-filesystem (rsync over
  ssh from VPS to Mac, then sidecar processes) or via HTTP POST
  to a Mac-hosted endpoint. Phase 6c plan owns that decision; phase
  6a's `/app/.uploads/voices/<uuid>.ogg` convention works for either.
- Voice files are small (typical OGG voice messages: 5-50 KB, hard
  cap 20 MB); disk usage impact negligible.

### Phase 6d (image-gen / outbound)

Outbound only — no `/app/.uploads/` interaction. Phase 6d would write
generated images to `/app/.outbound/<uuid>.png` (parallel directory)
and `bot.send_photo()` from there, then unlink. **Not** a continuation
of the phase 6a tmp-dir convention; outbound is a different lifecycle
(no boot-sweep needed if every send-then-unlink is `finally`-bound).

### Image size growth across 6b/c/d

Cumulative wheel growth on the runtime image:

| Phase | Adds | Image delta |
|-------|------|-------------|
| 6a | python-docx, openpyxl, pypdf | <2 MB |
| 6b | Anthropic vision is API-side; no client-side wheel except possibly Pillow for resize | ~3-5 MB if Pillow added |
| 6c | OGG handling on VPS side: `mutagen` or `pydub`. ffmpeg is on Mac sidecar, not VPS. | ~1-2 MB |
| 6d | Pillow (probably already in 6b), no new wheels | 0 |

Cumulative through 6d: ~7-10 MB. Still well below the 1 GB CI red line
(today the image is ~400 MB uncompressed per phase 5d). Phase 6c's
ffmpeg-on-VPS option (rejected per description.md, but worth restating)
would add ~150 MB; sticking to the Mac sidecar keeps the VPS image
lean.

### Boot-sweep extension contract

Phase 6b's coder MUST NOT rewrite `_boot_sweep_uploads`. They should
extend it by adding `photos`, `voices` to a "known subdir" allow-list
that recurses with the same per-file-stale + age-prune policy. The
current handler logs `boot_sweep_skipped_unexpected_dir` for any
unknown subdir — that log line is the explicit hand-off point ("if
you see this for a directory you intend to use, extend the sweep").

### Resource cap re-check

Phase 6c is the most likely to bump peak RSS: voice transcription
(if model loaded in-process) easily adds 500 MB-1 GB. **Phase-6c plan
must verify `mem_limit: 1500m` headroom.** Phase 6a does NOT need a
cap revisit; phase 6c does.

---

## 8. Findings by severity

### CRITICAL

None.

### HIGH

None.

### MEDIUM

1. **Container-layer destruction on `down`.** Add runbook recipe to
   preserve `.failed/` before recreate. (See §2, §6 Gap 3.)
2. **Quarantine inspection recipe missing from README.** Add 5-line
   §6 Gap 1 recipe.
3. **Disk-watch for `.failed/` growth.** Add 3-line §6 Gap 2 recipe;
   closes the H4 deferral loop.

### LOW

4. **`.dockerignore` could add `.uploads/`.** Defense-in-depth; no
   functional change. (See §2.)
5. **No `attachment_accepted` happy-path event.** Useful for log
   counting in phase 6e. (See §4.)
6. **No extract-elapsed-time telemetry.** Only relevant if a future
   phase adopts an SLO. (See §4.)
7. **README missing "boot-sweep observability" one-liner.** (See §6
   Gap 5.)
8. **README missing "healthcheck does NOT check uploads dir" note.**
   Defensive against future runbook drift. (See §6 Gap 4.)

---

## 9. Runbook gaps consolidated

| Topic | Gap | Severity |
|-------|-----|----------|
| Quarantine inspection | No `docker exec ls /app/.uploads/.failed/` recipe | Medium |
| `.failed/` disk watch | No `du -sh` recipe; closes H4 deferral loop | Medium |
| Quarantine destruction on `down` | Owner may lose forensic data unknowingly | Medium |
| Boot-sweep observability | No "grep boot_sweep_uploads_done after restart" recipe | Low |
| Healthcheck scope | Document why uploads dir is NOT health-checked | Low |
| Happy-path upload count | No `attachment_accepted` event for log-counting | Low |
| Extract latency | No per-extract elapsed-ms telemetry | Low |

---

## 10. Carry-forwards from phase 5d devops review

The following phase-5d findings remain open and are NOT addressed by
phase 6a (and shouldn't be — out of scope):

- Image-size CI guard (1 GB ceiling check) — still missing.
- `aquasecurity/trivy-action@master` — still unpinned.
- `willfarrell/autoheal:1.2.0` — semver-pinned (improvement over
  `:latest`); digest-pin still deferred to phase 9.
- Base-image digest refresh automation — still missing.
- Autoheal docker socket attack surface — phase 9.
- README missing `docker stats`, autoheal log channel, healthcheck
  failure-mode tree, GHCR-outage local-image-cache recipe — still
  applies, not regressed.

Phase 6a does NOT introduce any new finding in any of those
categories.

---

## 11. Positive observations

This deserves explicit acknowledgment:

- **Boot-sweep policy is genuinely well-engineered.** UNCONDITIONAL
  on top-level entries + 7-day age prune on `.failed/` is the right
  call; the devil-wave-1 H3 finding correctly drove the team off the
  1h-bound that would have crash-loop disk-filled. The fact that
  `_boot_sweep_uploads_done` emits structured counters (`wiped_orphans`,
  `pruned_failed`) shows ops-aware design.
- **Sync sweep before adapter polling starts** is the correct
  ordering. There is no "uploads in flight while sweep iterates" race
  by construction — clean invariant.
- **Single boot-sweep call site** (after `configure_memory`, before
  bridge construction) means one easy-to-reason-about ordering point,
  not a scattered set of cleanup hooks.
- **Forward-compat scaffold for 6b/6c.** The
  `boot_sweep_skipped_unexpected_dir` log line is an explicit
  hand-off marker for phase 6b — no future-self surprise. Same for
  the `entry.is_dir()` defensive check in `.failed/`.
- **Lazy imports inside extractor functions** (verified in
  `files/extract.py`) means a phase-5d rollback after phase 6a
  containers existed in the field doesn't crash on missing imports —
  the extractor module is never loaded if the handler never branches
  to it. Subtle and correct.
- **`.uploads/` location aligned with the hook constraint** (single
  `project_root` arg in `make_file_hook`, untouched). Choosing Option
  1 (move tmp into `/app/.uploads/`) over Option 2 (extend hook
  allow-list) keeps the most security-critical file in the repo
  unchanged. Right call.
- **Phase-7 vault git push leak prevention by design.** Vault at
  `<data_dir>/vault/` is on a different filesystem subtree from
  `/app/.uploads/`; the phase-7 push will only walk vault by
  construction. Zero leakage risk.
- **`stop_grace_period: 35s`** comfortably covers the worst-case
  7 s XLSX extract mid-shutdown. No new shutdown-window concern from
  6a.
- **Image size delta (<2 MB)** vs 1 GB CI red line: phase 6a is
  1/500th of the budget. Comfortable headroom for 6b/6c/6d cumulative
  growth.

This is a phase that demonstrates the team has internalized the ops
lessons from phase 5d (boot-sweep policy, structured logging on every
error path, lazy imports for rollback safety, container-layer-aware
disk usage bound). Findings above are real but none invalidate the
design; this is finishing-touches work, not foundation rebuild.

---

**End of review.**
