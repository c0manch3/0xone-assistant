# Phase 4 DevOps / Ops Review

Scope: operational readiness of the newly-shipped long-term memory subsystem
for `0xone-assistant` (single-user Telegram bot daemon, local macOS
workstation deploy, OAuth CLI session, no cloud). Review methodology:
read-only static analysis against the files listed in the review prompt,
cross-checked against on-disk runtime state under
`~/.local/share/0xone-assistant/` and `~/.config/0xone-assistant/`.

Intentionally out-of-scope (already covered elsewhere): H1/H2/H3/H4/M1-M10
in `review-code.md`; C3-W3/C4-W3/H4-W3/H5-W3/H6-W3/H7-W3/M4-W3/M5-W3 in
`devil-wave-3.md`. Where an ops concern overlaps a prior finding I add
operational context only.

## Executive summary

The memory subsystem is **operationally functional but not yet operationally
owned**. Core correctness (atomic write, fcntl lock, FTS5 transactional
semantics, cloud-sync detection) is solid. What is missing is the usual
boring ops scaffolding that turns a correct piece of code into a serviceable
one: documented env-var surface, a runbook, structured logging that an
operator can grep, and a plan for data layout that future phases (git
backup, scheduler) will not trip over. For a single-user bot on the
owner's own machine these gaps do not block a deploy; they do raise the
probability that the owner eventually pages themselves at 3 AM and has
nowhere to look first.

## Ops-readiness verdict

**Needs-polish.** Ship phase 4 after a small doc + visibility pass: env-var
documentation in `.env.example`, a 1-page `docs/runbook-memory.md` (or
equivalent) that tells the owner how to inspect/repair the vault, and one
boot-time INFO log that prints the effective memory paths so the first
boot leaves an obvious trail. No code changes required for ops acceptance
— everything material has already been fenced by the code review + devil
wave-3.

---

## Findings by category

### 1. Startup sequence reliability

**OPS-1 (LOW) — boot order is correct, but `configure_memory` can still
kill the daemon with an unhelpful traceback.** The boot sequence in
`main.py:86-138` is sound: preflight `claude` CLI → refuse custom
`.claude/settings*.json` → ensure skills symlink → `configure_installer`
→ `configure_memory` → bg sweeps → sqlite open → `ClaudeBridge` →
`TelegramAdapter`. Ordering is intentional (memory must be configured
before `ClaudeBridge` imports `MEMORY_SERVER`). However, `configure_memory`
synchronously runs `vault_dir.mkdir` → `_fs_type_check` (subprocess) →
`_ensure_index` (sqlite) → `_maybe_auto_reindex` and bubbles every `OSError`
straight up. If the owner's vault lives on a disk that just filled, the
daemon aborts with a stack trace rather than a structured hint. This is
already noted as H5-W3 in wave-3; from an ops angle add INFO-level
breadcrumbs around each sub-step (`memory_vault_init`, `memory_index_ready`,
`memory_auto_reindex_evaluated`) so the owner can see which one died.

**OPS-2 (LOW) — boot-time cost measured against observed vault.** The
owner's vault at `~/.local/share/0xone-assistant/vault` currently holds
1 `.md` (`_index.md`) plus three subdirectories (blog, inbox, projects).
Coder's `seed_vault_copy` fixture implies 12 real notes in an earlier
session, but the live state is far below the 2000-note auto-reindex cap.
Boot cost today is dominated by `_preflight_claude_auth` (up to 45 s
timeout, typically 1-3 s) plus `/sbin/mount` (5 s worst case, see H7-W3).
Memory config itself is <100 ms on this vault. **Future risk:** at the
prescribed 2000-note ceiling, `_scan_vault_stats` (one `stat` per note)
plus a potential `reindex_vault` (read+yaml.load per note) is the
expensive path. Wave-3 M4-W3 already recommends moving the auto-reindex
to `_spawn_bg`; I second that recommendation strictly on operational
grounds — any work that can be done after `TelegramAdapter.start()` should
be, because the first inbound Telegram message is the owner's primary
liveness signal.

