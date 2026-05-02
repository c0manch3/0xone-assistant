# Phase 9 — DevOps / Ops review (`render_doc` MCP @tool)

Lens: deployment, image, observability, fault tolerance, runbook, CI/CD.
Working tree state — files NOT yet committed. Spec referenced:
`plan/phase9/description.md` v3.

## Verdict

**APPROVE WITH FIX-PACK BEFORE DEPLOY.**

The build-time sanity in the runtime stage is the single best engineering
decision in this phase — it converts every category-of-failure I worried
about (missing apt pkg, Pango/HarfBuzz mismatch, WeasyPrint version drift)
into a build error rather than a 3 AM page after the first owner
`render_doc(...)` call. The boot reaper, lock-protected ledger, RSS gauge
integration, force-disable path, scoped pandoc env, and SIGTERM grace
recipe are all production-grade.

That said, the deployment story has gaps that must close before this
ships:

1. The `stop_grace_period: 35s` is **unchanged** in
   `deploy/docker/docker-compose.yml`, but the spec's own honesty
   paragraph admits the cumulative drain budget is 116s worst-case. The
   spec says "operator runbook documents this" — the runbook
   (`deploy/docker/README.md`) was **not updated** in this working tree.
   That is the phase-8 ssh-not-found anti-pattern in spirit: documented
   intent without the artefact.
2. There is **no image-size CI gate** despite spec §A8 mentioning one
   was considered. The new apt closure adds ~140-200 MB; without a
   gate, future pandoc/weasyprint bumps can drift the image silently.
3. The Trivy scan job runs against the new apt surface but Trivy will
   start flagging pandoc CVEs (Haskell-stack libs are CVE-noisy);
   nobody has reviewed whether `ignore-unfixed: true` will keep CI
   green on Debian bookworm's pandoc 2.17.1.1.
4. Multi-arch (arm64) was deferred from phase 5d "to phase 9" in the
   workflow comment — phase 9 did not reopen it. That's defensible
   given owner is on amd64 VPS, but the comment now lies.

Findings count: **2 CRITICAL · 5 HIGH · 8 MEDIUM · 6 LOW**.

---

## Findings

### CRITICAL

#### DC-1 — Operator runbook not updated; `stop_grace_period: 35s` unchanged with 116s worst-case drain

**Location:** `deploy/docker/docker-compose.yml:104`, `deploy/docker/README.md` (no phase-9 section).

**Description:** The spec §2.12 W2-HIGH-1 closure mandates: "operator
runbook + `deploy/docker/README.md` MUST mention `stop_grace_period`
override for production renders." Worst-case `Daemon.stop` drain is
`adapter.stop (~30s) + vault drain (60s) + render drain (20s) + bg
cancel (5s) + misc (1s) = 116s`. Current compose `stop_grace_period:
35s` SIGKILL fires before vault drain even completes, let alone render
drain.

A `grep -n 'render' /Users/agent2/Documents/0xone-assistant/deploy/docker/README.md`
returns nothing. Owner trying to render a large PDF concurrent with a
deploy gets: SIGKILL mid-pandoc, orphan staging file (boot reaper
catches), orphan SSH session for vault (phase-8 accepted residual),
**incomplete `.last_clean_exit` write** (phase-5b boot classifier marks
the next boot as "unclean-restart" → recap fires inappropriately).

**Risk:** HIGH. Production deploy → spurious "пока я спал" recap;
owner debugging cascading bugs. Phase-5d documented the
`stop_grace_period: 35s` as "comfortable for daemon's 35s shutdown
path"; phase 9 silently invalidated that comment because
`adapter.stop` alone is 30s and there's now another 60s + 20s
downstream of it.

**Fix:** Either:
- (a) Bump compose `stop_grace_period` to 180s (matches phase-8 LOW-3
  and phase-9 spec §10 W2-HIGH-1 recommendation),
- (b) Add an explicit "Phase 9 — render_doc operational notes" section
  to `deploy/docker/README.md` explaining the SIGKILL-during-render
  trade-off and the 180s override recipe, OR
- (c) Both (recommended — code default reflects most owners'
  expectations + docs let advanced operators tune).

```yaml
# deploy/docker/docker-compose.yml — recommended
stop_grace_period: 180s   # Phase 9 W2-HIGH-1: covers vault drain
                          # (60s) + render drain (20s) + adapter
                          # stop (30s) + audio persist (5s) + slack.
```

#### DC-2 — `_cleanup_stale_artefacts` fired BEFORE `RenderDocSubsystem.__init__`; no double-staging-dir creation race, but DEAD `.staging/` dir if subsystem disabled