**OPS-3 (MEDIUM) — no singleton guard.** There is no pidfile / flock on
the daemon process itself. Two `assistant` processes on the same vault is
possible (systemd-style restart overlap, terminal tab mistake). The
vault-level `flock` serialises individual writes but does NOT prevent
cross-process audit-log duplication or SDK session duplication against
the same `assistant.db`. `aiosqlite` + WAL will generally survive, but
it is still an undefined-behaviour corner. Mirrors H6-W3; call out here
because it is strictly a daemon-wide problem, not a memory problem.
Recommend adding a `<data_dir>/run/daemon.pid` + `fcntl.flock` acquisition
in `Daemon.start()` before `_preflight_claude_auth` runs — fail-fast if
another daemon already owns the data dir.

**OPS-4 (INFO) — `LOCK_NB` semantics at boot are correct.** Wave-3 raised
doubt about the non-blocking auto-reindex lock path; the implementation
in `_maybe_auto_reindex` (`_memory_core.py:846-854`) uses
`blocking=False` and catches `BlockingIOError` to a warning
(`memory_auto_reindex_skipped_lock_contention`). A stale `.lock` file
left by a SIGKILL'd daemon does NOT cause a hang because fcntl advisory
locks are released by the kernel on fd close, and the helper re-opens a
fresh fd on every call (confirmed by `test_memory_lock_released_after_kill`).
No change needed.

### 2. Configuration + secrets

**OPS-5 (MEDIUM, doc) — the owner-facing env surface is undocumented.**
Three memory-related env vars are wired end-to-end but invisible to the
owner:
- `MEMORY_VAULT_DIR` (override default `<data_dir>/vault`)
- `MEMORY_INDEX_DB_PATH` (override default `<data_dir>/memory-index.db`)
- `MEMORY_MAX_BODY_BYTES` (default `1048576` = 1 MiB)
- `MEMORY_ALLOW_LARGE_REINDEX` (opt-out of 2000-note auto-reindex cap)

`.env.example` enumerates Telegram + `CLAUDE_*` knobs but zero `MEMORY_*`.
If the owner later wants to point at their real Obsidian vault, there is
nothing in the repo (except deep plan docs) that tells them the knob
exists. **Fix:** add a commented `# -- Memory --` block to `.env.example`
listing all four. Cost: 8 lines of markdown; value: self-service
reconfig.

**OPS-6 (LOW) — no `ANTHROPIC_API_KEY` leakage introduced.** Verified:
`config.py` still forbids API-key auth at the `Claude`/`Memory` model
layers, and no new secret shows up in either `Settings` or
`MemorySettings`. `_bash_allowlist_check` + `BASH_SLIP_GUARD_RE` in
`hooks.py` continue to deny Bash reads of `.env` / `.ssh` / `.aws` /
literal `ANTHROPIC_API_KEY`. Unchanged from phase 3 — called out
positively.

**OPS-7 (LOW) — XDG path handling is correct but silently falls back.**
`_default_config_dir` / `_default_data_dir` respect `XDG_CONFIG_HOME` /
`XDG_DATA_HOME` when set, else `~/.config` / `~/.local/share`. The
owner's `~/.config/0xone-assistant/` directory does not exist on disk
today (empty `ls`); the bot is reading `.env` from the repo CWD per the
`Path(".env")` fallback. This works but means the "canonical" config
location is not yet populated — easy to cause confusion on the next
machine. **Recommendation:** first-boot INFO log `env_file_resolved=/path`
so the owner knows which file won.

### 3. Persistent state on disk

**OPS-8 (MEDIUM) — `.gitignore` covers the common traps but not
`.pre-wipe-backup-*/`.** The new exclusions in `.gitignore:42-46`
properly cover `memory-index.db*` + `memory-audit.log`. But the actual
data_dir also contains `.pre-wipe-backup-20260421-172516/` (32K, 7 files).
If a future automation step ever reaches into `<data_dir>` for a git
init (phase 8), the backup dir will be committed. It is a risk for
phase-8 scope, not phase-4; flagging now so the backup handler can
explicitly exclude `**/.pre-wipe-backup-*/`.

**OPS-9 (MEDIUM) — there is no vault-local `.gitignore` template.**
Phase-8 (deferred per CLAUDE.md) will introduce daily git push of the
vault. When that lands, the vault MUST ignore its own `.tmp/` subdir
(atomic-write staging, `_memory_core.py:248-277`). A stray SIGKILL
between `tf.flush()` and `os.replace` leaves `.tmp/.tmp-XXXX.md`
orphans that would be committed and pushed. Wave-3 M5-W3 already calls
this out on phase-5+ prerequisites; from an ops angle my specific ask
is: **ship a one-line `<vault>/.gitignore` (`.tmp/`) at
`configure_memory` time**, even now, so the vault is pre-prepared for
the eventual phase-8 flip.

**OPS-10 (LOW) — on-disk layout matches plan; paths are absolute
everywhere.** `Settings._resolve_absolute` (N2 validator) ensures both
`project_root` and `data_dir` are `.expanduser().resolve()` at
construction. `vault_dir` / `memory_index_path` re-apply
`.expanduser().resolve()` in their properties. No accidental relatives.

### 4. Audit log hygiene

**OPS-11 (MEDIUM, dup of H2 code-review) — `memory-audit.log` writes
with default umask.** `hooks.py:700` opens the file with a plain
`audit_path.open("a", ...)`. On macOS with default umask `022` the
resulting permissions are `0o644`. For single-user this is low-severity;
it becomes non-hypothetical the moment a second human account exists on
the laptop. Fix in the code-review fix-pack (`os.chmod(..., 0o600)` on
first create, or `os.umask` context). Applies equally to
`memory-index.db` / `.db-wal` / `.db-shm` / `.lock` — all are created
by stdlib sqlite / `os.open` with default mode `0o644`, which leaks
every note body to any reader on the host. Reminder: this is already
called out; reiterating the **set of files** (not just the audit log)
because the code-review only explicitly named the audit path.

**OPS-12 (MEDIUM) — audit log volume projection.** The file currently
does not exist at all on the owner's disk (`find ... memory-audit.log`
returned nothing — no memory ops yet). With body compaction fix (H1
code-review + C4-W3 wave-3 applied), expected per-entry size is
200-500 bytes. At an upper bound of ~50 memory ops/day, one year is
~9 MB — trivial. WITHOUT the compaction fix, a single `memory_write`
of 1 MiB body JSON-encodes to ~1 MiB + escaping overhead. Under an
agent gone-wild scenario (model decides to save 50 notes of 1 MiB each
in a single turn) the log fills 50 MB in seconds. This is why the
fix-pack is non-negotiable before owner smoke, not a phase-9 problem.

**OPS-13 (LOW) — audit log is JSONL; replay-able but schema is
implicit.** Entries are `{ts, tool_name, tool_use_id, tool_input,
response: {is_error, content_len}}`. No schema version. When phase-9
adds rotation + a consumer, the parser will have to infer fields.
Recommendation: add `"v": 1` to every entry now so future consumers
can handle schema evolution without sniffing. Cost: 12 bytes per line.

### 5. Observability

**OPS-14 (MEDIUM) — structured logging exists but memory subsystem
uses stdlib `logging.Logger`, not structlog.** `_memory_core._LOG =
logging.getLogger(__name__)` — writes via `_LOG.info(..., extra={...})`.
The rest of the app uses `assistant.logger.get_logger` which returns a
structlog `BoundLogger` with JSON output. Mixing the two means memory
log lines are **not guaranteed to be JSON-formatted** when the root
logger's structlog handler is installed — whether they are depends on
how `setup_logging` wires the root logger. At minimum the operator
cannot reliably grep `"event":"memory_auto_reindex_done"` if some
entries end up as text formatted lines. **Recommendation:** swap
`logging.getLogger(__name__)` for `get_logger("tools_sdk.memory_core")`
so all memory events travel the structlog pipeline. Low-effort; high
grepability win.