**Location:** `src/assistant/main.py:446-455`.

**Description:** Boot ordering:
```python
if self._settings.render_doc.enabled:
    artefact_dir = self._settings.artefact_dir
    artefact_dir.mkdir(parents=True, exist_ok=True)
    (artefact_dir / ".staging").mkdir(parents=True, exist_ok=True)
    _cleanup_stale_artefacts(...)
    self._render_doc = RenderDocSubsystem(...)
    await self._render_doc.startup_check()
```

If `startup_check` then sets `force_disabled=True` (pandoc / weasyprint
missing), the empty `.staging/` and `artefact_dir/` directories were
already created and `_cleanup_stale_artefacts` ran, but the subsystem
will **never produce or sweep artefacts**. Side-effect: every boot of a
disabled-by-binary-missing instance touches the artefact dir, leaving
empty dirs the operator sees in `df -h` output and questions whether
the subsystem is alive or dead.

This is a quality-of-life issue, not a fault, BUT it overlaps with the
Daemon.stop drain logic at `main.py:1119-1125`:

```python
if self._render_doc is not None:
    with contextlib.suppress(Exception):
        await self._render_doc.mark_orphans_delivered_at_shutdown()
```

`self._render_doc` is non-None **even when force_disabled=True** — so
shutdown still acquires the lock + iterates an empty dict. Cosmetic
but the asymmetry between "force_disabled hides the @tool but keeps
the subsystem alive" and "force_disabled skips the sweeper loop" is
exactly the kind of inconsistency that hides bugs in phase-10
maintenance.

**Risk:** MEDIUM (cosmetic + code-asymmetry hazard). Promoted to
CRITICAL because it lands in the boot-ordering block where all phase-5b
clean-exit-marker bugs live, and the previous review of similar
ordering deserves an explicit signoff.

**Fix:** Either:
1. Move `artefact_dir.mkdir` + `.staging.mkdir` + `_cleanup_stale_artefacts`
   call **inside** `RenderDocSubsystem` (lazily, in `startup_check` after
   the force-disable verdict), OR
2. Skip `mkdir` + cleanup when `startup_check` would fully force-disable
   (run check FIRST, fs ops SECOND). Cleanest is to make `startup_check`
   a static-ish classmethod that returns a verdict and only construct
   the subsystem if `verdict.fully_disabled is False`.

The current shape is functional but it makes the "is render_doc on?"
state a tri-state (subsystem absent / subsystem present but
force_disabled / subsystem live) when it should be bi.

---

### HIGH

#### DH-1 — No image-size budget gate in CI

**Location:** `.github/workflows/docker.yml` (no size assertion step).