**OPS-15 (MEDIUM) — no "memory ops in the last N minutes" view
besides tailing the audit log.** Owner-facing observability for "model
said saved but no file exists" is currently (a) tail `memory-audit.log`
and (b) `ls vault/inbox/`. There is no aggregated counter, no CLI, no
`structlog` span. For a personal bot this is acceptable; for a
runbook-driven diagnostic it is thin. **Not a blocker** — call out in
the runbook so the owner knows the two surfaces.

**OPS-16 (LOW) — memory log event names are namespaced inconsistently.**
`_memory_core.py` mixes `memory_*` prefixed events (`memory_reindex_done`,
`memory_vault_unsafe_fs`, `memory_auto_reindex_done`,
`memory_auto_reindex_skipped_lock_contention`,
`memory_vault_too_large_for_auto_reindex`, `memory_vault_cloud_sync_path`,
`memory_vault_unrecognized_fs`, `memory_vault_fs_type_unknown`) with
`hooks.py`'s `memory_audit_write_failed`. That is fine. But elsewhere in
the app event names are lowercase with underscores (`pretool_decision`,
`sdk_init`, `query_start`, `result_received`) — all singular. Memory is
consistent internally; there is no collision. **Commendation**: event
tags are informative and greppable.

### 6. Backup / disaster recovery

**OPS-17 (MEDIUM, doc) — DR story works but is nowhere written down.**
Truth on the ground:
- `vault/` is flat-file `.md` — trivially rsyncable + Time-Machine-safe.
- `memory-index.db` is derivable from `vault/` via `memory_reindex()`.
- `assistant.db` (conversation store from phase 2) has its own backup
  story, out of phase-4 scope.
- `memory-audit.log` is observational — no DR value beyond forensics.

None of this is documented in a runbook, a README section, or an
`--help` output. The first time the owner has to recover, they will be
reading this review as their runbook. **Recommendation:** ship a
1-page `docs/runbook-memory.md` as part of the phase-4 fix-pack. Minimum
content: (a) where files live, (b) how to back up (rsync example),
(c) how to recover index loss (run `memory_reindex` via a short-term
`memory` Skill or direct REPL), (d) env-var override quick-reference.

**OPS-18 (LOW) — no `PRAGMA integrity_check` on index boot.**
`_ensure_index` creates the DB and schema but does not validate an
existing DB. Hardware-corrupted index would manifest as
`sqlite3.DatabaseError` on the first search. Owner would not see it
until they searched. Given the index is fully derivable, a cheap fix
is: on boot, `PRAGMA quick_check` → on failure log `WARNING
memory_index_integrity_failed` + trigger a full `reindex_under_lock`.
Nice-to-have; not critical.

### 7. Tests as ops examples

**OPS-19 (LOW) — `conftest.py` has the right safety rail for
writes.** The `memory_ctx` fixture asserts `not
str(tmp_path).startswith(os.path.expanduser("~/.local/share/0xone-assistant"))`
— a defensive guard that prevents a mis-typed `tmp_path_factory`
fixture from trashing the owner's real vault. Good pattern, worth
copying for every future fixture that writes to disk.

**OPS-20 (LOW) — `seed_vault_copy` fixture depends on owner's real
vault.** `conftest.py:68-81` reads from
`~/.local/share/0xone-assistant/vault`. If the seed is absent the
test `pytest.skip`s — correct fallback. BUT: running the test suite on
CI would silently skip the seed-driven tests, hiding regressions.
**Recommendation:** ship a minimal seed vault under `tests/fixtures/`
and prefer it when the owner's vault is missing. Phase-4 scope is
single-user local; fine to defer if CI is out of scope.

**OPS-21 (LOW) — `_detect_fs_type` subprocess tests are
Darwin-only.** `test_memory_core_fs_type.py` monkeypatches `os.uname`
to return `Darwin` and stubs `subprocess.run`. The Linux branch of
`_detect_fs_type` (`stat -f -c '%T'`) has no test coverage. For the
owner's machine (arm64 Darwin 24.6) this is fine; if a future phase
runs this on Linux CI, the Linux branch is untested and likely to
fail on first contact. Low priority.

**OPS-22 (LOW) — flock tests work rootless and on tmpfs-free
systems.** `test_memory_core_vault_lock.py` uses `tmp_path` (pytest
fixture → APFS on owner's machine; ext4 on most Linux CIs; no tmpfs).
`fcntl.flock` is advisory and works on both. Should run fine in
rootless containers. No action.

### 8. Dependency supply chain

**OPS-23 (LOW) — PyStemmer wheel is present and loads correctly.**
Verified:
- `pyproject.toml` pin: `PyStemmer>=2.2,<4`
- Resolved: `pystemmer 3.0.0` (wider than the devil-wave-2 note which
  said `>=2.2,<4`; the `<4` upper bound is intact).
- Wheel installed as
  `.venv/lib/python3.12/site-packages/Stemmer.cpython-312-darwin.so`
  (Cython-compiled native extension, 956 KB).
- `import Stemmer` succeeds on arm64 Darwin.

**OPS-24 (LOW) — no graceful fallback if the wheel is missing or
ABI-incompatible.** `_STEMMER = Stemmer.Stemmer("russian")` runs at
module import (`_memory_core.py:44`). An ABI mismatch (e.g. Python 3.13
upgrade without wheel refresh) would raise `ImportError` at
`import assistant.tools_sdk.memory`, propagate through `ClaudeBridge`
init, and crash the daemon. The review prompt asked explicitly; M9 in
code-review flagged this. Ops perspective: the failure mode is not a
silent bad search — it is a refused-to-boot daemon. That is actually
preferable to degraded search behind the owner's back. Leave as-is.

**OPS-25 (LOW) — no other new deps smuggled in.** Diffed phase-4
`pyproject.toml` against phase-3 expected set. Only addition is
`PyStemmer`. `aiogram`, `pydantic`, `pydantic-settings`, `aiosqlite`,
`structlog`, `claude-agent-sdk`, `pyyaml` are unchanged. Dev deps
unchanged.

### 9. Phase-5+ ops readiness

**OPS-26 (MEDIUM) — `configure_memory` is callable from any asyncio
task, but is NOT re-entrant with a changed vault path.** Phase-5
(scheduler) may fire turns from cron, writing memory from non-main
asyncio contexts. The `@tool` handlers (`memory_search`,
`memory_write`, ...) wrap the blocking helpers via
`asyncio.to_thread`; they are safe in any task. BUT: if phase 5 ever
decides to reconfigure memory (e.g. user swaps vaults via a new CLI),
`configure_memory` will **raise `RuntimeError`** on a different
`vault_dir`/`index_db_path` (`memory.py:62-69`). That is the right
behaviour — silently swapping would lose notes (U2 in wave-3) — but
callers need to know. Document it in the runbook: "restart daemon to
change vault path."

**OPS-27 (MEDIUM) — phase-7 daily git commit vs live `.tmp/`
files.** When phase-7 introduces a scheduled `git add vault/ && git
commit`, a concurrent `memory_write` mid-`atomic_write` leaves
`<vault>/.tmp/.tmp-XXXX.md` on disk. If the commit runs at that
instant, `git add vault/` sweeps it into the index. On restart the
daemon does not clean `.tmp/` (wave-3 M5-W3). Two recommendations
stacked: (a) ship `<vault>/.gitignore` with `.tmp/` entry now (OPS-9);
(b) phase-7 must grab the vault flock before `git add` — same flock
`memory_write` holds, serialising writer vs backup.

**OPS-28 (INFO) — phase-8 deploy key design is still phase-8.**
CLAUDE.md explicitly defers GitHub auth. No work needed in phase 4.
When it lands: SSH deploy key should live in `~/.ssh/` (standard
agent socket) or `~/.config/0xone-assistant/deploy_key` with `0600`
permissions; daemon needs to know the path via a new
`BACKUP_SSH_KEY_PATH` env var. Flag for phase-8 plan.

### 10. Failure modes

**OPS-29 (LOW) — disk-full during `memory_write` is correctly rolled
back.** `write_note_tx` (`_memory_core.py:884-914`) runs
`BEGIN IMMEDIATE` → `INSERT OR REPLACE` → `atomic_write` → stat →
`COMMIT`. `atomic_write` itself runs `NamedTemporaryFile(delete=False)`
→ `write` → `flush` → `fsync`. Disk-full at `fsync` raises `OSError`;
the `finally` branch unlinks the tmp file; outer code catches the
exception and `conn.rollback()`s the pending INSERT. End state: vault
file unchanged, index unchanged, tmp file cleaned. Correct.
Audit log: the `@tool` handler returns `tool_error(CODE_IO)` →
`PostToolUse` hook fires and attempts to `audit_path.open("a")` —
which also fails with `OSError` — caught by `hooks.py:702`
`memory_audit_write_failed` warning. Double-failure is logged, not
hidden. Good.

**OPS-30 (LOW) — vault directory moved/renamed while daemon
running.** `_CTX["vault_dir"]` is cached at `configure_memory` time.
If the owner `mv`s the vault mid-run, every subsequent
`validate_path` still resolves against the old absolute path;
`full.is_file()` returns `False` → `CODE_NOT_FOUND`. No corruption;
just all reads start 404ing. Recovery: restart daemon.
**Recommendation:** one sentence in the runbook to cover this.

**OPS-31 (LOW) — clock skew / NTP corrections can rewind
`updated`.** `memory_write` always sets `updated = now_iso` from
`dt.datetime.now(dt.UTC)`. If NTP corrects the laptop clock backwards,
a subsequent write's `updated` will be older than the prior write's
`updated`. `memory_list` sorts by `updated DESC` — the newer record
would appear BELOW the older one. Single-user acceptable; the owner
would notice and re-save. Non-issue. Flag only for phase-5 scheduler
timestamps.

**OPS-32 (LOW) — `meta('max_mtime_ns')` staleness invariant is
correctly maintained.** `write_note_tx` updates it with `max(cur_max,
new_mtime_ns)`. `delete_note_tx` recomputes via `_scan_vault_stats`.
`_maybe_auto_reindex` trips on either count mismatch OR mtime-forward.
Thus: external Obsidian edit → mtime increases → boot-time
auto-reindex catches it. If count also matched before the edit (same
file, in-place rewrite), the mtime check is what saves us. Well
designed.

---

## Recommended runbook items

The phase-4 fix-pack SHOULD ship a 1-page `docs/runbook-memory.md`
covering at minimum:

1. **Where files live.** Vault: `~/.local/share/0xone-assistant/vault/`
   (or `$MEMORY_VAULT_DIR`). Index:
   `~/.local/share/0xone-assistant/memory-index.db`. Audit log:
   `~/.local/share/0xone-assistant/memory-audit.log`. Lock:
   `~/.local/share/0xone-assistant/memory-index.db.lock`.
2. **Env overrides.** List all four `MEMORY_*` env vars with default
   values, reason to set, and where to put them
   (`~/.config/0xone-assistant/.env`).
3. **Backup procedure.** `rsync -a ~/.local/share/0xone-assistant/vault/
   dest/` (flat files, safe to copy anytime). The index is derivable;
   do NOT back it up. After restore, run `memory_reindex` via chat.
4. **Recovery from corrupt index.** Delete
   `memory-index.db`+`.db-wal`+`.db-shm`, restart daemon — boot
   auto-reindexes from disk.
5. **Recovery from corrupt vault file.** Find the bad `.md`, fix
   frontmatter manually, restart daemon (auto-reindex picks up the
   edit via mtime).
6. **"Bot said saved but file missing."** Tail
   `memory-audit.log` for the `tool_name=mcp__memory__memory_write`
   entry; check `response.is_error`. Then `ls
   ~/.local/share/0xone-assistant/vault/<area>/`.
7. **Vault move / rename.** Edit `MEMORY_VAULT_DIR` in
   `~/.config/0xone-assistant/.env`, restart daemon. The old index
   will auto-rebuild on first boot at new location.
8. **Disk-full recovery.** Free space, restart daemon, prior failed
   write is fully rolled back.
9. **Cloud-sync detection.** On boot, daemon logs
   `memory_vault_cloud_sync_path` at WARNING if vault sits under
   iCloud / Dropbox / Mobile Documents. That fcntl.flock is a no-op
   there. Move the vault to a local APFS directory.
10. **Known env var quick-reference.** `MEMORY_VAULT_DIR`,
    `MEMORY_INDEX_DB_PATH`, `MEMORY_MAX_BODY_BYTES`,
    `MEMORY_ALLOW_LARGE_REINDEX`.

---

## Phase-5+ prerequisites

Before each future phase opens, these ops items should be resolved:

| Phase | Prerequisite | Source |
|-------|--------------|--------|
| 5 (scheduler) | Daemon singleton lock (OPS-3) | OPS-3, H6-W3 |
| 5 (scheduler) | Structlog migration for `_memory_core` (OPS-14) | OPS-14 |
| 5 (scheduler) | Clarify behaviour of `configure_memory` re-call across fork/task boundaries | OPS-26 |
| 7 (git commit) | `<vault>/.gitignore` seeded with `.tmp/` (OPS-9) | OPS-9, wave-3 phase-5+ list |
| 7 (git commit) | Scheduled backup must grab vault flock before `git add` (OPS-27) | OPS-27 |
| 7 (git commit) | Backup-dir exclusion (`.pre-wipe-backup-*/`) (OPS-8) | OPS-8 |
| 8 (gh/git push) | `BACKUP_SSH_KEY_PATH` env var + `0600` perms expectation | OPS-28 |
| 9 (audit rotation) | Audit schema version field (OPS-13) | OPS-13 |
| 9 (audit rotation) | `PRAGMA quick_check` on boot + auto-reindex trigger (OPS-18) | OPS-18 |

---

## Positive observations

- **Cloud-sync warning at boot is a genuine ops win.** Detecting iCloud
  / Dropbox / Mobile Documents path prefixes BEFORE the first silent
  flock-no-op is exactly the kind of defensive guard that saves the
  owner from mystery data loss 3 months in.
- **Atomic-write + transactional index update ordering is correct
  under every failure mode I could stress-test on paper.** Disk-full,
  OS crash during `fsync`, OS crash between `os.replace` and
  `COMMIT`, sqlite DB corruption — each ends with the vault
  filesystem authoritative and the index either in-sync or
  reparable by the next auto-reindex.
- **The `.gitignore` updates for memory artifacts are thorough.** `*.db`
  + `.db-wal` + `.db-shm` + `.lock` + `memory-audit.log` all explicitly
  excluded. Owner won't accidentally commit runtime state.
- **Path-validation defence is thorough.** Symlink rejection BEFORE
  `resolve()`, `..` check, absolute/tilde rejection, `_*.md` MOC
  block. Hard to traverse out of the vault.
- **`conftest.py` safety rail against trashing the real vault** is
  copy-paste-worthy for every future test fixture that writes to disk.
- **Non-blocking boot-time reindex with `LOCK_NB`** prevents daemon
  startup hang on a stuck prior-daemon lock — exactly the ops-friendly
  default.
- **All paths resolved absolute at `Settings` construction time** —
  downstream code never sees ambiguous relatives, preventing an entire
  class of "works in dev, breaks in prod" bugs.
- **No `ANTHROPIC_API_KEY` regression** — OAuth-only auth model
  preserved; Bash slip-guard still denies env/printenv/$ANTHROPIC
  patterns.

---

## Summary table

| Severity | Count |
|----------|-------|
| HIGH     | 0 (code-review + wave-3 already captured the only blockers) |
| MEDIUM   | 8 (OPS-3, OPS-5, OPS-8, OPS-9, OPS-11, OPS-12, OPS-14, OPS-15, OPS-17, OPS-26, OPS-27) |
| LOW      | 13 |
| INFO     | 3 |

Deploy gate: **Needs-polish.** Not blocking on ops grounds alone; the
above code-review/wave-3 fixes + a runbook doc + `.env.example` entries
close the operational gap before owner smoke.