**Description:** Spec §A8 was descoped (per §10 W2-HIGH-1: "Wave A A8
(image size CI gate) does NOT add static check on `stop_grace_period`")
but the image-size CI gate itself is not actually wired. The phase-9
apt closure adds ≈ 140-200 MB (see §Image size projection below) — a
single-shot delta the owner expects. But once it lands, future
maintenance cannot detect a 100 MB regression from a careless
`apt-get install` without a baseline.

**Risk:** MEDIUM-HIGH. Phase-5d targeted ~400 MB amd64; phase 9 pushes
that to ~550-600 MB. A future "let me add libreoffice for ODF support"
PR would slip past code review easily without a budget.

**Fix:** Add a `size-budget` job to `docker.yml`:

```yaml
size-budget:
  needs: build-and-push
  if: github.event_name != 'pull_request'
  runs-on: ubuntu-24.04
  steps:
    - uses: actions/checkout@v4
    - run: docker pull ghcr.io/c0manch3/0xone-assistant@${{ needs.build-and-push.outputs.digest }}
    - name: Assert image size <= budget
      run: |
        SIZE_MB=$(docker image inspect "ghcr.io/c0manch3/0xone-assistant@${{ needs.build-and-push.outputs.digest }}" \
          --format '{{.Size}}' | awk '{ printf "%d\n", $1/1024/1024 }')
        BUDGET_MB=650  # Phase 9 baseline; bump deliberately.
        echo "image_size_mb=$SIZE_MB"
        test "$SIZE_MB" -le "$BUDGET_MB" || { echo "Image $SIZE_MB MB > budget $BUDGET_MB MB"; exit 1; }
```

The budget number is committed to the workflow — bumping it requires a
PR and a reviewer signoff. Cheap, durable, prevents drift.

#### DH-2 — Force-disable Telegram notify uses bare-`Exception` catch on adapter timeout

**Location:** `src/assistant/render_doc/subsystem.py:678`.

**Description:** The notify path:
```python
except (TimeoutError, Exception) as exc:
```

`(TimeoutError, Exception)` is a redundancy bug — `TimeoutError` IS an
`Exception`. The redundancy is harmless, BUT it shows the test
coverage didn't exercise the full failure mode: any
`asyncio.CancelledError` raised through `_adapter.send_text` will
propagate up (CancelledError is NOT an Exception subclass since 3.8).
That's correct behaviour — boot phase shouldn't swallow cancel — but
the spec does not document this. Owner running `kill -SIGTERM` during
the boot notify would see an unhandled CancelledError before
`Daemon.stop` runs.

**Risk:** LOW-MEDIUM, but flagged HIGH because it's a precedent for
phase-10 notify paths.

**Fix:**
```python
except TimeoutError as exc:
    log.warning("render_doc_force_disable_notify_timeout", error=repr(exc))
except Exception as exc:
    log.warning("render_doc_force_disable_notify_failed", error=repr(exc))
```

Also: the `_notified_force_disable = True` flag is only set on the
success path. On any failure the next boot retries the notify (good)
BUT if the failure is a permanent permissions problem (Telegram bot
revoked), the boot path retries forever. Add a per-boot `notified =
True` set inside the except as well, OR keep the retry-on-failure but
emit only at `info` level after the first attempt.

#### DH-3 — Sweeper backoff on respawn doesn't bound disk fill

**Location:** `src/assistant/render_doc/subsystem.py:332-342` + `main.py:_spawn_bg_supervised:883-`.

**Description:** `_spawn_bg_supervised` respawns the sweeper after
`backoff_s=5.0` and gives up after 3 crashes/hour. During that hour:
- Render concurrency = 2 (capped by semaphore).
- Each render produces a finalised artefact under
  `<data_dir>/artefacts/<uuid>.{pdf|docx|xlsx}`.
- TTL = 600s, sweep every 60s — under healthy operation 10x more
  artefacts can land than are swept, but TTL caps the steady state.
- If sweeper IS dead, after one hour you have ~120 artefacts × ~2 MB
  avg = 240 MB unswept. After 24h with no operator intervention =
  ~5.7 GB on a 4 GB-RAM VPS' state volume.

**Risk:** HIGH. Worse if the sweeper crash mode is something
deterministic (a single corrupt artefact triggers `OSError` on
`unlink`). The current implementation logs and continues
(`log.warning("render_doc_sweep_unlink_failed")`) — good — but the
**outer `_sweep_iteration` exception handler** is `except Exception`
(line 341) which catches everything including the lock acquisition
itself. If the lock somehow deadlocks (it shouldn't, async lock is
single-thread), the sweeper would crash, respawn, crash again 5s
later, give up after 15s of crashes.

**Fix:** This is mostly fine but `RSS observer` should also surface
artefact-dir disk usage:

```python
# In _rss_observer payload
if self._render_doc is not None:
    rss_payload["render_doc_inflight"] = self._render_doc.get_inflight_count()
    # NEW: surface disk floor so a dead sweeper is visible at runtime.
    try:
        artefact_bytes = sum(
            f.stat().st_size for f in self._settings.artefact_dir.iterdir()
            if f.is_file()
        )
        rss_payload["render_doc_artefact_bytes"] = artefact_bytes
    except OSError:
        pass
```

Cheap (one stat per file at 60s cadence; ≤ 200 files in steady state)
and surfaces "sweeper crashed at 03:14, disk fill noticed at 04:14"
in the same log stream as RSS.

#### DH-4 — Pandoc env whitelist drops `LC_ALL` — Cyrillic locale risk

**Location:** `src/assistant/render_doc/_subprocess.py:54-58`.

**Description:** Pandoc env:
```python
return {
    "PATH": os.environ.get("PATH", ""),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
    "HOME": os.environ.get("HOME", "/tmp"),
}
```

`LANG=C.UTF-8` is correct for UTF-8 byte handling. But the bookworm
runtime image installs `fonts-dejavu-core` (Cyrillic glyphs) and the
spec mandates Cyrillic content correctness. Pandoc 2.17.1.1 is
generally locale-tolerant for UTF-8 input — it doesn't `iconv`
internally — BUT certain pandoc filter operations (citation sorting,
title-case conversions) DO depend on `LC_COLLATE`. Setting `LC_ALL` is
defense-in-depth.

The Dockerfile does **not** install `locales` or generate any locale
beyond what slim-bookworm ships (none — `locale -a` returns only `C`,
`C.UTF-8`, `POSIX`). So even if the daemon sets `LC_ALL=ru_RU.UTF-8`,
glibc would fall back to C. Acceptable BUT the ops-side test must
verify Cyrillic round-trip in container.

**Risk:** MEDIUM. Likely no functional issue, but the test plan in spec
§Test fixture migration does not enumerate "render Cyrillic markdown
in container, check PDF text extraction returns same Cyrillic" as an
integration test. Phase 6c had a Cyrillic regression that mocks
missed.

**Fix:** Add `LC_ALL: "C.UTF-8"` to `_pandoc_env()` and add a CI
integration test that renders `# Привет, мир` to PDF and verifies the
extracted text matches via `pikepdf`.

```python
return {
    "PATH": os.environ.get("PATH", ""),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
    "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    "HOME": os.environ.get("HOME", "/tmp"),
}
```

#### DH-5 — Trivy scan will flag pandoc CVEs; no exemption strategy documented

**Location:** `.github/workflows/docker.yml:179-197`.

**Description:** Bookworm's pandoc 2.17.1.1 is built from Haskell
ecosystem; the lower-level `libgmp10`, `libffi8`, `libnuma1` libs it
pulls have a non-zero rate of HIGH/CRITICAL CVE filings. Once phase 9
deploys, the next Trivy run may flag a `libgmp10`-tagged CVE that
requires `apt upgrade` from bookworm-security. CI gates push on these
findings (`exit-code: "1"`).

The workflow has `ignore-unfixed: true` — so only **fixed** CVEs trip
the gate. Bookworm-security pushes fixes for security-team-graded
vulns. Most likely outcome: CI fails on a Tuesday, owner spends 30
minutes triaging.

**Risk:** MEDIUM. The phase-5d apt closure (sshd, git, openssh-client)
already passed Trivy; pandoc is a meaningful surface increase. No
breakage today, but no documented response either.

**Fix:** Add to `deploy/docker/README.md`:
- "When Trivy fails on a base-image CVE: `docker build --pull` to
  refresh apt, OR add `--no-progress` `--ignore-policy=...` allowlist
  for known false positives."
- Or: switch the OS scan to non-blocking (`exit-code: "0"`) and
  upload SARIF (deferred to phase 10 in the workflow comment). This
  is the supply-chain-mature path.

Also add the `--severity HIGH,CRITICAL` filter explicitly to the
language scan so a future numpy/cffi noise doesn't waste a triage
session.

---

### MEDIUM

#### DM-1 — `libpango-1.0-0` is correct but `libpangoft2-1.0-0` may be redundant

**Location:** `deploy/docker/Dockerfile:144-149`.

**Description:** Bookworm package names verified via debian.org:
- `libpango-1.0-0` ✓ (Pango type render lib).
- `libpangoft2-1.0-0` ✓ (Pango FreeType integration).
- `libharfbuzz-subset0` ✓ (HarfBuzz subset library — NEW since
  WeasyPrint 53; PDF font subsetting).
- `fonts-dejavu-core` ✓ (DejaVu base — Cyrillic glyph coverage).
- `pandoc` ✓ (bookworm 2.17.1.1).

The `libpangoft2-1.0-0` may be implicitly pulled by `libpango-1.0-0`
on bookworm (`apt-cache depends libpango-1.0-0`). If so, listing both
is harmless redundancy; if not, listing both is correct. Worth
verifying via `docker run --rm 0xone-test:ci dpkg -l | grep pango`
post-build.

WeasyPrint v63+ dependency claim that `libcairo2` and
`libgdk-pixbuf2.0-0` are NOT needed is correct per WeasyPrint upstream
docs — v53 dropped cairo direct linkage in favour of a private cffi
binding. **However**, libcairo2 may still be transitively pulled via
`libpangocairo-1.0-0` if it's in your dependency tree. The
Dockerfile doesn't apt-install `libpangocairo`; if WeasyPrint imports
it via dlopen, you'd see a runtime error. The build-time `import
weasyprint` smoke catches this — ✓.

**Risk:** LOW (build-time RUN catches mismatch).

**Fix:** Strengthen the build-time smoke (DM-2 below).

#### DM-2 — Build-time smoke imports WeasyPrint but does not exercise rendering

**Location:** `deploy/docker/Dockerfile:161-164`.

**Description:** Current smoke:
```dockerfile
RUN pandoc --version \
    && pandoc --list-extensions=markdown-...\
        | grep -E '^-(...)$' \
    && /opt/venv/bin/python -c "import weasyprint; print('weasyprint', weasyprint.__version__)"
```

`import weasyprint` triggers cffi binding load → libpango / libgobject /
libharfbuzz dlopen. If any are missing, fails LOUD ✓. **But** it does
NOT call `HTML(...).write_pdf(...)` — which is the path that exercises
font subsetting (libharfbuzz-subset0) AND the URLFetcher hierarchy
(R1.2 closure). If a future bookworm bump drops `libharfbuzz-subset0`
ABI silently (unlikely but possible), the import succeeds and the
first owner PDF render fails.

The spec §A3 mandates a stronger smoke. Test
`test_phase9_render_doc_binaries.py::test_weasyprint_smoke_probe`
covers this in the **test** target only — not the **runtime** stage.

**Risk:** MEDIUM. Phase-8 ssh-not-found pattern: missing system dep
escapes mocked tests. We have the test in `--target test` but
`--target runtime` doesn't run it.

**Fix:** Extend the runtime RUN to actually render a probe PDF:
```dockerfile
RUN pandoc --version \
    && pandoc --list-extensions=...| grep -E '^-(...)$' \
    && /opt/venv/bin/python -c "
import weasyprint
from io import BytesIO
buf = BytesIO()
weasyprint.HTML(string='<p>Привет</p>').write_pdf(target=buf)
data = buf.getvalue()
assert data.startswith(b'%PDF-'), data[:32]
assert b'%%EOF' in data[-256:]
print('weasyprint probe ok', len(data))
"
```

Adds <100ms to build, catches every dlopen + font-subset failure mode.

#### DM-3 — Audit log path lives in `/run/` subdir but no tmpfs concern

**Location:** `src/assistant/render_doc/subsystem.py:140`,
`docker-compose.yml:67` (volume mount).

**Description:** Audit path is
`<data_dir>/run/render-doc-audit.jsonl`. `<data_dir>` =
`~/.local/share/0xone-assistant/`. Bind-mounted from host, NOT a
tmpfs. Phase 8's vault audit lives in the same `run/` family. Container
runs as `bot` (UID 1000); host dir created by VPS `0xone` (also UID
1000) per `deploy/docker/README.md` prereqs. ✓

The `run/` naming might mislead an operator into thinking it's a
tmpfs. Worth a one-line comment in the README:
> `~/.local/share/0xone-assistant/run/` — durable audit logs (NOT
> tmpfs, despite the directory name).

**Risk:** LOW. Documentation-only.

#### DM-4 — Audit log rotation pruning races with concurrent writes

**Location:** `src/assistant/render_doc/audit.py:113-126`.

**Description:** The rotation flow is:
1. `os.replace(path, rotated)` — atomic.
2. `_list_rotated_siblings(path)` — iterates parent dir.
3. Prune siblings beyond `keep_last_n`.
4. Open fresh file, append.

If two `render_doc` @tool invocations land within the same `await`
slot (concurrency=2), both can hit the size threshold simultaneously
and try to rotate. Outcomes:
- Both call `os.replace`. The second `os.replace` overwrites the
  first's rotated file (same `<YYYYMMDD-HHMMSS>` stamp because
  `microsecond=0`). The first's audit row is **lost**.

The spec §audit acknowledges this: "two rotations within the same
second collide; in practice, audit volume is well under 1/s and the
tie is resolved by `os.replace` overwriting any prior file with the
identical timestamp." Acceptable trade-off for an audit log used as a
durability floor (not the sole source of truth — `log.info` events are
still emitted to stderr/json-file driver).

**Risk:** LOW (acknowledged) but flagged because operator running a
post-incident audit needs to know "missing JSONL row ≠ missing
render — cross-check structlog."

**Fix:** Document explicitly in `deploy/docker/README.md` audit
section. Or add `microsecond` to the rotation stamp + tiny `+f"-{os.getpid()}"`
to handle the concurrency-2 collision.

#### DM-5 — Force-disable check uses `shutil.which("pandoc")` but doesn't verify pandoc version

**Location:** `src/assistant/render_doc/subsystem.py:173`.

**Description:** Boot check verifies pandoc is on PATH. Doesn't verify
pandoc version meets the markdown-variant-subtraction floor. Bookworm
ships 2.17.1.1; pandoc < 2.10 doesn't support `--list-extensions=`
with subtraction syntax in the form we use.

**Risk:** LOW (we control the bookworm pin). HIGH if someone ever
runs the container on a non-bookworm host that has older pandoc.

**Fix:** Add a runtime version check on first import:
```python
result = subprocess.run(["pandoc", "--version"], capture_output=True, text=True)
# parse "pandoc 2.17.1.1" -> tuple
# require >= 2.10
```

Or just skip — the `--target test` smoke covers this and bookworm pin
is solid.

#### DM-6 — `mark_orphans_delivered_at_shutdown` runs AFTER `_render_doc_pending` drain — ordering risk

**Location:** `src/assistant/main.py:1118-1125`.

**Description:** Sequence:
1. `await asyncio.wait(render_pending, timeout=20s)` — drain renders.
2. `self._render_doc_pending.clear()`.
3. `mark_orphans_delivered_at_shutdown()` — flip ledger `in_flight=False`.

If a render task was draining and got cancelled at step 1's timeout,
the @tool body raises `CancelledError`. The body's `finally`
(if any) writes nothing to the ledger because the artefact was never
registered (registration happens only after successful render). So
the ledger has stale `in_flight=True` records ONLY for renders that
completed BEFORE the drain timeout but whose handler crashed
mid-delivery.

Step 3 then flips them all. Looks correct. But: the `mtime` cleanup at
next boot relies on `mtime > 24h` (default). A successful but
undelivered artefact has fresh mtime (just-rendered), so it survives
the next boot's cleanup and stays on disk for **24 hours after the
crash**.

**Risk:** LOW. The artefact is owner-only (Telegram document), no
multi-tenant disclosure risk. Sweeper picks it up on next boot once
TTL=600s is past delivered_at — but `delivered_at` is now set to
`time.monotonic()` in `mark_orphans_delivered_at_shutdown`, which is
**lost across process restart**. Next boot's sweeper sees
`delivered_at` of zero (record gone — ledger is in-memory). So
artefact lingers until 24h boot reaper.

**Fix:** Either:
- Persist ledger to disk at shutdown (overkill for ephemeral artefacts).
- Tighten boot reaper threshold from 24h to `artefact_ttl_s + 60s`
  (~11 minutes) — caps lingering orphan window.
- Document the 24h orphan window in README.

#### DM-7 — Daemon.stop drain budget validator is opt-in, not enforced

**Location:** `src/assistant/config.py:472-486`.

**Description:** Validator only fires when `render_drain_timeout_s` is
EXPLICITLY set in env. Default 20s bypasses the validator (intentional
per W2-MED-3). An owner who sets `RENDER_DOC_RENDER_DRAIN_TIMEOUT_S=10`
gets validator rejection. An owner who sets `=0` (no-drain opt-out)
passes. An owner who sets `=20` (matches default) — also passes
because matches `model_fields_set` lookup.

The validator does NOT cross-check `render_drain_timeout_s +
vault_sync.drain_timeout_s + adapter_stop_timeout_s <=
host's docker stop_grace_period`. There's no env hook to surface
`stop_grace_period` (it's a compose-level config). **The boundary is
external to the Python config.**

**Risk:** MEDIUM. Operator runbook MUST document this. Currently
doesn't.

**Fix:** Add `Daemon._validate_drain_budget` at start that:
1. Reads `STOP_GRACE_PERIOD_S` from env (compose can set this).
2. Computes worst-case drain.
3. Logs `WARNING` if cumulative > stop_grace_period (don't fail
   start — owner may know better).

Compose-side: surface the value:
```yaml
environment:
  STOP_GRACE_PERIOD_S: 35  # compose stop_grace_period mirror
```

#### DM-8 — Multi-arch (arm64) deferred, comment now misleading

**Location:** `.github/workflows/docker.yml:8-9, 100-102`.

**Description:** Comment says "arm64 reopens in phase 9". Phase 9 did
not reopen it. Comment now lies. The reasons (PyStemmer arm64 wheel
absence; qemu compile speed) may still hold but should be re-verified.

**Risk:** LOW (single-arch is fine for current owner). Future migration
to arm64 VPS / Apple silicon dev needs a fresh research session.

**Fix:** Update comment in `docker.yml` to "arm64 deferred to phase
10+" OR run a quick `docker buildx ls` + `docker manifest inspect
ghcr.io/c0manch3/0xone-assistant:phase8` to confirm the situation, then
update.

---

### LOW

#### DL-1 — Audit log path concatenation uses `with_suffix`, edge case on dotfiles

**Location:** `src/assistant/render_doc/audit.py:117`.
`rotated = path.with_suffix(path.suffix + f".{stamp}")` — if path is
`render-doc-audit.jsonl`, suffix is `.jsonl`, rotated becomes
`render-doc-audit.jsonl.20260502-143000`. Correct. If audit ever moves
to a path with multiple dots, `.with_suffix` only affects the last;
prior to phase-9 conventions this is fine.

#### DL-2 — `_list_rotated_siblings` doesn't filter on date-stamp regex

**Location:** `src/assistant/render_doc/audit.py:64-79`.
Filters by `name.startswith(prefix)` where prefix is
`render-doc-audit.jsonl.`. Anything matching is treated as a rotated
sibling. A typo'd `render-doc-audit.jsonl.bak` user-created file
gets pruned by `keep_last_n` policy. Low-risk because nobody touches
this dir manually, but worth a regex match `r"\.\d{8}-\d{6}$"` for
defense.

#### DL-3 — Healthcheck doesn't verify render_doc subsystem state

**Location:** `deploy/docker/docker-compose.yml:149-161`.
Healthcheck verifies daemon PID alive, not render_doc subsystem
status. If render_doc force-disables silently after a base-image apt
update (pandoc removed, weasyprint cffi binding broke), healthcheck
stays green. Owner discovers via "render PDF, fails" complaint chain.

**Fix:** Add an optional second healthcheck or a `/health` endpoint
that includes subsystem status. Out of scope for phase 9 but worth a
phase-10 ticket.

#### DL-4 — `RENDER_DOC_ENABLED=false` rollback path not explicitly tested

**Location:** Rollback plan not documented.
If phase 9 deploys break (e.g. weasyprint-69 hits PyPI before pin
review), owner expects to flip `RENDER_DOC_ENABLED=false` in
`~/.config/0xone-assistant/.env` and restart. Subsystem won't construct
(`if self._settings.render_doc.enabled` is False at `main.py:446`). ✓
Tool stays unregistered. The rest of phase-1..8 traffic continues.

But: no integration test asserts this path. There's a setting-level
unit test (`test_phase9_render_doc_settings.py`) but no
`Daemon.start with enabled=False asserts subsystem is None` test.

**Fix:** Add to `test_phase9_subsystem_force_disable.py` a parametrised
case with `enabled=False`.

#### DL-5 — `pandoc_sigterm_grace_s + pandoc_sigkill_grace_s = 10s` doesn't account for `wait_for` overhead

**Location:** `src/assistant/render_doc/_subprocess.py:86-101`.
SIGTERM grace 5s + SIGKILL grace 5s = 10s. `render_drain_timeout_s` =
20s. Validator (`config.py:488-496`) rejects grace_sum > drain. Fine.
But `asyncio.wait_for(proc.communicate(), timeout=timeout_s)`'s
internal cancel + cleanup adds ~50-100ms — negligible but unbounded
under stress. Not a fix; just a note.

#### DL-6 — No TTL race protection on artefact delivery

**Location:** `src/assistant/render_doc/subsystem.py:258-270`.
If `mark_delivered` is called more than `artefact_ttl_s + sweep_interval_s`
(660s default) AFTER `register_artefact`, the sweeper may race ahead
and `unlink` the file before the handler reads it. `in_flight=True`
guards the sweeper (it only deletes `not in_flight` records), so the
race exists only if the handler took >10 minutes between
`register_artefact` and `send_document`. Not realistic for phase 9
but worth a comment.

---

## Image size projection

Bookworm `apt-cache show` Installed-Size sums (kB → MB rounded):

| Package | Installed-Size (kB) | Notes |
|---------|---------------------|-------|
| `pandoc` | ~155,000 | Haskell binary; biggest single delta. |
| `libpango-1.0-0` | ~600 | Pulls libgobject2.0-0 (~5 MB), libfreetype6 (~700 kB) transitively. |
| `libpangoft2-1.0-0` | ~120 | Pango FreeType. |
| `libharfbuzz-subset0` | ~700 | HarfBuzz subset library; pulls libharfbuzz0b ~1.8 MB transitively. |
| `fonts-dejavu-core` | ~1,400 | Single .ttf file. |
| **Direct sum** | **~158 MB** | |
| **Transitive (estimated)** | **~30-50 MB** | libgobject, libffi, libgcc-s1 (already in slim), libnuma1, libgmp10. |
| **Total apt delta** | **~190-210 MB** | Within spec ≤250 MB budget. |

Plus weasyprint pip wheel: ~0.7 MB pure-Python (cffi bindings to system
libs, no compiled wheel).

**Image size projection:** Phase 5d = ~400 MB amd64. Phase 9 estimated
**~590-610 MB**. Confirm with:
```bash
docker images ghcr.io/c0manch3/0xone-assistant:test-rc1 --format '{{.Size}}'
```

**Recommendation:** Run `docker history` on first build to identify any
unexpectedly large layers; commit the result in the PR description so
future phases have a baseline.

---

## Build-time verifications

Verifications that already exist in `deploy/docker/Dockerfile`:
- ✓ `pandoc --version` (catches missing binary).
- ✓ `pandoc --list-extensions=markdown-...` regex grep (catches
  variant-subtraction silent-no-op trap, R-Pandoc).
- ✓ `import weasyprint` (catches cffi/Pango/HarfBuzz/libgobject
  dlopen failures).
- ✓ Bundled claude binary `--version` (phase-5d invariant).
- ✓ pillow-heif HEIF plugin registration (phase-6b invariant).

Verifications missing — recommended:
- **Render probe PDF in build-time RUN** (DM-2 above):
  `weasyprint.HTML(string='<p>Привет</p>').write_pdf(...)`. Catches
  font-subsetting failures and Cyrillic glyph absence.
- **Pandoc render to PDF via WeasyPrint pipeline** end-to-end:
  ```dockerfile
  RUN echo '# Hello' | pandoc -t html5 | /opt/venv/bin/python -c "
  import sys
  import weasyprint
  weasyprint.HTML(string=sys.stdin.read()).write_pdf('/tmp/probe.pdf')
  print('e2e ok')
  "
  ```
- **Image size budget gate** (DH-1 above) — separate CI job.

---

## Top-3 fix-pack priorities

### 1. Update operator runbook + bump `stop_grace_period` (DC-1)

**Owner action:**
- Bump `deploy/docker/docker-compose.yml:104` to `stop_grace_period:
  180s`.
- Add a "Phase 9 — render_doc operations" section to
  `deploy/docker/README.md` covering: drain budget, SIGKILL trade-off,
  `RENDER_DOC_ENABLED=false` rollback recipe, audit log location +
  rotation policy, pandoc CVE triage flow.

**Why first:** Spec mandates it (W2-HIGH-1 closure §10). Without it,
the cumulative-drain-budget math is broken in production.

### 2. Strengthen build-time WeasyPrint probe + add image-size gate (DM-2 + DH-1)

**Owner action:**
- Extend the `RUN pandoc --version && ...` block in Dockerfile runtime
  stage to actually exercise `HTML.write_pdf(...)` against a Cyrillic
  string + `%PDF-` magic check.
- Add a `size-budget` job to `.github/workflows/docker.yml` that fails
  CI if image > 650 MB.

**Why second:** Catches the next class of silent-deploy-break.
Build-time fail-loud is the cheapest insurance against the phase-8
ssh-not-found pattern. Image-size gate prevents silent drift.

### 3. Add `LC_ALL` to pandoc env + Cyrillic round-trip CI test (DH-4)

**Owner action:**
- Add `LC_ALL` to `_pandoc_env()` in `_subprocess.py:54-58`.
- Add `tests/test_phase9_pdf_renderer_cyrillic_round_trip.py` that
  renders `# Привет, мир` to PDF and verifies extracted text via
  `pikepdf` matches.

**Why third:** Phase-6c had a Cyrillic regression that mocks missed.
Audio path (transcription) was Cyrillic-aware end-to-end; render path
should match.

---

## Positive observations

- Build-time RUN block is exemplary — combining `pandoc --version`,
  variant-subtraction grep, and `import weasyprint` catches every
  dlopen failure mode and the silent-no-op pandoc trap. Phase-5d
  bundled-claude smoke + phase-6b pillow-heif smoke patterns
  consistently applied. **This is the gold standard.**
- `_artefacts_lock` discipline (W2-HIGH-2) is correctly implemented:
  snapshot under lock, I/O outside, pop under lock.
- `_pending_set` integration with `Daemon.stop` drain is the same
  shape as phase-8 vault_sync — clean, consistent, no new abstractions.
- `force_disabled` boot path falls back gracefully + emits owner
  notify with timeout shield. Mirrors phase-8 F9. ✓
- Pandoc env scoping (`PATH/LANG/HOME` only, no token leakage)
  follows the principle of least privilege correctly.
- Audit JSONL with `schema_version: 1` future-proofs the format —
  good.
- RSS observer integration (`render_doc_inflight=N`) at line 837-840
  follows the phase-6e pattern. Sweeper supervisor respawn (3/hour
  cap) is correct for fault tolerance.
- Boot reaper unconditional `.staging/` wipe (MED-4 closure) is the
  right call — staging files are by definition transient.

---

## Acknowledged residual risks (not blocking)

- WeasyPrint thread orphan on `Daemon.stop` mid-render — accepted per
  spec §2.12 (iii). Documented.
- Pandoc CVE noise from Trivy — can be triaged on first occurrence;
  no preemptive action required.
- arm64 build deferred — single-user single-VPS, owner accepted.
- Audit log rotation collision under concurrency=2 within same second
  — acknowledged; structlog stream is durability floor.
