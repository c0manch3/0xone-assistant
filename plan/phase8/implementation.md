# Phase 8 — Implementation prescription v2

Based on: `description.md` (48d3ccb), `detailed-plan.md` r2 (Q1-Q12 decisions applied, devil wave 1 closed), `spike-findings.md` (10 probes), devil-wave-2-fixes (6 blockers + 20 should-fix closed).

Target: full phase-8 (GitHub CLI + daily vault auto-commit) ready for multi-wave coder execution after v2 fix-pack. Every section below is a concrete prescription for the coder — exact imports, exact exit codes, exact regex, exact SQL.

**Δ v1 → v2 (summary — full details in §Appendix fix-pack changelog):**

- **B-A1** `GitHubSettings.allowed_repos` now `Annotated[tuple[str, ...], NoDecode]` (pydantic-settings 2.3+ feature; 2.13.1 confirmed). Without `NoDecode`, the settings framework JSON-decodes `"a/b,c/d"` BEFORE the validator runs and raises `SettingsError`. [§C1]
- **B-A2** `tools/gh/main.py` is stdlib-first like `tools/schedule/main.py` + `tools/genimage/main.py`: a local `_data_dir()` helper + direct `GitHubSettings()` instantiation (not `get_settings()`), so CLI works when TG tokens are missing (fresh install, manual smoke, cron). [§C2, §C4]
- **B-A3** `_cmd_vault_commit_push` creates `vault_dir` with `mkdir(parents=True, exist_ok=True, mode=0o700)` BEFORE any is-dir / is-git check. Handles fresh install + user-deletes-vault race. [§C4]
- **B-B2** Unpushed-commit detection at loop start: `git rev-list @{u}..HEAD --count` > 0 → retry push-only path (no re-stage, no re-commit). Divergence handler does `git reset --soft HEAD~1` so working tree changes survive exit 7. No silent data loss. [§C4]
- **B-D1** `tools/schedule/main.py cmd_rm` stays **sync `sqlite3`** + **soft-delete** (`UPDATE enabled=0`). Tombstone insert happens in the same sync `BEGIN IMMEDIATE` transaction iff row had `seed_key`. Async `SchedulerStore.delete_schedule` signature is UNCHANGED (callers are tests only). New `cmd_revive_seed` uses the same sync pattern. [§C5 rewritten]
- **B-D6** E2E `test_gh_e2e_scheduler_to_commit.py` downgraded to manual smoke note — component tests (C4 happy / no-changes / diverged + C5 seed + C6 hooks) cover the functional path. [§C7]

Empirical backing (spike IDs inline):

- `spike_gh_auth_shapes.py` / `spike_gh_home_probe.py` — gh 2.89 exit/stderr shape; HOME discovery. [R-1, R-15]
- `spike_git_status_porcelain.py` — porcelain prefix shapes; `git diff --quiet` MISSES untracked. [R-9, B2]
- `spike_git_ssh_command.py` — `shlex.quote` defeats env-value shell-split injection. [R-12, B6]
- `spike_flock_oom.py` — kernel auto-releases flock on SIGKILL; re-acquire in ~0.003 ms. [R-4]
- `spike_sqlite_alter_table.py` — migration 0005/0006 shapes; partial UNIQUE INDEX + tombstones. [R-6, R-11]
- `spike_git_commit_only.py` — `--only <path>` isolates staged content to path. [R-7]
- `spike_vault_bootstrap.py` — init+remote+empty-commit+push flow works; divergence stderr markers. [R-8, R-14]
- `spike_zoneinfo_darwin.py` — `zoneinfo` stdlib on Darwin 24; Cyrillic tz renders correctly. [R-10, B4]
- `spike_artefact_re_corpus.py` — 50-entry gh CLI corpus → 0 ARTEFACT_RE false positives. [R-13, Q11]

Companion docs coder MUST read first: `plan/phase8/description.md`, `plan/phase8/detailed-plan.md`, `plan/phase7/implementation.md` (style), `plan/phase7/summary.md` (I-7.x invariants).

**Auth:** OAuth via `claude` CLI. No `ANTHROPIC_API_KEY`. Owner's GitHub operations use `gh auth login` (main host account). Vault push uses a dedicated deploy key + separate GitHub account `vaultbot-owner` (see docs/ops/github-setup.md).

---

## 0. Pitfall box (MUST READ) — spike-verified

1. **DO NOT use `git diff --quiet` to detect "something to commit".** Spike R-9 proved untracked files evade `diff`. Use `git status --porcelain`; non-empty stdout = changes exist. Relevant to §4 C4 step 7.

2. **DO NOT concatenate `GIT_SSH_COMMAND` token-by-token with raw paths.** Spike R-12 showed the unquoted injection `/tmp/key -o ProxyCommand=curl` splits into separate args, giving attacker control of ssh `-o` flags. ALWAYS wrap every path/string through `shlex.quote`. Even "literal" tokens (known-safe `-o IdentitiesOnly=yes`) stay unquoted, but user-influenced values MUST be quoted.

3. **DO NOT use `json.load` on `gh` output without `isinstance(..., dict)` guard.** The X-1 pre-existing bug (genimage) teaches: `json.loads('null')` returns `None`; `json.loads('[]')` returns a list. Any `state.get(...)` on a non-dict raises `AttributeError`. Pattern: `data = json.loads(raw); data = data if isinstance(data, dict) else {}`.

4. **DO NOT pass `--json user` to `gh auth status`.** Spike R-1 confirmed gh 2.89 does not support that field. Only `--json hosts` works. The probe uses rc + stderr text parsing; avoid `--json` for auth detection.

5. **DO NOT forget env-wipe on every `subprocess.run(["gh", ...])` call.** Q2 decision. `env.pop("GH_TOKEN", None); env.pop("GITHUB_TOKEN", None)` — if owner has `GITHUB_TOKEN` set for another tool, it would override the OAuth session. Wipe forces gh to use `~/.config/gh/hosts.yml`.

6. **DO NOT use `gh pr create`, `gh issue close`, `gh issue comment`, `gh pr merge`, `gh api -X POST/...`.** Phase 8 is read-only gh + write-only vault git. Write flows are phase 9 keyboard-confirm. `_validate_gh_argv` denies these; defence-in-depth in the CLI itself.

7. **DO NOT use `git add -A` from `project_root`.** Spike R-9 + Q9 decision: vault_dir is a standalone git repo. `git -C <vault_dir> add -A` is correct. Running `git add -A` from `project_root` would try to stage everything in the main 0xone-assistant checkout — catastrophic.

8. **DO NOT skip tombstone check before seed insert.** Q10 decision (I-8.9): `rm` on a seeded row inserts a tombstone; re-seeding without checking re-creates the row owner explicitly deleted. Pattern: `if await store.tombstone_exists(seed_key): log.info(...); return`.

9. **DO NOT rely on `SCHEMA_VERSION` alone to skip migrations.** The dispatcher in `state/db.py` already uses `if current < N` cascades — keep that pattern. Add `_apply_v5` then `_apply_v6` with `if current < 5:` and `if current < 6:` in `apply_schema`.

10. **DO NOT forget the pidfile flock is the single-daemon barrier.** SF4 ordering: `apply_schema` runs AFTER `_acquire_pid_lock_or_exit`, so OLD daemon's WAL connection cannot block ALTER TABLE. R-16 is a non-issue because of this ordering.

11. **DO NOT use `StrictHostKeyChecking=no`.** Plan specifies `accept-new` — this trusts on first-use but rejects subsequent key changes. `no` would silently accept any MITM. Also, isolate `UserKnownHostsFile` under `<data_dir>/run/gh-vault-known-hosts` so other ssh usage on the host is not polluted.

12. **DO NOT forget `GIT_TERMINAL_PROMPT=0`.** Without it, if ssh fails somehow and git falls back to prompting, it could block forever in a non-interactive daemon. Set it in the push env.

13. **DO NOT bump `_ARTEFACT_RE`.** Spike R-13 confirmed 0 false positives on 50-string gh CLI corpus. Phase-7 v3 stands; do NOT touch `media/artefacts.py`.

14. **DO NOT use `asyncio.Lock` for flock.** Requirement is cross-process single-flight. Use `fcntl.flock(LOCK_EX | LOCK_NB)` on `<data_dir>/run/gh-vault-commit.lock`. Kernel auto-releases on any process death (spike R-4 proved sub-millisecond on macOS).

15. **DO NOT omit the `auth_email/name` via `-c user.email=... -c user.name=...` inline.** I-8.4: never call `git config` (persists). Inline `-c` overrides are per-invocation only.

16. **DO NOT place `-c` AFTER the git subcommand.** v2 SF-A4: `git commit -c user.email=X` means "use this commit as a template" (`<commit-ish>`), not "set config key". Always write `git -C <cwd> -c KEY=VALUE commit ...`. Verified by reading `git commit --help`.

17. **DO NOT call `get_settings()` from `tools/gh/main.py`.** v2 B-A2: `Settings()` requires `TELEGRAM_BOT_TOKEN` + `OWNER_CHAT_ID`; the CLI must work standalone. Use `GitHubSettings()` directly (sub-model with no required fields) and local `_data_dir()` / `_vault_dir()` helpers that mirror the `tools/schedule/main.py` + `tools/memory/main.py` pattern.

18. **DO NOT annotate `allowed_repos: tuple[str, ...]` without `NoDecode`.** v2 B-A1: pydantic-settings 2.3+ JSON-decodes typed tuple/list/dict env values BEFORE validators run, so `GH_ALLOWED_REPOS="a/b,c/d"` raises `SettingsError`. Wrap the annotation: `Annotated[tuple[str, ...], NoDecode]`, then the `mode="before"` validator sees the raw string and splits on commas.

19. **DO NOT use `executescript` for multi-statement migrations.** v2 SF-F2: on aiosqlite, `executescript` auto-commits each statement, bypassing the `BEGIN IMMEDIATE` you opened. Execute each statement individually with `await conn.execute(...)` so the migration is one atomic transaction.

20. **DO NOT return exit 5 (`no_changes`) while a local commit has not yet been pushed.** v2 B-B2: check `git rev-list @{u}..HEAD --count` BEFORE the porcelain check. If unpushed > 0, do a push-only retry of the existing commit. Without this, a failed push in run N followed by a clean tree in run N+1 silently drops the commit from the backup flow.

21. **DO NOT hard-DELETE a schedule row from the CLI.** v2 B-D1: `tools/schedule/main.py::cmd_rm` soft-deletes via `UPDATE enabled=0` (phase-5 G-W2-x: preserves trigger-history FK chain). The tombstone insert happens in the SAME sync `BEGIN IMMEDIATE` transaction if the row has `seed_key IS NOT NULL`. Don't rewire this to the async store — the sync sqlite3 path is the correct home for CLI state mutations.

---

## Pre-work: Wave 0 pre-flight (single commit)

**Commit: `fix(genimage): close X-1 shape-guard + X-2 UnicodeDecodeError`**

Files:

1. `tools/genimage/main.py` — two tiny edits:

   **Edit A** — inside `_check_and_increment_quota` around line ~335:

   ```python
   try:
       state = json.loads(raw.decode("utf-8")) if raw else {}
   except (UnicodeDecodeError, json.JSONDecodeError):
       state = {}
   if not isinstance(state, dict):
       state = {}
   ```

   The `if not isinstance(...)` line is the NEW addition. Keep the `except` clause intact (it already exists per the current file — confirmed during research). After this guard, subsequent `state.get("date")` / `state.get("count")` are safe.

   **Edit B** — inside `_read_quota_best_effort` around line ~363:

   ```python
   def _read_quota_best_effort(path: Path) -> dict[str, Any]:
       """Return the quota state without locking; used only for diagnostics."""
       try:
           raw = path.read_bytes()
       except OSError:
           return {}
       try:
           text = raw.decode("utf-8", errors="replace")
       except Exception:
           return {}
       try:
           parsed = json.loads(text)
       except json.JSONDecodeError:
           return {}
       return parsed if isinstance(parsed, dict) else {}
   ```

   Changes: `read_text(encoding="utf-8")` → `read_bytes()` + explicit `decode(errors="replace")`. Symmetric with the locked write path; never raises `UnicodeDecodeError`.

2. `tests/test_genimage_quota_midnight_rollover.py`:

   - Remove `@pytest.mark.xfail(strict=True, reason="...")` from `test_wrong_shape_list_payload_recovers`.
   - Remove `@pytest.mark.xfail(strict=True, reason="...")` from `test_best_effort_reader_binary_input_xfail`; rename test to `test_best_effort_reader_binary_input_recovers`.

**Acceptance:**

- `uv run pytest tests/test_genimage_quota_midnight_rollover.py -q` → 0 failed / 0 xfailed (those two tests now pass strictly).
- Full suite xfail count: 5 → 3 (X-1/X-2 closed, X-3/X-4 already closed, 3 regex S-2 adjacency residuals remain).

**Dependencies:** phase-7 HEAD (nothing else required).

---

## Commits C1–C7 (main phase 8)

Commit discipline: each commit under ~500 LOC diff (src+tests). `just lint && uv run pytest -q` between commits.

### C1: `GitHubSettings` + URL/tz/path validators

**Files:**

- `src/assistant/config.py` — append `GitHubSettings(BaseSettings)`; wire into `Settings.github: GitHubSettings`.
- `tests/test_gh_settings_ssh_url_validation.py`
- `tests/test_gh_settings_tz_validation.py` (new — research recommendation R-10)

**`GitHubSettings` full prescription:**

```python
# In src/assistant/config.py, after MediaSettings class:

import re
import shlex
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# ASCII-only owner/repo slug (SF2 — reject kириллица, reject `..`).
# Owner segment is GitHub-accurate per SF-C1: <=38 chars, alnum+hyphen, no
# leading/trailing hyphen, no consecutive hyphens. Repo allows the broader
# `[A-Za-z0-9._-]+` per GitHub's lenient repo-name rules.
_GH_SSH_URL_RE = re.compile(
    r"^git@github\.com:"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"
    r"/(?P<repo>[A-Za-z0-9._-]+)\.git$"
)
# Shell metacharacters B6 rejects in ssh key path.
_SSH_KEY_BAD_CHARS = set(" \t\n$;&|<>'\"\\`")


class GitHubSettings(BaseSettings):
    """Phase-8 GitHub operations + vault auto-commit knobs.

    All fields overridable via `GH_<NAME>` env var. Defaults are
    spike-verified (R-1/R-9/R-10/R-12). Nested under `Settings.github`.

    **v2 note on `allowed_repos`:** the annotation uses
    `Annotated[tuple[str, ...], NoDecode]` because pydantic-settings 2.3+
    eagerly JSON-decodes any *typed* tuple/list/dict env value BEFORE the
    field validator runs. Without `NoDecode`, `GH_ALLOWED_REPOS="a/b,c/d"`
    would raise `SettingsError: error parsing value for field ...`. With
    `NoDecode`, the framework delivers the raw string to our `mode="before"`
    validator which splits on commas. Confirmed against pydantic-settings
    2.13.1 (blocker B-A1 closed).
    """

    model_config = SettingsConfigDict(
        env_prefix="GH_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    vault_remote_url: str = ""  # empty -> auto_commit_enabled auto-disabled (Q4)
    vault_ssh_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".ssh" / "id_vault"
    )
    vault_remote_name: str = "vault-backup"  # Q5
    vault_branch: str = "main"
    auto_commit_enabled: bool = True
    auto_commit_cron: str = "0 3 * * *"
    auto_commit_tz: str = "Europe/Moscow"  # Q6
    commit_message_template: str = "vault sync {date}"
    commit_author_email: str = "vaultbot@localhost"  # Q7
    # SF-F3: field name ends in `_path` to match env var GH_VAULT_SSH_KEY_PATH.
    # A pre-validator expands `~` (see `_expand_ssh_key_path` below).
    # B-A1: NoDecode forces raw string to reach the `_parse_allowed_repos`
    # validator; without it pydantic-settings 2.13.1 tries JSON-decoding first.
    allowed_repos: Annotated[tuple[str, ...], NoDecode] = ()

    # ------------------------------------------------------------------
    # Validators

    @field_validator("vault_remote_url")
    @classmethod
    def _validate_remote_url(cls, v: str) -> str:
        if not v:
            return v  # empty allowed; triggers auto-disable in model_validator
        if not _GH_SSH_URL_RE.match(v):
            raise ValueError(
                f"vault_remote_url must match git@github.com:OWNER/REPO.git "
                f"(ASCII alnum+._-); got: {v!r}"
            )
        m = _GH_SSH_URL_RE.match(v)
        assert m
        owner, repo = m.group("owner"), m.group("repo")
        for seg, name in ((owner, "owner"), (repo, "repo")):
            if ".." in seg or seg.startswith(".") or seg.endswith("."):
                raise ValueError(f"{name} segment {seg!r} has dangerous dots")
        return v

    @field_validator("vault_ssh_key_path", mode="before")
    @classmethod
    def _expand_ssh_key_path(cls, v: object) -> object:
        """SF-F3: expand `~` in paths coming from env BEFORE downstream checks.

        `env_file` may contain `GH_VAULT_SSH_KEY_PATH=~/.ssh/id_vault`; if we
        don't expand here pydantic stores the literal `~/.ssh/id_vault` which
        then fails `is_file()` in the command handler.
        """
        if isinstance(v, str):
            return Path(v).expanduser()
        if isinstance(v, Path):
            return v.expanduser()
        return v

    @field_validator("vault_ssh_key_path")
    @classmethod
    def _validate_ssh_key_path(cls, v: Path) -> Path:
        s = str(v)
        for ch in _SSH_KEY_BAD_CHARS:
            if ch in s:
                raise ValueError(
                    f"vault_ssh_key_path must not contain metacharacter {ch!r}; got: {s!r}"
                )
        if " -o " in s:  # paranoid defence vs. clever bypass
            raise ValueError("vault_ssh_key_path contains ' -o ' substring")
        return v

    @field_validator("auto_commit_tz")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v

    @field_validator("auto_commit_cron")
    @classmethod
    def _validate_cron(cls, v: str) -> str:
        # Lazy import to avoid coupling config -> scheduler.
        from assistant.scheduler.cron import parse_cron
        try:
            parse_cron(v)
        except Exception as exc:
            raise ValueError(f"invalid cron expr {v!r}: {exc}") from exc
        return v

    @field_validator("allowed_repos", mode="before")
    @classmethod
    def _parse_allowed_repos(cls, v: object) -> tuple[str, ...]:
        """B-A1: accepts raw string thanks to `NoDecode` annotation.

        Input shapes:
        - `"a/b,c/d"` from env (most common after NoDecode)
        - `("a/b", "c/d")` from programmatic instantiation (tests)
        - `[]` / `()` / `None` / `""` → all collapse to `()`
        """
        if v is None or v == "":
            return ()
        if isinstance(v, str):
            return tuple(s.strip() for s in v.split(",") if s.strip())
        if isinstance(v, (list, tuple)):
            return tuple(str(s).strip() for s in v if str(s).strip())
        return ()

    @model_validator(mode="after")
    def _auto_disable_on_empty_url(self) -> "GitHubSettings":
        if self.auto_commit_enabled and not self.vault_remote_url:
            # Can't modify self in v2 validators; use object.__setattr__
            # because BaseSettings is mutable but pydantic flags us.
            object.__setattr__(self, "auto_commit_enabled", False)
        return self
```

Wire into `Settings`:

```python
class Settings(BaseSettings):
    # ... existing fields ...
    github: GitHubSettings = Field(default_factory=GitHubSettings)
```

**Tests (`tests/test_gh_settings_ssh_url_validation.py`):**

Required cases (each as `def test_...`):

1. Valid ssh URL `git@github.com:owner/repo.git` accepted.
2. https URL `https://github.com/owner/repo.git` raises `ValidationError`.
3. URL with cyrillic `git@github.com:оwner/repo.git` raises `ValidationError`.
4. URL with `..` in owner `git@github.com:foo..bar/repo.git` raises `ValidationError` (tightened owner regex rejects consecutive hyphens + underscores — `..` has dots which are also excluded by the SF-C1 char class).
5. Empty URL with `auto_commit_enabled=True` → field auto-disabled in resulting settings.
6. `allowed_repos` parsed from env string `"foo/bar,baz/qux"` → `("foo/bar", "baz/qux")`. Use `monkeypatch.setenv("GH_ALLOWED_REPOS", "foo/bar,baz/qux")`; instantiate `GitHubSettings()` directly (NOT `Settings()` — TG tokens not required for this test).
7. ssh key path with space `Path("/tmp/id vault")` raises `ValidationError`.
8. ssh key path containing ` -o ` substring raises `ValidationError`.
9. ssh key path with unicode `Path("/tmp/ключ")` — ACCEPTED (no space, no metachars).
10. **SF-C1 new:** owner `-leading-hyphen` rejected (`git@github.com:-bad/repo.git` → `ValidationError`).
11. **SF-C1 new:** owner `trailing-` (ends with hyphen) rejected.
12. **SF-C1 new:** 39-char owner rejected (GitHub caps owners at 39, but the regex uses 38 + 1 leading/trailing = 39 max; boundary case). Owner `a` * 39 → ValidationError.
13. **SF-F3 new:** env `GH_VAULT_SSH_KEY_PATH=~/.ssh/id_vault` expands to absolute path during validation (assert `str(gh.vault_ssh_key_path).startswith(str(Path.home()))`).
14. **B-A1 regression:** env `GH_ALLOWED_REPOS=""` (empty string) → `allowed_repos == ()` (no SettingsError).

**Tests (`tests/test_gh_settings_tz_validation.py`):**

10. `auto_commit_tz="Europe/Moscow"` accepted.
11. `auto_commit_tz="UTC"` accepted.
12. `auto_commit_tz="Xyz/Nowhere"` raises `ValidationError` wrapping `ZoneInfoNotFoundError`.
13. `auto_commit_cron="0 3 * * *"` accepted.
14. `auto_commit_cron="invalid cron"` raises `ValidationError`.

**Acceptance:**

- `just lint` green.
- 19 new tests pass (14 original + 5 added in v2 for SF-C1 / SF-F3 / B-A1); mypy strict on `config.py` stays green.
- `GitHubSettings()` instantiated without `TELEGRAM_BOT_TOKEN` set in env — must NOT raise (sub-model isolation).

**Dependencies:** C0.

---

### C2: `tools/gh/` scaffolding + `auth-status` + SKILL.md draft

**Files (new):**

- `tools/gh/__init__.py` (empty; package marker)
- `tools/gh/main.py` (skeleton ~120 LOC: argparse subparsers for `auth-status`, `issue`, `pr`, `repo`, `vault-commit-push` — the last three are placeholders at this commit, only `auth-status` has logic)
- `tools/gh/_lib/__init__.py` (empty)
- `tools/gh/_lib/exit_codes.py`
- `tools/gh/_lib/gh_ops.py` (skeleton; full in C3)
- `skills/gh/SKILL.md`
- `tests/test_gh_skill_md_assertion.py`
- `tests/test_gh_auth_status_probe.py`

**`tools/gh/_lib/exit_codes.py` (exact content):**

```python
"""Phase-8 CLI exit codes. Module constants, stdlib-only."""

OK = 0
ARGV = 2
VALIDATION = 3
GH_NOT_AUTHED = 4
NO_CHANGES = 5
REPO_NOT_ALLOWED = 6
DIVERGED = 7
PUSH_FAILED = 8
LOCK_BUSY = 9
SSH_KEY_ERROR = 10

__all__ = [
    "OK", "ARGV", "VALIDATION", "GH_NOT_AUTHED", "NO_CHANGES",
    "REPO_NOT_ALLOWED", "DIVERGED", "PUSH_FAILED", "LOCK_BUSY", "SSH_KEY_ERROR",
]
```

**`tools/gh/_lib/gh_ops.py` (C2 skeleton — C3 extends):**

```python
"""Thin `subprocess.run` wrappers around `gh` CLI with env-wipe (Q2)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# SF-C2: every variant of GH token / host / config-dir env must be stripped
# so `gh` falls back to `~/.config/gh/hosts.yml` (the OAuth session). Missing
# any of these enterprise variants would let an old env leak through.
_GH_ENV_SCRUB_KEYS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
)


def build_gh_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env dict with all GH_TOKEN / enterprise / host overrides removed (Q2, SF-C2)."""
    env = dict(os.environ)
    for key in _GH_ENV_SCRUB_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def gh_auth_status(timeout_s: float = 10.0) -> tuple[int, str, str]:
    """Run `gh auth status --hostname github.com`. Returns (rc, stdout, stderr).

    SF-A7: catches `subprocess.TimeoutExpired` and maps to (-1, "", "timeout")
    so callers can emit a consistent JSON error rather than propagating the
    exception up through the CLI.
    """
    if shutil.which("gh") is None:
        return (127, "", "gh not on PATH")
    try:
        proc = subprocess.run(
            ["gh", "auth", "status", "--hostname", "github.com"],
            env=build_gh_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout")
    return proc.returncode, proc.stdout, proc.stderr
```

**`tools/gh/main.py` skeleton (C2 level) — v2 adopts the `tools/schedule/main.py` + `tools/genimage/main.py` stdlib-first CLI pattern:**

> **Blocker B-A2 (closed in v2):** the previous v1 skeleton imported `from assistant.config import get_settings` and called `get_settings()` inside each handler. That breaks standalone CLI use because `Settings()` requires `TELEGRAM_BOT_TOKEN` + `OWNER_CHAT_ID`; `python tools/gh/main.py auth-status` on a fresh box without those vars raised `ValidationError`. v2 mirrors `tools/schedule/main.py` (sync sqlite3 + local `_data_dir()` helper) and instantiates the sub-model `GitHubSettings()` directly — the sub-model has no required fields (all defaults), so CLI works even when the daemon envs are absent.

```python
#!/usr/bin/env python3
"""Phase-8 thin CLI wrapper for gh + vault auto-commit.

Stdlib-first + direct sub-model instantiation so the CLI runs standalone
(fresh install, manual smoke, cron) WITHOUT TELEGRAM_BOT_TOKEN /
OWNER_CHAT_ID in the environment. Mirrors `tools/schedule/main.py` +
`tools/genimage/main.py`. Do NOT import `get_settings` here — see
§0 blocker B-A2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# sys.path pragma for cwd + module invocation parity (phase-7 pattern).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.gh._lib import exit_codes as ec
from tools.gh._lib.gh_ops import gh_auth_status


# ---------------------------------------------------------------------------
# Module-local path helpers (B-A2). Identical semantics to
# `tools/schedule/main.py::_data_dir` + `tools/memory/main.py::_resolve_vault_dir`
# so CLI invocations stay consistent with daemon behaviour.


def _data_dir() -> Path:
    """Resolve `<data_dir>` without importing `assistant.config.Settings`.

    Precedence (matches `assistant.config._default_data_dir`):
      1. `ASSISTANT_DATA_DIR` (explicit override)
      2. `$XDG_DATA_HOME/0xone-assistant`
      3. `~/.local/share/0xone-assistant`
    """
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "0xone-assistant"
    return Path.home() / ".local" / "share" / "0xone-assistant"


def _vault_dir() -> Path:
    """Resolve vault dir (mirrors `MemorySettings.vault_dir` default)."""
    override = os.environ.get("MEMORY_VAULT_DIR")
    if override:
        return Path(override).expanduser()
    return _data_dir() / "vault"


def _cmd_auth_status(args: argparse.Namespace) -> int:
    rc, out, err = gh_auth_status()
    if rc == 0:
        print(json.dumps({"ok": True}))
        return ec.OK
    # SF-A7 map: -1 stderr=="timeout" is our timeout sentinel from gh_auth_status.
    if rc == -1 and err == "timeout":
        print(json.dumps({"ok": False, "error": "gh_timeout"}))
        return 1
    print(json.dumps({"ok": False, "error": "not_authenticated"}))
    return ec.GH_NOT_AUTHED


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tools/gh/main.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-status")
    # issue / pr / repo / vault-commit-push subparsers added in C3/C4.
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "auth-status":
        return _cmd_auth_status(args)
    parser.error(f"unknown subcommand {args.cmd}")
    return ec.ARGV


if __name__ == "__main__":
    sys.exit(main())
```

**`skills/gh/SKILL.md` (C2 draft — refined in C7):**

```markdown
---
name: gh
description: GitHub issues/PRs/repos read-only + daily vault backup commits
allowed-tools: [Bash]
---

# gh — GitHub CLI wrapper (phase 8)

Используй `tools/gh/main.py` для read-only GitHub операций и ежедневного
бэкапа `<data_dir>/vault/` на отдельный GitHub аккаунт.

## Команды

- `python tools/gh/main.py auth-status` — проверка gh OAuth session.
- `python tools/gh/main.py issue list|view|create --repo OWNER/REPO ...`
- `python tools/gh/main.py pr list|view --repo OWNER/REPO ...`
- `python tools/gh/main.py repo view OWNER/REPO`
- `python tools/gh/main.py vault-commit-push [--message MSG] [--dry-run]`

## Exit codes

| code | meaning | что делать |
|---|---|---|
| 0 | ok | коммит/push прошёл, либо read-команда вернула данные |
| 2 | argv error | неверные аргументы; пересмотри вызов |
| 3 | validation | vault_dir не настроен, или путь невалиден |
| 4 | gh_not_authed | `gh auth login` требует вмешательства owner'а |
| 5 | no_changes | silent для scheduler; модели → ничего не отвечать owner'у |
| 6 | repo_not_allowed | целевой `--repo` не в `GH_ALLOWED_REPOS` |
| 7 | diverged | remote расходится; попроси owner'а разрешить вручную |
| 8 | push_failed | другая ошибка git push |
| 9 | lock_busy | параллельный `vault-commit-push` в процессе; подожди минуту |
| 10 | ssh_key_error | deploy key отсутствует/неправильные permissions |

## Правила безопасности (выдерживать все)

1. НЕ использовать `gh pr create`, `gh issue close/comment/edit`, `gh pr merge`,
   `gh api -X POST/...`. Это phase 9 keyboard-confirm.
2. НЕ использовать `--force`, `--force-with-lease`, `--no-verify`, `--amend`.
3. Сообщения об артефактах всегда через пробел после `:`, например
   `готово: abc1234` (phase-7 H-13 правило).
4. После `vault-commit-push` — всегда упоминай `commit_sha` если exit 0;
   silent если exit 5 (no_changes).

## Примеры диалогов

- "запушь vault" → `python tools/gh/main.py vault-commit-push`
  → exit 0 → `vault сохранён, sha=abc1234, файлов: 3`.
- "сделай бэкап" (scheduler 03:00) → тот же CLI с auto-generated message.
- "какие открытые issues?" → `python tools/gh/main.py issue list --repo OWNER/REPO --state open`.
- "открой issue про баг X" → `python tools/gh/main.py issue create --repo OWNER/REPO --title "bug: X" --body "..."`.
- "посмотри PR #15" → `python tools/gh/main.py pr view 15 --repo OWNER/REPO`.
```

**Tests:**

`tests/test_gh_skill_md_assertion.py` (H-13 pattern — SKILL.md structural checks):

```python
import pathlib
import re

def test_gh_skill_md_exists_and_valid():
    path = pathlib.Path("skills/gh/SKILL.md")
    assert path.is_file(), "SKILL.md missing"
    text = path.read_text(encoding="utf-8")
    # YAML frontmatter present
    m = re.match(r"^---\n(?P<fm>.+?)\n---\n", text, re.DOTALL)
    assert m, "frontmatter missing"
    fm = m.group("fm")
    assert "name: gh" in fm
    assert "description:" in fm
    assert "allowed-tools: [Bash]" in fm
    # Exit code table contains all 10 codes 0/2/3/4/5/6/7/8/9/10
    for code in ("| 0 ", "| 2 ", "| 3 ", "| 4 ", "| 5 ", "| 6 ", "| 7 ", "| 8 ", "| 9 ", "| 10 "):
        assert code in text, f"exit-code row {code.strip()} missing"
    # At least 5 dialog examples
    assert text.count('→ `python tools/gh/main.py') >= 5
```

`tests/test_gh_auth_status_probe.py` — monkeypatch `subprocess.run`:

```python
import json
import subprocess
import sys
from unittest import mock

from tools.gh import main as gh_main


def _fake_run_factory(rc: int, stdout: str = "", stderr: str = ""):
    def _run(*a, **kw):
        return subprocess.CompletedProcess(a, rc, stdout, stderr)
    return _run


def test_auth_status_ok(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(0, stdout="ok\n"))
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    rc = gh_main.main(["auth-status"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_auth_status_not_authed(monkeypatch, capsys):
    monkeypatch.setattr(
        subprocess, "run",
        _fake_run_factory(1, stderr="You are not logged into any GitHub hosts.\n"),
    )
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    rc = gh_main.main(["auth-status"])
    assert rc == 4
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "not_authenticated"}


def test_auth_status_gh_not_on_path(monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda name: None)
    rc = gh_main.main(["auth-status"])
    assert rc == 4
```

**Acceptance:**

- `python tools/gh/main.py auth-status` on dev machine → rc=0, `{"ok": true}` (machine is logged in per spike_gh_home_probe).
- 3 unit tests pass; mypy clean on `tools/gh/`.

**Dependencies:** C1.

---

### C3: `tools/gh/` — issue/pr/repo read-only subcommands + allow-list

**Files (new + extend):**

- `tools/gh/main.py` (extend with `issue`, `pr`, `repo` subcommands)
- `tools/gh/_lib/gh_ops.py` (extend with `run_gh_json`)
- `tools/gh/_lib/repo_allowlist.py` (new, ~40 LOC)
- `tests/test_gh_issue_create_happy.py`
- `tests/test_gh_repo_whitelist.py`

**`tools/gh/_lib/repo_allowlist.py`:**

```python
"""Extract OWNER/REPO from ssh URL and assert membership in GH_ALLOWED_REPOS."""

from __future__ import annotations

import re
from typing import Iterable

_SSH_URL_RE = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)\.git$"
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def extract_owner_repo(ssh_url: str) -> str | None:
    """Return 'owner/repo' or None if not a valid ssh url."""
    m = _SSH_URL_RE.match(ssh_url)
    if not m:
        return None
    return f"{m.group('owner')}/{m.group('repo')}"


def is_allowed(slug: str, allowed: Iterable[str]) -> bool:
    """Exact-match membership; `slug` must be 'owner/repo'."""
    if not _SLUG_RE.match(slug):
        return False
    return slug in set(allowed)
```

**`tools/gh/_lib/gh_ops.py` extension:**

```python
def run_gh_json(
    args: list[str], *, timeout_s: float = 30.0
) -> tuple[int, dict | list | None, str]:
    """Run `gh <args>` with env-wipe; return (rc, parsed_json_or_None, stderr).

    If stdout is not valid JSON, parsed is None (but rc passes through).
    SF-A7: `subprocess.TimeoutExpired` is caught → (-1, None, "timeout")
    so the CLI can map it to exit 1 `gh_timeout` JSON instead of crashing.
    """
    try:
        proc = subprocess.run(
            ["gh", *args],
            env=build_gh_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return (-1, None, "timeout")
    parsed: dict | list | None = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, proc.stderr
```

**`tools/gh/main.py` extension (new subcommands + allow-list hooks):**

> **v2 blocker B-A2:** handlers instantiate `GitHubSettings()` directly
> (sub-model with no required fields) instead of `get_settings()`. This
> keeps the CLI usable without TG tokens. Auth/unauth classification now
> matches both `"authenticated"` and `"not logged into"` per SF-A6.

```python
from assistant.config import GitHubSettings  # OK — sub-model has no required fields
from tools.gh._lib import repo_allowlist
from tools.gh._lib.gh_ops import run_gh_json


# SF-A6: substring hit-list for the "unauthenticated" exit-4 branch.
_GH_UNAUTH_SUBSTRINGS = ("authenticated", "not logged into")


def _is_unauth_stderr(stderr: str) -> bool:
    lo = stderr.lower()
    return any(s in lo for s in _GH_UNAUTH_SUBSTRINGS)


def _cmd_issue(args: argparse.Namespace) -> int:
    gh = GitHubSettings()  # reads GH_* from env, NO TG tokens required
    repo = args.repo
    if not repo_allowlist.is_allowed(repo, gh.allowed_repos):
        print(json.dumps({"ok": False, "error": "repo_not_allowed", "repo": repo}))
        return ec.REPO_NOT_ALLOWED
    if args.subsub == "create":
        gh_args = [
            "issue", "create", "--repo", repo,
            "--title", args.title, "--body", args.body,
            "--json", "url,number",
        ]
        for label in args.label or []:
            gh_args.extend(["--label", label])
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            print(json.dumps({"ok": False, "error": stderr[:200]}))
            return ec.GH_NOT_AUTHED if _is_unauth_stderr(stderr) else 1
        print(json.dumps({"ok": True, **(data or {})}))
        return ec.OK
    elif args.subsub == "list":
        gh_args = ["issue", "list", "--repo", repo, "--json", "number,title,state,labels"]
        if args.state:
            gh_args.extend(["--state", args.state])
        if args.limit:
            gh_args.extend(["--limit", str(min(args.limit, 100))])  # SF5 hard-cap
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            print(json.dumps({"ok": False, "error": stderr[:200]}))
            return ec.GH_NOT_AUTHED if _is_unauth_stderr(stderr) else 1
        print(json.dumps({"ok": True, "issues": data or []}))
        return ec.OK
    elif args.subsub == "view":
        gh_args = [
            "issue", "view", str(args.number),
            "--repo", repo,
            "--json", "number,title,body,state,labels,author",
        ]
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            print(json.dumps({"ok": False, "error": stderr[:200]}))
            return ec.GH_NOT_AUTHED if _is_unauth_stderr(stderr) else 1
        print(json.dumps({"ok": True, **(data or {})}))
        return ec.OK
    else:
        return ec.ARGV


def _cmd_pr(args: argparse.Namespace) -> int:
    gh = GitHubSettings()
    repo = args.repo
    if not repo_allowlist.is_allowed(repo, gh.allowed_repos):
        print(json.dumps({"ok": False, "error": "repo_not_allowed", "repo": repo}))
        return ec.REPO_NOT_ALLOWED
    if args.subsub == "list":
        gh_args = ["pr", "list", "--repo", repo, "--json", "number,title,state"]
        if args.limit:
            gh_args.extend(["--limit", str(min(args.limit, 100))])
        rc, data, stderr = run_gh_json(gh_args)
    elif args.subsub == "view":
        gh_args = [
            "pr", "view", str(args.number),
            "--repo", repo,
            "--json", "number,title,body,state,mergeable,author",
        ]
        rc, data, stderr = run_gh_json(gh_args)
        # SF-A5: flatten `gh pr view` nested author JSON (`{"author": {"login": X}}` → `"author": X`)
        # so downstream consumers see a flat shape consistent with other commands.
        if rc == 0 and isinstance(data, dict):
            author = data.get("author")
            if isinstance(author, dict) and "login" in author:
                data["author"] = author["login"]
    else:
        return ec.ARGV
    if rc != 0:
        print(json.dumps({"ok": False, "error": stderr[:200]}))
        return ec.GH_NOT_AUTHED if _is_unauth_stderr(stderr) else 1
    print(json.dumps({"ok": True, **(data if isinstance(data, dict) else {"items": data})}))
    return ec.OK


def _cmd_repo(args: argparse.Namespace) -> int:
    gh = GitHubSettings()
    repo = args.repo
    if not repo_allowlist.is_allowed(repo, gh.allowed_repos):
        print(json.dumps({"ok": False, "error": "repo_not_allowed", "repo": repo}))
        return ec.REPO_NOT_ALLOWED
    # only `repo view` is supported
    gh_args = ["repo", "view", repo, "--json", "name,description,defaultBranchRef,visibility"]
    rc, data, stderr = run_gh_json(gh_args)
    if rc != 0:
        print(json.dumps({"ok": False, "error": stderr[:200]}))
        return ec.GH_NOT_AUTHED if _is_unauth_stderr(stderr) else 1
    print(json.dumps({"ok": True, **(data or {})}))
    return ec.OK
```

Extend `_build_parser`:

```python
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tools/gh/main.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth-status")

    # issue
    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="subsub", required=True)
    create = issue_sub.add_parser("create")
    create.add_argument("--repo", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--body", required=True)
    create.add_argument("--label", action="append", default=[])
    lst = issue_sub.add_parser("list")
    lst.add_argument("--repo", required=True)
    lst.add_argument("--state", choices=["open", "closed", "all"])
    lst.add_argument("--limit", type=int, default=30)
    view = issue_sub.add_parser("view")
    view.add_argument("number", type=int)
    view.add_argument("--repo", required=True)

    # pr
    pr = sub.add_parser("pr")
    pr_sub = pr.add_subparsers(dest="subsub", required=True)
    pr_list = pr_sub.add_parser("list")
    pr_list.add_argument("--repo", required=True)
    pr_list.add_argument("--limit", type=int, default=30)
    pr_view = pr_sub.add_parser("view")
    pr_view.add_argument("number", type=int)
    pr_view.add_argument("--repo", required=True)

    # repo
    repo = sub.add_parser("repo")
    repo_sub = repo.add_subparsers(dest="subsub", required=True)
    rv = repo_sub.add_parser("view")
    rv.add_argument("repo")

    return p
```

**Tests:**

`tests/test_gh_issue_create_happy.py`:

```python
import json
import subprocess
from unittest import mock

from tools.gh import main as gh_main

def test_issue_create_happy(monkeypatch, capsys, tmp_path):
    # v2 (B-A2): NO TELEGRAM_BOT_TOKEN / OWNER_CHAT_ID — GitHubSettings
    # instantiated directly, doesn't require the Settings top-level fields.
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "git@github.com:owner/vault.git")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)

    fake_out = json.dumps({"url": "https://github.com/owner/repo/issues/42", "number": 42})

    def _run(*a, **kw):
        return subprocess.CompletedProcess(a, 0, fake_out, "")

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/gh")

    rc = gh_main.main([
        "issue", "create", "--repo", "owner/repo",
        "--title", "bug", "--body", "test",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["number"] == 42
```

`tests/test_gh_repo_whitelist.py`:

```python
def test_repo_not_allowed(monkeypatch, capsys):
    # v2 (B-A2): no TG-token dependency.
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/a")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    from tools.gh import main as gh_main
    rc = gh_main.main([
        "issue", "list", "--repo", "evil/exfil",
    ])
    assert rc == 6
    out = capsys.readouterr().out
    assert "repo_not_allowed" in out
```

`tests/test_gh_pr_view_flattens_author.py` (SF-A5, new):

```python
import json
import subprocess

from tools.gh import main as gh_main


def test_pr_view_flattens_author(monkeypatch, capsys):
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    fake = json.dumps({
        "number": 15, "title": "x", "body": "y", "state": "OPEN",
        "mergeable": "MERGEABLE",
        "author": {"login": "octocat", "url": "https://github.com/octocat"},
    })
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, fake, ""))
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/gh")
    rc = gh_main.main(["pr", "view", "15", "--repo", "owner/repo"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["author"] == "octocat"  # flattened, not dict
```

**Acceptance:** 3+ tests pass (happy issue-create, repo whitelist, SF-A5 pr-view flatten). `mypy` clean on new files.

**Dependencies:** C1, C2.

---

### C4: `vault-commit-push` subcommand + git_ops + flock + vault_git_init

**Files (new + extend):**

- `tools/gh/main.py` (extend with `vault-commit-push`)
- `tools/gh/_lib/git_ops.py` (~160 LOC)
- `tools/gh/_lib/lock.py` (~50 LOC)
- `tools/gh/_lib/vault_git_init.py` (~80 LOC)
- `tests/test_gh_vault_commit_push_happy.py`
- `tests/test_gh_vault_commit_push_no_changes.py`
- `tests/test_gh_vault_commit_push_diverged.py`
- `tests/test_gh_vault_commit_path_isolation.py` (CRITICAL)
- `tests/test_gh_flock_concurrency.py`
- `tests/test_gh_ssh_key_missing.py`
- `tests/test_gh_vault_git_bootstrap.py`
- `tests/test_gh_dispatch_reply_no_artefact_match.py` (Q11 regression corpus)
- `tests/fixtures/gh_responses.txt` (copy from `spikes/phase8/spike_artefact_re_corpus.txt`)

**`tools/gh/_lib/git_ops.py`:**

```python
"""Git subprocess wrappers for vault-commit-push (path-pinned, env-controlled)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

DIVERGED_RE = re.compile(
    r"\b(rejected|non-fast-forward|fetch first|updates were rejected)\b",
    re.IGNORECASE,
)


@dataclass
class GitResult:
    rc: int
    stdout: str
    stderr: str


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env copy with GH_* + ssh helpers scrubbed; never prompts; extra merges on top.

    SF-C2/SF-C3: wipe enterprise tokens, GH_HOST, GH_CONFIG_DIR, GIT_SSH_COMMAND,
    SSH_ASKPASS. `SSH_AUTH_SOCK` is intentionally KEPT — our `-o IdentitiesOnly=yes`
    in the push `GIT_SSH_COMMAND` instructs openssh to ignore agent identities,
    so leaving SSH_AUTH_SOCK set is harmless and avoids breaking unrelated
    ssh tooling that may run from the same shell later.
    """
    env = dict(os.environ)
    for key in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_CONFIG_DIR",
        "GIT_SSH_COMMAND",  # will be set explicitly by `push()` when relevant
        "SSH_ASKPASS",      # avoid password-prompt popups in headless daemon
    ):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout_s: float = 30.0,
    check: bool = False,
) -> GitResult:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=env or _base_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args!r} failed rc={proc.returncode} stderr={proc.stderr}")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def is_inside_work_tree(vault_dir: Path) -> bool:
    r = _run_git(["rev-parse", "--is-inside-work-tree"], vault_dir)
    return r.rc == 0 and r.stdout.strip() == "true"


def porcelain_status(vault_dir: Path) -> str:
    r = _run_git(["status", "--porcelain"], vault_dir, check=True)
    return r.stdout


def stage_all(vault_dir: Path) -> None:
    _run_git(["add", "-A"], vault_dir, check=True)


def commit(
    vault_dir: Path, *, message: str, author_email: str, author_name: str = "vaultbot"
) -> str:
    """Commit via inline `-c user.email=X -c user.name=Y`. Returns HEAD sha.

    SF-A4 nota bene: `-c KEY=VALUE` MUST precede the git subcommand per
    git's argv rules (`git -c user.email=X commit ...` is valid;
    `git commit -c user.email=X` is NOT — `-c` on the subcommand means
    "use this existing commit as a template", a wildly different operation).
    The `_run_git` wrapper prepends `-C <cwd>` so the final argv becomes
    `git -C <vault_dir> -c user.email=... commit --only -m ... -- .` which
    correctly applies the config override only to this invocation.
    """
    _run_git(
        [
            "-c", f"user.email={author_email}",
            "-c", f"user.name={author_name}",
            "commit", "--only", "-m", message, "--", ".",
        ],
        vault_dir,
        check=True,
    )
    head = _run_git(["rev-parse", "HEAD"], vault_dir, check=True)
    return head.stdout.strip()


def unpushed_commit_count(vault_dir: Path) -> int:
    """B-B2: number of commits that are local-only (not on upstream).

    Returns 0 if `@{u}` is not configured (e.g. branch has never been pushed
    before, so no upstream tracking yet). Never raises.
    """
    r = _run_git(["rev-list", "@{u}..HEAD", "--count"], vault_dir)
    if r.rc != 0:
        return 0  # upstream unknown (first push), treat as "nothing unpushed"
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


def reset_soft_head_one(vault_dir: Path) -> None:
    """B-B2: undo the most recent commit while keeping the tree dirty.

    Called when `push()` returns `diverged` — we drop the commit that
    failed to push so the owner's working-tree changes (still in the index
    after `stage_all`) are preserved. Next run will re-stage + re-commit.
    """
    _run_git(["reset", "--soft", "HEAD~1"], vault_dir, check=True)


def diff_cached_empty(vault_dir: Path) -> bool:
    """Return True iff `git diff --cached --quiet` returns 0 (nothing staged)."""
    r = _run_git(["diff", "--cached", "--quiet"], vault_dir)
    return r.rc == 0


def files_changed_count(vault_dir: Path) -> int:
    r = _run_git(["show", "--name-only", "--pretty=format:", "HEAD"], vault_dir)
    return len([line for line in r.stdout.splitlines() if line.strip()])


def push(
    vault_dir: Path,
    *,
    remote: str,
    branch: str,
    ssh_key_path: Path,
    known_hosts_path: Path,
) -> tuple[GitResult, str]:
    """Return (result, classification). classification ∈ {'ok','diverged','failed'}."""
    ssh_cmd = " ".join([
        "ssh",
        "-i", shlex.quote(str(ssh_key_path)),
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts_path))}",
    ])
    env = _base_env({"GIT_SSH_COMMAND": ssh_cmd})
    r = _run_git(["push", remote, branch], vault_dir, env=env, timeout_s=60.0)
    if r.rc == 0:
        return r, "ok"
    if DIVERGED_RE.search(r.stderr):
        return r, "diverged"
    return r, "failed"
```

**`tools/gh/_lib/lock.py`:**

```python
"""fcntl.flock wrapper for vault-commit-push cross-process single-flight."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path


class LockBusyError(Exception):
    pass


@contextlib.contextmanager
def flock_exclusive_nb(lock_path: Path):
    """Exclusive non-blocking flock context. BlockingIOError → LockBusyError.

    Kernel auto-releases on process death (spike R-4 verified).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusyError(str(lock_path)) from exc
        try:
            yield fd
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
```

**`tools/gh/_lib/vault_git_init.py`:**

```python
"""Bootstrap a brand-new vault_dir into a git repo on first vault-commit-push."""

from __future__ import annotations

from pathlib import Path

from tools.gh._lib.git_ops import _run_git


def bootstrap(
    vault_dir: Path,
    *,
    remote_name: str,
    remote_url: str,
    branch: str,
    author_email: str,
    author_name: str = "vaultbot",
) -> None:
    """Initialize `vault_dir` as a git repo with remote + empty bootstrap commit.

    Steps (all subprocess with GIT_TERMINAL_PROMPT=0 env):
      1. git init -b <branch>
      2. write `.gitignore` containing `.tmp/` (SF-D7 — vault writes
         temp files under `.tmp/` during memory indexing that should
         never ship to the backup remote)
      3. git remote add <remote_name> <remote_url>
      4. git -c user.email=X -c user.name=Y add .gitignore && commit --allow-empty
         -m "bootstrap" (seed both the gitignore and the empty-parent commit)

    Does NOT push — caller pushes after real content is staged.
    """
    _run_git(["init", "-q", "-b", branch], vault_dir, check=True)

    # SF-D7: seed .gitignore with .tmp/ exclusion BEFORE first commit so
    # test_gh_vault_commit_path_isolation (and real runs) never pick up
    # memory-indexer scratch files.
    gitignore = vault_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# Auto-generated by vault_git_init bootstrap (phase 8)\n"
            ".tmp/\n"
            "*.tmp\n",
            encoding="utf-8",
        )

    _run_git(
        ["remote", "add", remote_name, remote_url],
        vault_dir,
        check=True,
    )
    # Stage the gitignore explicitly, then create the bootstrap commit that
    # also carries it. `--allow-empty` is kept for the zero-files case
    # where `.gitignore` somehow pre-existed with identical content.
    _run_git(["add", ".gitignore"], vault_dir, check=True)
    _run_git(
        [
            "-c", f"user.email={author_email}",
            "-c", f"user.name={author_name}",
            "commit", "--allow-empty", "-q", "-m", "bootstrap",
        ],
        vault_dir,
        check=True,
    )
```

**`tools/gh/main.py` — `vault-commit-push` prescription:**

Add subparser (inside `_build_parser`):

```python
vcp = sub.add_parser("vault-commit-push")
vcp.add_argument("--message", default=None)
vcp.add_argument("--dry-run", action="store_true")
```

Add command handler — **v2 (B-A2/B-A3/B-B2) rewrite**:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from assistant.config import GitHubSettings  # sub-model — no TG tokens required
from tools.gh._lib import repo_allowlist
from tools.gh._lib.git_ops import (
    commit, files_changed_count, is_inside_work_tree,
    porcelain_status, push, reset_soft_head_one, stage_all,
    unpushed_commit_count,
)
from tools.gh._lib.lock import LockBusyError, flock_exclusive_nb
from tools.gh._lib.vault_git_init import bootstrap


def _render_message(template: str, tz_name: str) -> str:
    date_str = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    return template.format(date=date_str)


def _ssh_key_readable(path: Path) -> tuple[bool, str]:
    if not path.is_file():
        return False, f"not a file: {path}"
    if not os.access(path, os.R_OK):
        return False, f"not readable: {path}"
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            # warning, not fatal
            return True, f"permissive_mode:{oct(mode)}"
    except OSError as exc:
        return False, f"stat failed: {exc}"
    return True, ""


def _do_push_cycle(
    *,
    vault_dir: Path,
    gh: GitHubSettings,
    known_hosts: Path,
    stage: bool,
    message: str | None,
) -> tuple[int, dict]:
    """Single commit-then-push cycle. Returns (exit_code, payload_dict).

    B-B2 helper. Two entry modes:
      - `stage=True`  → stage_all + commit + push (the usual path)
      - `stage=False` → push-only retry of an already-existing local commit
        (a previous run committed but push failed; we don't re-stage because
        the working tree may have drifted since then).
    """
    if stage:
        stage_all(vault_dir)
        # Re-check after add — nothing actually made it into the index.
        from tools.gh._lib.git_ops import diff_cached_empty as _dce
        if _dce(vault_dir):
            return ec.NO_CHANGES, {"ok": True, "no_changes": True, "race": True}
        sha = commit(
            vault_dir,
            message=message or "",
            author_email=gh.commit_author_email,
        )
    else:
        # Push-only path: HEAD is already the commit we want to ship.
        from tools.gh._lib.git_ops import _run_git
        head = _run_git(["rev-parse", "HEAD"], vault_dir, check=True)
        sha = head.stdout.strip()

    files_n = files_changed_count(vault_dir)

    push_result, verdict = push(
        vault_dir,
        remote=gh.vault_remote_name,
        branch=gh.vault_branch,
        ssh_key_path=gh.vault_ssh_key_path,
        known_hosts_path=known_hosts,
    )
    if verdict == "ok":
        return ec.OK, {"ok": True, "commit_sha": sha, "files_changed": files_n,
                       "retried_unpushed": not stage}
    if verdict == "diverged":
        # B-B2: protect next run from silent data loss — undo the commit so
        # the working tree stays dirty and the next invocation re-attempts.
        # Only do this in the `stage=True` path; push-only retry keeps the
        # local commit so the owner can inspect it manually.
        if stage:
            try:
                reset_soft_head_one(vault_dir)
            except Exception as exc:  # pragma: no cover — last-resort
                return ec.DIVERGED, {
                    "ok": False, "error": "diverged_and_reset_failed",
                    "commit_sha": sha, "reset_error": repr(exc),
                    "stderr": push_result.stderr[:300],
                }
        return ec.DIVERGED, {
            "ok": False, "error": "remote has diverged",
            "commit_sha": sha, "reset": stage,
            "stderr": push_result.stderr[:300],
        }
    return ec.PUSH_FAILED, {
        "ok": False, "error": "push_failed",
        "stderr": push_result.stderr[:300],
    }


def _cmd_vault_commit_push(args: argparse.Namespace) -> int:
    """v2 execution flow (B-A2 direct sub-model, B-A3 mkdir, B-B2 unpushed):

    1. Instantiate `GitHubSettings()` directly (no `get_settings()` — see B-A2).
    2. `vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)` — fresh
       install + user-deletes-vault race survive (B-A3). Replaces the v1
       `is_dir()` guard which exited 3 on first run before the daemon
       created the dir.
    3. Allow-list / ssh-key / URL-regex validation (unchanged from v1).
    4. If `--dry-run`: skip flock (read-only; SF-B3), skip bootstrap bypass,
       print porcelain + planned message, exit 0.
    5. Acquire flock.
    6. Bootstrap repo if not yet initialised.
    7. **B-B2 unpushed-detection step:** before porcelain check, if
       `unpushed_commit_count(vault_dir) > 0` we go straight into the
       push-only retry path (no re-stage, no re-commit). Outcome maps
       exactly like the normal push path.
    8. Porcelain change detection. Empty → exit 5 `no_changes`.
    9. Stage + commit + push via `_do_push_cycle(stage=True)`.
    10. On divergence from step 9, `_do_push_cycle` already invoked
        `reset_soft_head_one` so the working tree stays dirty (owner's
        changes preserved). Exit 7 carries `"reset": true` in payload.
    """
    gh = GitHubSettings()
    data_dir = _data_dir()
    vault_dir = _vault_dir()

    # Step 2 (B-A3): create vault_dir unconditionally. `mode=0o700` mirrors
    # the existing `memory` tool's assumptions about vault privacy.
    try:
        vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        print(json.dumps({
            "ok": False, "error": "vault_mkdir_failed",
            "path": str(vault_dir), "detail": repr(exc),
        }))
        return ec.VALIDATION

    # Step 3: allow-list check before ANY subprocess / ssh.
    if not gh.vault_remote_url:
        print(json.dumps({"ok": False, "error": "vault_remote_url_unset"}))
        return ec.VALIDATION
    slug = repo_allowlist.extract_owner_repo(gh.vault_remote_url)
    if slug is None:
        print(json.dumps({"ok": False, "error": "bad_remote_url"}))
        return ec.VALIDATION
    if not repo_allowlist.is_allowed(slug, gh.allowed_repos):
        print(json.dumps({"ok": False, "error": "repo_not_allowed", "repo": slug}))
        return ec.REPO_NOT_ALLOWED

    # Step 4: ssh key check (skip for `file://` remotes — test-only).
    using_ssh = gh.vault_remote_url.startswith("git@")
    if using_ssh:
        key_ok, key_note = _ssh_key_readable(gh.vault_ssh_key_path)
        if not key_ok:
            print(json.dumps({
                "ok": False, "error": "ssh_key_error", "detail": key_note,
            }))
            return ec.SSH_KEY_ERROR
        if key_note.startswith("permissive_mode:"):
            print(f"warning: ssh key {key_note}", file=sys.stderr)

    # Step 5 (SF-B3): dry-run is read-only → bypass flock entirely.
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "run").mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / "run" / "gh-vault-commit.lock"
    known_hosts = data_dir / "run" / "gh-vault-known-hosts"

    if args.dry_run:
        if not is_inside_work_tree(vault_dir):
            print(json.dumps({
                "ok": True, "dry_run": True,
                "would_bootstrap": True,
                "vault_dir": str(vault_dir),
            }))
            return ec.OK
        porcelain = porcelain_status(vault_dir)
        print(json.dumps({
            "ok": True, "dry_run": True,
            "porcelain": porcelain,
            "unpushed_commits": unpushed_commit_count(vault_dir),
            "planned_message": _render_message(
                gh.commit_message_template, gh.auto_commit_tz
            ),
        }))
        return ec.OK

    # Step 6: flock (write path only).
    try:
        with flock_exclusive_nb(lock_path):
            # Step 7: bootstrap if needed.
            if not is_inside_work_tree(vault_dir):
                bootstrap(
                    vault_dir,
                    remote_name=gh.vault_remote_name,
                    remote_url=gh.vault_remote_url,
                    branch=gh.vault_branch,
                    author_email=gh.commit_author_email,
                )

            # Step 8 (B-B2): detect unpushed commits FIRST. Non-zero means
            # a prior run committed locally but push failed for a reason that
            # cleared before this run — try the push-only retry before
            # scanning for new changes.
            unpushed = unpushed_commit_count(vault_dir)
            if unpushed > 0:
                # Push-only retry; no stage, no new commit.
                rc, payload = _do_push_cycle(
                    vault_dir=vault_dir, gh=gh, known_hosts=known_hosts,
                    stage=False, message=None,
                )
                payload["retried_unpushed_count"] = unpushed
                print(json.dumps(payload))
                return rc

            # Step 9: change detection via porcelain.
            porcelain = porcelain_status(vault_dir)
            if not porcelain.strip():
                print(json.dumps({"ok": True, "no_changes": True}))
                return ec.NO_CHANGES

            # Step 10: render message.
            message = args.message or _render_message(
                gh.commit_message_template, gh.auto_commit_tz
            )

            # Step 11: stage + commit + push (with divergence reset inside).
            rc, payload = _do_push_cycle(
                vault_dir=vault_dir, gh=gh, known_hosts=known_hosts,
                stage=True, message=message,
            )
            print(json.dumps(payload))
            return rc
    except LockBusyError:
        print(json.dumps({"ok": False, "error": "lock_busy"}))
        return ec.LOCK_BUSY
```

Hook `vault-commit-push` into `main()` dispatch.

**Tests (critical):**

1. `test_gh_vault_commit_push_happy.py` — happy path with monkeypatched `subprocess.run` routing to a fake `git` that emulates all transitions. Assert exit 0, stdout is JSON with `ok=true`, `commit_sha` is a 40-char hex. Verify `GIT_SSH_COMMAND` env contained `IdentitiesOnly=yes`.

2. `test_gh_vault_commit_push_no_changes.py` — monkeypatch `porcelain_status` to return empty string → exit 5.

3. `test_gh_vault_commit_push_diverged.py` — push stderr contains `! [rejected]  main -> main (non-fast-forward)` → exit 7.

4. `test_gh_vault_commit_path_isolation.py` — CRITICAL (I-8.1). Use real git. Seed:
   - `<tmp>/data_dir/vault/note.md` (the vault file)
   - `<tmp>/data_dir/media/outbox/leak.png` (must NOT be committed)
   - `<tmp>/data_dir/assistant.db` (must NOT be committed)
   - `<tmp>/data_dir/run/tmp/junk.txt` (must NOT be committed)

   Point `GH_VAULT_REMOTE_URL=file:///tmp/bare.git`, `ASSISTANT_DATA_DIR=<tmp>/data_dir`. Run `python tools/gh/main.py vault-commit-push`. Assert `git show --stat HEAD` on the bare repo shows only `note.md` changed; no mention of `leak.png`, `assistant.db`, `junk.txt`.

5. `test_gh_flock_concurrency.py` — use `multiprocessing` to spawn two processes, each calling a fake `vault-commit-push` that holds the flock for 5 seconds. Assert second exits 9 immediately (within 100 ms).

6. `test_gh_ssh_key_missing.py` — `GH_VAULT_SSH_KEY_PATH=/nonexistent/id_vault` → exit 10 (v2 SF-F3 rename).

7. `test_gh_vault_git_bootstrap.py` — empty `<tmp>/vault_dir/` (no .git). Run CLI. Assert `.git` directory was created, `.gitignore` contains `.tmp/`, `remote -v` shows `vault-backup`, bootstrap commit is HEAD's parent, real note.md commit is HEAD.

8. `test_gh_dispatch_reply_no_artefact_match.py` — load corpus from `tests/fixtures/gh_responses.txt`, assert for each line `ARTEFACT_RE.search(line) is None`. **v2 SF-D2 correction:** import is `from assistant.media.artefacts import ARTEFACT_RE` (NOT `adapters/`). Verified path in repo during fix-pack research.

9. **v2 new — B-A3 mkdir:** `test_gh_vault_commit_push_mkdir_fresh.py`. Point `ASSISTANT_DATA_DIR=<tmp>/fresh` (dir does not exist at all). Configure `GH_VAULT_REMOTE_URL=file:///<tmp>/bare.git`, allow-list + `git init --bare`. Run `python tools/gh/main.py vault-commit-push --dry-run`. Assert: rc==0, `<tmp>/fresh/vault` was created with mode `0o700`, and dry-run output reports `"would_bootstrap": true`.

10. **v2 new — B-B2 unpushed retry:** `test_gh_vault_commit_push_unpushed_retry.py`. Two-stage real-git test:
    - Stage 1: configure `GH_VAULT_REMOTE_URL=file:///<tmp>/bare.git`, run CLI. Break bare repo path (rename to make push fail — simulates network blip) AFTER local commit is made. Assert: exit 8 `push_failed`, unpushed count == 1.
    - Stage 2: restore bare repo. Run CLI again WITHOUT touching the vault file. Assert: exit 0, payload has `retried_unpushed=True` + `retried_unpushed_count=1`, bare repo now contains the commit from stage 1. No new commit was created in stage 2.

11. **v2 new — B-B2 divergence reset:** `test_gh_vault_commit_push_diverged_resets.py`. Seed vault with one commit already in bare repo. Create local commit that conflicts (e.g. push another commit directly into bare from a second clone to simulate divergence). Run CLI. Assert: exit 7, payload has `"reset": true`, `git log` on vault_dir shows the local commit was undone (HEAD points at the previous sha), `git status --porcelain` is non-empty (working-tree changes preserved).

12. **v2 new — SF-B3 dry-run skips flock:** `test_gh_vault_commit_push_dry_run_no_flock.py`. Acquire flock manually in a second process (hold for 30 s). While held, run `python tools/gh/main.py vault-commit-push --dry-run`. Assert rc==0 (not 9) — dry-run bypasses the lock.

**Acceptance:**

- `python tools/gh/main.py vault-commit-push --dry-run` on a non-vault dir → exit 3 `vault_not_configured`.
- With a configured vault + dirty state → exit 0, JSON emitted, `commit_sha` field populated.

**Dependencies:** C1, C2, C3.

---

### C5: Migration `0005_schedule_seed_key.sql` + `0006_seed_tombstones.sql` + seed helper

**Files:**

- `src/assistant/state/migrations/0005_schedule_seed_key.sql` (new)
- `src/assistant/state/migrations/0006_seed_tombstones.sql` (new)
- `src/assistant/state/db.py` (modify: bump `SCHEMA_VERSION=6`, add `_apply_v5` + `_apply_v6`)
- `src/assistant/scheduler/store.py` (modify: extend `insert_schedule` with `seed_key`; add `find_by_seed_key`, `tombstone_exists`, `insert_tombstone`, `delete_tombstone`). **v2 B-D1:** the existing `delete_schedule` is LEFT UNCHANGED — the tombstone logic lives in `tools/schedule/main.py::cmd_rm` (sync sqlite3) instead, consistent with the phase-5 soft-delete pattern.
- `src/assistant/scheduler/seed.py` (new, ~70 LOC)
- `tools/schedule/main.py` (modify: `cmd_rm` soft-delete + inline tombstone insert + new `cmd_revive_seed` — all sync sqlite3)
- `tests/test_gh_seed_idempotency.py`
- `tests/test_gh_seed_disabled.py`
- `tests/test_gh_seed_tombstone.py`
- `tests/test_gh_migration_v5_v6.py`

**`0005_schedule_seed_key.sql` (exact content):**

```sql
-- 0005_schedule_seed_key.sql — phase 8 (idempotent vault_auto_commit seed)
--
-- Adds `seed_key TEXT` column (NULLable) to schedules and a partial
-- UNIQUE INDEX that ignores NULLs. Pre-existing rows keep `seed_key
-- IS NULL` and are unaffected; new default-seeded rows carry a stable
-- key (e.g. 'vault_auto_commit') that the unique index prevents from
-- duplicating on Daemon restart.
--
-- Partial index (WHERE seed_key IS NOT NULL) is SQLite 3.8+; confirmed
-- supported by aiosqlite bundle (spike R-6).

ALTER TABLE schedules ADD COLUMN seed_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key
    ON schedules(seed_key) WHERE seed_key IS NOT NULL;

PRAGMA user_version = 5;
```

**`0006_seed_tombstones.sql` (exact content):**

```sql
-- 0006_seed_tombstones.sql — phase 8 Q10 (owner-deleted-seed marker)
--
-- When `tools/schedule/main.py rm <id>` deletes a row with a non-NULL
-- seed_key, we INSERT into this table so the next `Daemon.start()`
-- does NOT re-seed autonomously. The owner explicitly uses
-- `revive-seed <key>` to re-enable.

CREATE TABLE IF NOT EXISTS seed_tombstones (
    seed_key   TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

PRAGMA user_version = 6;
```

**`src/assistant/state/db.py` diff:**

```python
SCHEMA_VERSION = 6  # was 4


async def _apply_v5(conn: aiosqlite.Connection) -> None:
    """Phase 8: `schedules.seed_key` + partial UNIQUE INDEX. Additive.

    v2 SF-F2: avoid `executescript` (which auto-commits per statement on
    aiosqlite, bypassing our `BEGIN IMMEDIATE`). Execute each statement
    individually so the whole migration is one transaction.
    """
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("ALTER TABLE schedules ADD COLUMN seed_key TEXT")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedules_seed_key "
            "ON schedules(seed_key) WHERE seed_key IS NOT NULL"
        )
        await conn.execute("PRAGMA user_version = 5")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def _apply_v6(conn: aiosqlite.Connection) -> None:
    """Phase 8 Q10: seed_tombstones table. v2 SF-F2: explicit stmts, no executescript."""
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS seed_tombstones ("
            "  seed_key   TEXT PRIMARY KEY,"
            "  deleted_at TEXT NOT NULL DEFAULT "
            "  (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
            ")"
        )
        await conn.execute("PRAGMA user_version = 6")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def apply_schema(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    current = row[0] if row else 0

    if current < 1:
        await _apply_v1(conn); current = 1
    if current < 2:
        await _apply_v2(conn); current = 2
    if current < 3:
        await _apply_v3(conn); current = 3
    if current < 4:
        await _apply_v4(conn); current = 4
    if current < 5:
        await _apply_v5(conn); current = 5
    if current < 6:
        await _apply_v6(conn); current = 6
```

**`src/assistant/scheduler/store.py` extension:**

```python
# Extend insert_schedule:
async def insert_schedule(
    self, *, cron: str, prompt: str, tz: str = "UTC", seed_key: str | None = None
) -> int:
    async with self._lock:
        cur = await self._conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
            "VALUES (?, ?, ?, 1, ?)",
            (cron, prompt, tz, seed_key),
        )
        await self._conn.commit()
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


async def find_by_seed_key(self, seed_key: str) -> dict[str, Any] | None:
    async with self._conn.execute(
        "SELECT id, cron, prompt, tz, enabled, seed_key FROM schedules WHERE seed_key=?",
        (seed_key,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0], "cron": row[1], "prompt": row[2],
        "tz": row[3], "enabled": bool(row[4]), "seed_key": row[5],
    }


async def tombstone_exists(self, seed_key: str) -> bool:
    async with self._conn.execute(
        "SELECT 1 FROM seed_tombstones WHERE seed_key=?", (seed_key,)
    ) as cur:
        row = await cur.fetchone()
    return row is not None


async def insert_tombstone(self, seed_key: str) -> None:
    """Async insert kept for symmetry / future callers. v2 note:
    the `rm` CLI does NOT call this — it inserts via sync sqlite3 inside
    `cmd_rm`'s own `BEGIN IMMEDIATE` (B-D1). Tests may call this method
    directly to set up tombstone-exists scenarios.
    """
    async with self._lock:
        await self._conn.execute(
            "INSERT OR REPLACE INTO seed_tombstones(seed_key) VALUES (?)",
            (seed_key,),
        )
        await self._conn.commit()


async def delete_tombstone(self, seed_key: str) -> bool:
    """Async delete kept for symmetry / future callers. v2 note:
    the `revive-seed` CLI does NOT call this — it deletes via sync
    sqlite3 inside `cmd_revive_seed` (B-D1). Tests may call this.
    """
    async with self._lock:
        cur = await self._conn.execute(
            "DELETE FROM seed_tombstones WHERE seed_key=?", (seed_key,)
        )
        await self._conn.commit()
    return (cur.rowcount or 0) > 0
```

**v2 B-B1 / B1 tightening** — the seed helper's two-step check (`tombstone_exists` then `find_by_seed_key` then `insert_schedule`) must run inside a **`BEGIN IMMEDIATE` transaction** so a concurrent inserter (hypothetically — we hold the pidfile flock, so this is defence-in-depth) cannot slip a row in between the check and the insert. Add a helper to the store:

```python
async def ensure_seed_row(
    self, *, seed_key: str, cron: str, prompt: str, tz: str
) -> tuple[int, str]:
    """Atomic tombstone-check + find + insert.

    Returns `(schedule_id, action)` where action in
    {"exists", "tombstoned", "inserted"}.
    """
    async with self._lock:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            async with self._conn.execute(
                "SELECT 1 FROM seed_tombstones WHERE seed_key=?",
                (seed_key,),
            ) as cur:
                if await cur.fetchone():
                    await self._conn.rollback()
                    return (0, "tombstoned")
            async with self._conn.execute(
                "SELECT id FROM schedules WHERE seed_key=?", (seed_key,)
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                await self._conn.rollback()
                return (int(row[0]), "exists")
            cur = await self._conn.execute(
                "INSERT INTO schedules(cron, prompt, tz, enabled, seed_key) "
                "VALUES (?, ?, ?, 1, ?)",
                (cron, prompt, tz, seed_key),
            )
            await self._conn.commit()
            assert cur.lastrowid is not None
            return (int(cur.lastrowid), "inserted")
        except Exception:
            await self._conn.rollback()
            raise
```

The seed helper (`scheduler/seed.py`) is simplified accordingly — it delegates the critical section to `ensure_seed_row` and only layers on the `GitHubSettings` gating (disabled / empty URL / missing ssh key).

**v2 B-D1: `SchedulerStore.delete_schedule` is NOT modified.**

The v1 plan overwrote `delete_schedule` to async-insert a tombstone and hard-DELETE the row. That conflicted with two existing contracts:

1. **`tools/schedule/main.py::cmd_rm` is sync `sqlite3`** (not aiosqlite) and **soft-deletes** via `UPDATE schedules SET enabled=0` — the phase-5 G-W2-x guarantee that trigger-history rows keep their FK parent for observability.
2. **`SchedulerStore.delete_schedule` callers** are tests only; they rely on the existing `bool` return shape and the hard-DELETE semantics.

v2 keeps `SchedulerStore.delete_schedule` untouched. Instead the tombstone insert is **co-located inside `cmd_rm`** (sync sqlite3, same `BEGIN IMMEDIATE` transaction as the soft-delete) so `rm` remains atomic from the owner's perspective.

**`src/assistant/scheduler/seed.py` full content:**

```python
"""Phase 8 default-seed for vault_auto_commit schedule.

Idempotent: check tombstones, check `find_by_seed_key`, INSERT only if
both empty. The UNIQUE INDEX idx_schedules_seed_key is the last-barrier
race guard in case two daemons somehow bypass the pidfile flock.
"""

from __future__ import annotations

from assistant.config import GitHubSettings
from assistant.logger import get_logger
from assistant.scheduler.store import SchedulerStore

log = get_logger("scheduler.seed")

SEED_KEY_VAULT_AUTO_COMMIT = "vault_auto_commit"
SEED_PROMPT = (
    "ежедневный бэкап vault: сделай git add data/vault, коммит и git push"
)


async def ensure_vault_auto_commit_seed(
    store: SchedulerStore, gh: GitHubSettings
) -> int | None:
    """Return new schedule_id if inserted, existing id if present, None if skipped.

    v2 B-B1: delegates the critical section (tombstone-check + find + insert)
    to `store.ensure_seed_row`, which wraps all three SQL ops in a
    `BEGIN IMMEDIATE` transaction. Any GitHubSettings-level gating happens
    BEFORE the transaction so we don't hold a write lock while deciding
    whether we should do anything at all.
    """
    if not gh.auto_commit_enabled:
        log.info("vault_auto_commit_seed_skipped_disabled")
        return None
    if not gh.vault_remote_url:
        log.warning("vault_remote_not_configured")
        return None
    if not gh.vault_ssh_key_path.is_file():
        log.warning("vault_ssh_key_missing", path=str(gh.vault_ssh_key_path))
        return None

    sid, action = await store.ensure_seed_row(
        seed_key=SEED_KEY_VAULT_AUTO_COMMIT,
        cron=gh.auto_commit_cron,
        prompt=SEED_PROMPT,
        tz=gh.auto_commit_tz,
    )
    if action == "tombstoned":
        log.info("vault_auto_commit_seed_tombstoned_skip")
        return None
    if action == "exists":
        log.info("vault_auto_commit_seed_present", schedule_id=sid)
        return sid
    # action == "inserted"
    log.info(
        "vault_auto_commit_seed_created",
        schedule_id=sid, cron=gh.auto_commit_cron, tz=gh.auto_commit_tz,
    )
    return sid
```

**v2 `tools/schedule/main.py` extension — sync sqlite3 + soft-delete + inline tombstone insert:**

Replaces the existing `cmd_rm` body AND adds a new `cmd_revive_seed`. Both are **sync `sqlite3`**, mirroring the existing patterns (`_connect`, `_ok`, `_fail`, manual `BEGIN IMMEDIATE` + `commit` / `rollback`).

```python
# tools/schedule/main.py — REPLACE the existing cmd_rm body:

def cmd_rm(args: argparse.Namespace) -> int:
    """Soft-delete a schedule (`UPDATE enabled=0`).

    v2 addition: if the row carries a non-NULL `seed_key`, insert into
    `seed_tombstones` in the same `BEGIN IMMEDIATE` transaction so the
    Daemon's next `ensure_vault_auto_commit_seed` call finds the
    tombstone and skips re-seeding. Hard-DELETE is still avoided because
    trigger history rows reference schedules via FK (phase-5 G-W2-x).
    """
    conn = _connect()
    try:
        # Pre-check: row exists? Capture seed_key if so.
        cur = conn.execute(
            "SELECT seed_key FROM schedules WHERE id=?", (args.id,)
        )
        row = cur.fetchone()
        if row is None:
            return _fail(EXIT_NOT_FOUND, "schedule not found", id=args.id)
        seed_key = row[0]

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE schedules SET enabled=0 WHERE id=?", (args.id,)
            )
            tombstoned: str | None = None
            if seed_key:
                conn.execute(
                    "INSERT OR REPLACE INTO seed_tombstones(seed_key) "
                    "VALUES (?)",
                    (seed_key,),
                )
                tombstoned = seed_key
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        payload: dict[str, object] = {"id": args.id, "deleted": True}
        if tombstoned:
            payload["tombstoned_seed_key"] = tombstoned
        return _ok(payload)
    finally:
        conn.close()


# tools/schedule/main.py — ADD new subcommand:

def cmd_revive_seed(args: argparse.Namespace) -> int:
    """Remove a seed_key tombstone so the Daemon re-creates the seed row
    on next start. No-op if no tombstone exists (reports `revived=False`).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM seed_tombstones WHERE seed_key=?",
            (args.seed_key,),
        )
        conn.commit()
        return _ok({
            "seed_key": args.seed_key,
            "tombstone_removed": (cur.rowcount or 0) > 0,
        })
    finally:
        conn.close()
```

Wire into the subparser table (follow the existing `("rm", cmd_rm, ...)` tuple style):

```python
SUBCOMMANDS: list[tuple[str, Callable[..., int], str]] = [
    # ... existing entries ...
    ("rm", cmd_rm, "Soft-delete a schedule (set enabled=0); seeds get tombstoned"),
    ("revive-seed", cmd_revive_seed,
     "Remove a tombstone so the Daemon re-seeds on next start"),
]

# For revive-seed parser:
rs = sub.add_parser("revive-seed")
rs.add_argument("seed_key", help="e.g. `vault_auto_commit`")
```

**Note on seed helper (`src/assistant/scheduler/seed.py`):** the helper's ordering is already correct for v2:

1. `tombstone_exists(seed_key)` → if True: skip (respects `cmd_rm`'s tombstone).
2. `find_by_seed_key(seed_key)` → if a row exists (including `enabled=0` after soft-delete), skip — don't resurrect a disabled row.
3. Otherwise INSERT.

The `enabled=0` soft-delete state means `find_by_seed_key` still finds the row — this is the belt-and-suspenders second guard in case a race between two daemons inserted a tombstone only on one and the other reached the seed step first. The partial UNIQUE INDEX from 0005 is the final barrier.

**Tests:**

- `test_gh_seed_idempotency.py` — call `ensure_vault_auto_commit_seed` twice; assert exactly 1 row with `seed_key='vault_auto_commit'`.
- `test_gh_seed_disabled.py` — `auto_commit_enabled=False` → 0 rows, log line `vault_auto_commit_seed_skipped_disabled`.
- `test_gh_seed_tombstone.py` — **v2 rewrite for sync cmd_rm path**:
  1. `await ensure_vault_auto_commit_seed` → row inserted with `seed_key='vault_auto_commit'`.
  2. Invoke `tools/schedule/main.py rm <id>` via `subprocess.run([sys.executable, "tools/schedule/main.py", "rm", str(sid)])`. Assert stdout JSON has `tombstoned_seed_key='vault_auto_commit'` AND the schedules row has `enabled=0` (NOT hard-deleted).
  3. Re-open async store; call `ensure_vault_auto_commit_seed` → returns `None`, no new row inserted. Assert schedules count still 1 (the soft-deleted row).
  4. Invoke `tools/schedule/main.py revive-seed vault_auto_commit` via subprocess. Assert stdout JSON has `tombstone_removed=True`.
  5. `ensure_vault_auto_commit_seed` → skipped with `action=="exists"` (the soft-deleted row is still there). To get a fresh seed after revive the owner would need `enable <id>` first — document this in `docs/ops/github-setup.md`.
- **v2 SF-D5 new:** `test_gh_seed_tombstone_nullable_branch.py` — insert a schedule with `seed_key=NULL` (user-created row via `cmd_add`), run `cmd_rm <id>`. Assert: row soft-deleted, `seed_tombstones` count unchanged (no tombstone inserted — tombstones only for non-NULL seed_key).
- `test_gh_migration_v5_v6.py` — bring a v3 schema up to v6, seed 5 v3-rows, verify post-migration all 5 have `seed_key IS NULL`; verify `idx_schedules_seed_key` exists and is partial; duplicate insert of `seed_key='x'` raises `IntegrityError`; `seed_tombstones` table exists; `user_version == 6`.
  - **v2 SF-D3 addition:** assert the index's SQL text contains `WHERE seed_key IS NOT NULL` by querying `sqlite_master`:
    ```python
    r = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_schedules_seed_key'"
    )
    sql = (await r.fetchone())[0]
    assert "WHERE seed_key IS NOT NULL" in sql.upper().replace("`", "").replace("\"", "")
    ```

**Dependencies:** C1.

---

### C6: Daemon integration + `_validate_gh_argv` + preflight helpers

**Files:**

- `src/assistant/main.py` (modify `Daemon.start`)
- `src/assistant/bridge/hooks.py` (add `_validate_gh_argv` + dispatch in `_validate_python_invocation`)
- `tests/test_gh_bash_hook_integration.py`
- `tests/test_gh_validate_argv.py`

**`src/assistant/bridge/hooks.py` — `_validate_gh_argv`:**

Insert near `_validate_gh_invocation` (that one handles direct `gh api` calls — KEEP it; this one handles `python tools/gh/main.py ...`):

```python
_GH_CLI_SUBCMDS = frozenset({
    "auth-status", "issue", "pr", "repo", "vault-commit-push",
})
# issue sub-sub not allowed (phase 9). v2 SF-C6: added `develop`, `pin`,
# `unpin`, `status` to match the gh 2.89 issue verb list; these were
# previously allowed by default which would have let an LLM call them.
_GH_ISSUE_FORBIDDEN = frozenset({
    "close", "comment", "edit", "delete", "reopen", "transfer",
    "unlock", "lock", "develop", "pin", "unpin", "status",
})
_GH_PR_FORBIDDEN = frozenset({
    "create", "merge", "close", "comment", "edit", "delete",
    "review", "ready", "checkout", "checks",
})
# v2 SF-C6: `repo` only permits `view`. Any other sub-sub (clone, create,
# edit, archive, delete, rename, sync, ...) is denied even if it LOOKS
# read-only, because gh mutates local state (e.g. clone writes to cwd).
_GH_REPO_ALLOWED_SUBSUB = frozenset({"view"})
_GH_FORBIDDEN_FLAGS = frozenset({
    "--force", "--force-with-lease", "--no-verify", "--amend",
    "-X", "--method",        # no POST/PUT/PATCH/DELETE
    "--body-file",            # SF-C6: file-based body reads bypass body-size cap
})


def _validate_gh_argv(args: list[str]) -> str | None:
    """Validate argv AFTER `python tools/gh/main.py`. Empty → error."""
    if not args:
        return "gh CLI requires a subcommand"
    sub = args[0]
    if sub not in _GH_CLI_SUBCMDS:
        return f"gh CLI subcommand '{sub}' not allowed"

    # Duplicate-flag guard (prevents e.g. `--repo a/b --repo evil/exfil`).
    seen: set[str] = set()
    for arg in args[1:]:
        if arg.startswith("--"):
            key = arg.split("=", 1)[0]
            if key in seen:
                return f"gh CLI duplicate flag {key}"
            seen.add(key)

    # Forbidden flag matrix.
    for bad in _GH_FORBIDDEN_FLAGS:
        if bad in args or any(a.startswith(bad + "=") for a in args):
            return f"gh CLI flag {bad} not allowed"

    # SF5: --limit numeric cap.
    for i, arg in enumerate(args):
        if arg == "--limit" and i + 1 < len(args):
            try:
                if int(args[i + 1]) > 100:
                    return "gh CLI --limit max 100"
            except ValueError:
                return "gh CLI --limit requires integer"
        elif arg.startswith("--limit="):
            try:
                if int(arg.split("=", 1)[1]) > 100:
                    return "gh CLI --limit max 100"
            except ValueError:
                return "gh CLI --limit requires integer"

    # Sub-subcommand matrix.
    if sub == "issue" and len(args) >= 2:
        subsub = args[1]
        if subsub in _GH_ISSUE_FORBIDDEN:
            return f"gh issue subsub '{subsub}' not allowed (phase 9)"
    if sub == "pr" and len(args) >= 2:
        subsub = args[1]
        if subsub in _GH_PR_FORBIDDEN:
            return f"gh pr subsub '{subsub}' not allowed (phase 9)"
    # v2 SF-C6: `repo` must be `repo view` exactly.
    if sub == "repo":
        if len(args) < 2:
            return "gh repo requires a sub-subcommand"
        subsub = args[1]
        if subsub not in _GH_REPO_ALLOWED_SUBSUB:
            return f"gh repo subsub '{subsub}' not allowed; only 'view'"

    return None
```

**Dispatch into `_validate_python_invocation`:** The function already routes `tools/transcribe/main.py`, `tools/genimage/main.py`, etc. Add a new branch:

```python
# Inside _validate_python_invocation, after existing routing:
if script_rel == "tools/gh/main.py":
    return _validate_gh_argv(argv[2:])
```

(Exact insertion depends on the structure; coder MUST read the existing `_validate_python_invocation` first to match the existing `_validate_render_doc_argv` dispatch pattern byte-for-byte.)

**Tests:**

`tests/test_gh_validate_argv.py`:

```python
import pytest
from assistant.bridge.hooks import _validate_gh_argv

ALLOW = [
    (["auth-status"], None),
    (["issue", "list", "--repo", "owner/a"], None),
    (["issue", "view", "42", "--repo", "owner/a"], None),
    (["issue", "create", "--repo", "o/r", "--title", "t", "--body", "b"], None),
    (["pr", "list", "--repo", "o/r"], None),
    (["pr", "view", "15", "--repo", "o/r"], None),
    (["repo", "view", "o/r"], None),
    (["vault-commit-push"], None),
    (["vault-commit-push", "--message", "x"], None),
    (["vault-commit-push", "--dry-run"], None),
    (["issue", "list", "--repo", "o/r", "--limit", "50"], None),
    (["issue", "list", "--repo", "o/r", "--limit=100"], None),
]

DENY = [
    ([], "subcommand"),
    (["unknown"], "not allowed"),
    (["issue", "close", "1"], "phase 9"),
    (["issue", "comment", "1"], "phase 9"),
    (["issue", "pin", "1"], "phase 9"),      # SF-C6
    (["issue", "unpin", "1"], "phase 9"),    # SF-C6
    (["issue", "develop", "1"], "phase 9"),  # SF-C6
    (["issue", "status"], "phase 9"),        # SF-C6
    (["pr", "create"], "phase 9"),
    (["pr", "merge", "1"], "phase 9"),
    (["issue", "create", "--force"], "not allowed"),
    (["issue", "create", "--body-file", "/tmp/x"], "not allowed"),  # SF-C6
    (["vault-commit-push", "--no-verify"], "not allowed"),
    (["vault-commit-push", "--force-with-lease"], "not allowed"),
    (["issue", "list", "--repo", "a/b", "--repo", "c/d"], "duplicate flag"),
    (["issue", "list", "--limit", "101"], "max 100"),
    (["issue", "list", "--limit=101"], "max 100"),
    (["issue", "list", "--limit", "abc"], "integer"),
    (["auth-status", "-X", "POST"], "not allowed"),
    (["repo"], "sub-subcommand"),              # SF-C6
    (["repo", "clone", "owner/repo"], "only 'view'"),  # SF-C6
    (["repo", "create", "owner/repo"], "only 'view'"), # SF-C6
]

@pytest.mark.parametrize("argv, expected_substr", ALLOW)
def test_allow(argv, expected_substr):
    assert _validate_gh_argv(argv) is expected_substr  # None matches None

@pytest.mark.parametrize("argv, needle", DENY)
def test_deny(argv, needle):
    result = _validate_gh_argv(argv)
    assert result is not None
    assert needle in result
```

`tests/test_gh_bash_hook_integration.py`:

```python
from pathlib import Path
from assistant.bridge.hooks import _validate_bash_argv


def test_bash_argv_routes_gh_cli(tmp_path):
    argv = ["python", "tools/gh/main.py", "auth-status"]
    assert _validate_bash_argv(argv, tmp_path) is None


def test_bash_argv_denies_force_in_vault_commit(tmp_path):
    argv = ["python", "tools/gh/main.py", "vault-commit-push", "--force"]
    reason = _validate_bash_argv(argv, tmp_path)
    assert reason is not None and "not allowed" in reason


def test_phase3_gh_api_still_works(tmp_path):
    """_validate_gh_invocation (direct gh api) NOT regressed."""
    argv = ["gh", "api", "/repos/anthropics/skills/contents/"]
    # Should pass or fail based on phase-3 logic, not our new validator.
    # Assertion depends on the existing allowlist; test asserts it is UNCHANGED.
```

**`src/assistant/main.py` — `Daemon.start` integration:**

Insert after `reverted = await sched_store.clean_slate_sent()` and BEFORE adapter creation:

```python
# Phase 8 (C6): preflight + default-seed.
from tools.gh._lib.gh_ops import build_gh_env
from assistant.scheduler.seed import ensure_vault_auto_commit_seed

gh_settings = self._settings.github

# B7: gh config preflight. Non-fatal; seed still runs.
# v2 SF-E1: helper is now async — `await` from Daemon.start().
_gh_config_ok = await _verify_gh_config_accessible_for_daemon(self._log)

# B8: extend cloud-sync guard to ssh key parent (only if auto-commit will run).
if gh_settings.auto_commit_enabled and gh_settings.vault_remote_url:
    _check_path_not_in_cloud_sync(
        gh_settings.vault_ssh_key_path.parent,
        label="vault_ssh_key_path",
        log=self._log,
    )

# B10: warning if allowed_repos empty.
if (
    gh_settings.auto_commit_enabled
    and gh_settings.vault_remote_url
    and not gh_settings.allowed_repos
):
    self._log.warning(
        "vault_auto_commit_allowed_repos_empty_will_reject",
        hint=(
            "GH_ALLOWED_REPOS is empty; every vault-commit-push will exit 6. "
            "Set GH_ALLOWED_REPOS to a comma-separated list of 'owner/repo' slugs."
        ),
    )

# gh --version preflight (analogous to claude --version).
if shutil.which("gh") is None:
    self._log.warning("gh_cli_not_found_issue_pr_disabled")

# Q10: seed (checks tombstone internally).
seed_id = await ensure_vault_auto_commit_seed(sched_store, gh_settings)
if seed_id:
    self._log.info("vault_auto_commit_seed_ready", schedule_id=seed_id)
```

Plus two helper functions (top-level, co-located with `_check_data_dir_not_in_cloud_sync`):

```python
async def _verify_gh_config_accessible_for_daemon(
    log: structlog.stdlib.BoundLogger, timeout_s: float = 10.0
) -> bool:
    """Phase 8 B7: probe `gh auth status` with current HOME + env-wipe.

    Non-fatal: returns False on any problem so the daemon still starts;
    a warning is logged instructing the operator. Invocations of
    `python tools/gh/main.py issue ...` would return exit 4 if truly broken.

    v2 SF-E1: now `async` — uses asyncio subprocess API + `asyncio.wait_for`
    so the probe is cancellable via task cancellation (the previous sync
    `subprocess.run(timeout=...)` form cannot be interrupted if the
    Daemon is receiving SIGTERM during startup).
    """
    if shutil.which("gh") is None:
        log.warning("gh_not_on_path")
        return False
    env = build_gh_env()
    env["HOME"] = str(Path.home())
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(  # noqa: S603 — argv list, no shell
                "gh", "auth", "status", "--hostname", "github.com",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        _stdout, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("gh_auth_status_probe_timed_out", timeout_s=timeout_s)
        return False
    except OSError as exc:
        log.warning("gh_auth_status_probe_failed", error=repr(exc))
        return False
    rc = proc.returncode or 0
    stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace")
    if rc == 0:
        log.info("gh_auth_preflight_ok")
        return True
    stderr_first = stderr_text.splitlines()[0] if stderr_text else ""
    log.warning(
        "gh_config_not_accessible",
        rc=rc, stderr=stderr_first[:200],
        hint=(
            "Run `gh auth login` as the daemon user, OR ensure the systemd/"
            "launchd service sets HOME correctly."
        ),
    )
    return False


def _check_path_not_in_cloud_sync(
    path: Path, *, label: str, log: structlog.stdlib.BoundLogger
) -> None:
    """Phase 8 B8: warn if `path` is under a known cloud-sync root.

    Unlike `_check_data_dir_not_in_cloud_sync`, this is WARN-only (no
    sys.exit) because ssh keys under iCloud are a known owner pattern
    that mostly works (iCloud caches small files immediately). A delete
    race is less catastrophic than media_dir because the key file is
    never modified at runtime.
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return
    for raw, lbl in _CLOUD_SYNC_ROOTS:
        root = Path(raw).expanduser()
        try:
            root_r = root.resolve()
        except (OSError, RuntimeError):
            continue
        if not root_r.exists():
            continue
        try:
            inside = resolved.is_relative_to(root_r)
        except ValueError:
            inside = False
        if inside:
            log.warning(
                f"{label}_under_sync_folder",
                path=str(resolved), cloud=lbl,
                hint=f"ssh key under {lbl} — sync delays may impact auto-commit.",
            )
            return
```

**Dependencies:** C4, C5.

---

### C7: docs + README/CLAUDE.md mention (E2E test downgraded in v2)

**Files:**

- `docs/ops/github-setup.md` (new, ~180 LOC)
- `README.md` / `CLAUDE.md` (touch — mention phase-8 shipped)

**v2 B-D6 change:** `test_gh_e2e_scheduler_to_commit.py` is **REMOVED from the must-have list**. The component-level tests cover the functional path end-to-end: C4 (happy + no-changes + diverged + unpushed-retry + path-isolation) exercises the CLI write-flow; C5 (seed idempotency + tombstone) exercises the seed lifecycle; C6 (`_validate_gh_argv` + bash-hook integration + preflight) exercises the daemon wiring. Instead of a fragile 150-LOC freezegun + mock-bridge fixture, the acceptance matrix calls for **manual smoke** at §Acceptance step `Live smoke:`.

If a future phase introduces a true E2E harness (e.g. pytest-asyncio Daemon with fake Telegram poller), the E2E test can be added then. Listed as **optional future manual smoke** in `docs/ops/github-setup.md`.

**`docs/ops/github-setup.md` outline:**

1. **Create dedicated `vaultbot-owner` GitHub account** (separate email; free tier sufficient). Rationale: blast-radius isolation. Even if deploy key leaks, attacker gains write access to exactly one private repo.

2. **Create private repo `vaultbot-owner/vault-backup`** via web UI.

3. **Generate deploy key:**

   ```
   ssh-keygen -t ed25519 -f ~/.ssh/id_vault -N "" -C "vault@$(hostname)"
   chmod 600 ~/.ssh/id_vault
   chmod 644 ~/.ssh/id_vault.pub
   ```

   Upload `~/.ssh/id_vault.pub` to the repo's **Deploy keys** settings. **Enable "Allow write access"** (otherwise pushes will fail).

4. **`.env` snippet** — **v2 SF-F3 rename: `GH_VAULT_SSH_KEY_PATH`** (matches the field name in `GitHubSettings` so pydantic-settings picks it up through its `env_prefix="GH_"`; v1 called the var `GH_VAULT_SSH_KEY` which would have silently gone to `extra="ignore"` and left the default in place):

   ```env
   GH_VAULT_REMOTE_URL=git@github.com:vaultbot-owner/vault-backup.git
   GH_VAULT_SSH_KEY_PATH=~/.ssh/id_vault
   GH_ALLOWED_REPOS=vaultbot-owner/vault-backup,c0manch3/0xone-assistant
   GH_AUTO_COMMIT_ENABLED=true
   GH_AUTO_COMMIT_CRON=0 3 * * *
   GH_AUTO_COMMIT_TZ=Europe/Moscow
   GH_COMMIT_AUTHOR_EMAIL=vaultbot@localhost
   ```

   Note: `~` is expanded by the SF-F3 validator; both `~/.ssh/id_vault` and `/Users/you/.ssh/id_vault` are accepted.

5. **Main `gh auth login`** (for issues/PRs on user's main account):

   ```
   gh auth login --hostname github.com --git-protocol ssh
   ```

   Follow the interactive flow; scopes `repo, read:org` sufficient.

6. **Key rotation:**
   - Revoke current key on GitHub repo settings → Deploy keys.
   - Regenerate: `ssh-keygen -t ed25519 -f ~/.ssh/id_vault -N "" -C "vault@host (rotated YYYY-MM-DD)"`.
   - Upload new pub key, enable write access.
   - Restart daemon.

7. **Override cron:** `python tools/schedule/main.py rm <seed_id>` creates tombstone; then `python tools/schedule/main.py add "0 4 * * *" "vault sync"` creates an orphan row without seed_key (works fine but owner must update manually on any cron change).

8. **Disable auto-commit permanently:** `python tools/schedule/main.py rm <seed_id>` (tombstone is sticky; `revive-seed vault_auto_commit` re-enables on restart).

9. **Encryption warning:** vault markdown is pushed in PLAINTEXT. Private repo + separate vaultbot account + write-only deploy key = adequate for a single-user bot's threat model. For SOC/PII — consider git-crypt in phase 10 (out of scope).

10. **HOME caveat:** if running the daemon via systemd or launchd, ensure `HOME=/Users/<you>` is set in the unit. `gh` reads `$HOME/.config/gh/hosts.yml`.

11. **First-push TOFU** — **v2 SF-F1: use the isolated known_hosts file** so TOFU state matches what the daemon will use at 03:00:

    ```bash
    # Use the SAME UserKnownHostsFile the daemon will use so TOFU "sticks".
    mkdir -p ~/.local/share/0xone-assistant/run
    ssh -i ~/.ssh/id_vault \
        -o UserKnownHostsFile=$HOME/.local/share/0xone-assistant/run/gh-vault-known-hosts \
        -o IdentitiesOnly=yes \
        -T git@github.com
    ```

    (If your `ASSISTANT_DATA_DIR` differs, substitute the correct path.) Accept the prompt from a known-safe network so GitHub's host key is pinned. The daemon will then use `StrictHostKeyChecking=accept-new` against the same file, trusting only the pinned key.

12. **vault_dir as own repo (Q9):** the vault directory `<data_dir>/vault/` becomes a standalone git repo on first `vault-commit-push`. Owner may `git clone git@github.com:vaultbot-owner/vault-backup.git <other-laptop>/vault` to replicate.

13. **Optional future manual smoke (v2 B-D6):** to hand-verify the end-to-end chain (scheduler → handler → CLI → remote), set a short test cron (e.g. `* * * * *`), modify a vault file, wait ~90 s, inspect `git log --oneline` on the remote. Remove the test cron afterwards (`rm <id>` then `revive-seed vault_auto_commit` to restore default).

**README.md / CLAUDE.md:** add a line under "Phases shipped": "Phase 8 — GitHub CLI wrapper + daily vault auto-commit to separate GitHub account via deploy key."

**Dependencies:** C6.

---

## Invariants checklist (I-8.x)

| # | Invariant | Code pointer | Test |
|---|---|---|---|
| I-8.1 | Path-pinned `git add` — vault_dir standalone repo; `git -C <vault_dir> add -A` never touches project_root | `tools/gh/_lib/git_ops.py::stage_all` | `test_gh_vault_commit_path_isolation.py` |
| I-8.2 | Single-flight flock `LOCK_EX \| LOCK_NB`; kernel auto-releases on crash | `tools/gh/_lib/lock.py::flock_exclusive_nb` | `test_gh_flock_concurrency.py` |
| I-8.3 | Fail-fast on divergence: `DIVERGED_RE` → exit 7; no pull/fetch/rebase | `tools/gh/_lib/git_ops.py::DIVERGED_RE` + `push()` classification | `test_gh_vault_commit_push_diverged.py` |
| I-8.4 | SSH key isolation via `GIT_SSH_COMMAND`; inline `-c user.email/name`; no `git config`; isolated `UserKnownHostsFile` | `tools/gh/_lib/git_ops.py::push`, `commit` | `test_gh_vault_commit_push_happy.py` env assertion |
| I-8.5 | Repo allow-list pre-subprocess | `tools/gh/_lib/repo_allowlist.py::is_allowed` called in `_cmd_vault_commit_push` + `_cmd_issue` + `_cmd_pr` + `_cmd_repo` before any `subprocess.run` | `test_gh_repo_whitelist.py` |
| I-8.6 | Scheduler seed idempotency via partial UNIQUE INDEX + pre-INSERT `find_by_seed_key` | `src/assistant/scheduler/seed.py` + `0005_schedule_seed_key.sql` | `test_gh_seed_idempotency.py` |
| I-8.7 | `auth-status` subcommand isolation: no flock, no git, no vault | `tools/gh/main.py::_cmd_auth_status` | `test_gh_auth_status_probe.py` |
| I-8.8 | vault_dir independence from project_root | `tools/gh/main.py::_cmd_vault_commit_push` — uses only `settings.vault_dir`, never `settings.project_root` | `test_gh_vault_commit_path_isolation.py` |
| I-8.9 | Seed tombstone respects owner intent | `src/assistant/scheduler/store.py::delete_schedule` + `tombstone_exists` check in seed helper | `test_gh_seed_tombstone.py` |

Plus phase-7 invariants preserved (I-7.1..I-7.9) — no changes in media pipeline code except new tests that import `ARTEFACT_RE`.

---

## Test matrix

| Test file | Blocker/Q closed | Key assertion |
|---|---|---|
| `test_gh_settings_ssh_url_validation.py` | SF2 | ASCII-only regex; `..` rejection; https rejection |
| `test_gh_settings_tz_validation.py` | R-10/B4 | `Europe/Moscow` ok; `Xyz/Nowhere` raises |
| `test_gh_skill_md_assertion.py` | H-13 | frontmatter + 10 exit-codes + ≥5 dialog examples |
| `test_gh_auth_status_probe.py` | R-1/B7 | rc=0→exit0; rc=1→exit4; gh missing→exit4 |
| `test_gh_issue_create_happy.py` | I-8.5 | JSON pass-through; `number` field present |
| `test_gh_repo_whitelist.py` | I-8.5 | unknown repo → exit 6 WITHOUT subprocess |
| `test_gh_validate_argv.py` | hook | 12 allow + 14 deny cases |
| `test_gh_bash_hook_integration.py` | hook | dispatch routes `python tools/gh/main.py` to `_validate_gh_argv` |
| `test_gh_vault_commit_push_happy.py` | I-8.3/I-8.4 | `GIT_SSH_COMMAND` contains `IdentitiesOnly=yes`; exit 0; JSON has `commit_sha` |
| `test_gh_vault_commit_push_no_changes.py` | B2/R-9 | empty porcelain → exit 5 |
| `test_gh_vault_commit_push_diverged.py` | R-14/I-8.3 | stderr `rejected/non-fast-forward` → exit 7 |
| `test_gh_vault_commit_path_isolation.py` | **I-8.1 CRITICAL** | outbox/db/run files NOT in commit; ONLY vault/*.md |
| `test_gh_vault_git_bootstrap.py` | Q9/R-8 | `git init`+remote+bootstrap commit; real commit is HEAD |
| `test_gh_flock_concurrency.py` | I-8.2/R-4 | 2 procs; 2nd exits 9 in <100ms |
| `test_gh_ssh_key_missing.py` | B6 | nonexistent key → exit 10 |
| `test_gh_dispatch_reply_no_artefact_match.py` | Q11/R-13 | 50-line corpus → 0 regex matches |
| `test_gh_seed_idempotency.py` | I-8.6 | double-call → 1 row |
| `test_gh_seed_disabled.py` | Q8 | `auto_commit_enabled=False` → 0 rows |
| `test_gh_seed_tombstone.py` | I-8.9/Q10 | `rm` → tombstone → seed skip → `revive-seed` → re-create |
| `test_gh_migration_v5_v6.py` | R-6 | v3→v6 sequential; existing rows get `seed_key=NULL`; partial index works |
| `test_gh_vault_commit_push_mkdir_fresh.py` | **v2 B-A3** | fresh ASSISTANT_DATA_DIR → `_cmd_vault_commit_push` creates `<data>/vault/` mode 0o700 before checks |
| `test_gh_vault_commit_push_unpushed_retry.py` | **v2 B-B2** | local commit after push-fail → next run pushes without re-stage; no silent data loss |
| `test_gh_vault_commit_push_diverged_resets.py` | **v2 B-B2** | divergence → `reset --soft HEAD~1` → working tree dirty, next run retries cleanly |
| `test_gh_vault_commit_push_dry_run_no_flock.py` | **v2 SF-B3** | `--dry-run` bypasses flock — rc 0 while external process holds lock |
| `test_gh_pr_view_flattens_author.py` | **v2 SF-A5** | nested `{"author": {"login": X}}` → `"author": X` |
| `test_gh_seed_tombstone_nullable_branch.py` | **v2 SF-D5** | `cmd_rm` on seed_key=NULL row → no tombstone inserted |
| `test_genimage_quota_midnight_rollover.py::test_wrong_shape_list_payload_recovers` | X-1 | xfail removed; passes strictly |
| `test_genimage_quota_midnight_rollover.py::test_best_effort_reader_binary_input_recovers` | X-2 | xfail removed; passes strictly |

**v2 note:** `test_gh_e2e_scheduler_to_commit.py` is removed (B-D6 downgrade). Component-level tests + manual smoke cover the functional path.

**Total new test files:** ~26 (v1 ~21 + 6 v2 additions − 1 E2E removal). **Existing tests modified:** 1 (Wave 0 xfail removal).

---

## Known gotchas (from spikes)

1. **gh 2.89 rejects `--json user`.** Only `--json hosts` supported on `gh auth status`. Plan's probe MUST use rc + stderr text parsing (not `--json`). [spike_gh_auth_shapes]

2. **`git diff --quiet` misses untracked files.** Use `git status --porcelain` for change detection. [spike_git_status_porcelain]

3. **Unquoted `GIT_SSH_COMMAND` is shell-split.** `shlex.quote` on every path is mandatory; defence validated by reproducing `-o ProxyCommand=curl` injection. [spike_git_ssh_command]

4. **macOS Darwin 24 ships tzdata.** No PyPI `tzdata` package needed for `ZoneInfo("Europe/Moscow")`. Document fallback for minimal Linux base images. [spike_zoneinfo_darwin]

5. **Flock auto-release is sub-millisecond.** No need for stale-PID-file cleanup logic. Kernel does the right thing. [spike_flock_oom]

6. **Partial UNIQUE INDEX works on aiosqlite's bundled SQLite.** No version gotcha. `CREATE UNIQUE INDEX ... WHERE col IS NOT NULL` syntax is stable. [spike_sqlite_alter_table]

7. **`git commit --only -- <path>` leaves other staged changes staged.** `--only` is belt-and-suspenders in vault-as-own-repo topology (Q9), but keep it. [spike_git_commit_only]

8. **`file://` URLs bypass ssh entirely.** Test infrastructure: `git init --bare /tmp/bare.git` + `git push file:///tmp/bare.git main` — no `GIT_SSH_COMMAND` parsing, no credentials needed. [spike_vault_bootstrap]

9. **Divergence stderr includes `! [rejected]` AND `non-fast-forward`/`fetch first`/`updates were rejected`.** Use case-insensitive substring match, not rc-only. [spike_vault_bootstrap]

10. **`ARTEFACT_RE v3` has 0 false positives on gh CLI corpus.** Do NOT modify `media/artefacts.py`; just add the regression test. [spike_artefact_re_corpus]

11. **macOS `ZoneInfoNotFoundError` for typos.** Wrap pydantic validator around `ZoneInfo(v)` to surface config mistakes at startup rather than as a runtime crash at 03:00. [spike_zoneinfo_darwin]

12. **`gh` stderr on missing auth is predictable:** `"You are not logged into any GitHub hosts. To log in, run: gh auth login"`. Use exact-substring matching `"not logged into"` for reason classification. [spike_gh_auth_shapes]

---

## Acceptance summary (pre-merge gate)

- [ ] `uv sync` green.
- [ ] `just lint` green (ruff + format + mypy strict on new files).
- [ ] `uv run pytest -q` → 1200+ passed, 3 xfailed, 0 failed (Wave 0 cut 5→3; phase 8 adds ~45 new tests in v2).
- [ ] Live smoke: `python tools/gh/main.py auth-status` on dev machine → rc=0, JSON `{"ok":true}`.
- [ ] Live smoke: `python tools/gh/main.py vault-commit-push --dry-run` on configured vault → rc=0 JSON with `planned_message`, `porcelain`, AND `unpushed_commits: 0` (v2).
- [ ] Live smoke (v2 B-A2): `env -i HOME=$HOME PATH=$PATH python tools/gh/main.py auth-status` — runs without TG tokens in env.
- [ ] Live smoke (v2 B-A3): `rm -rf $ASSISTANT_DATA_DIR/vault && python tools/gh/main.py vault-commit-push --dry-run` — recreates vault dir, does NOT exit 3.
- [ ] Live smoke (v2 B-B2): set `GH_VAULT_REMOTE_URL=git@github.com:invalid/nonexistent.git`, run CLI twice. First run → exit 8 `push_failed`, local HEAD advanced. Second run → attempts push-only retry (payload has `retried_unpushed=true`). Revert URL; next run clears the unpushed commit.
- [ ] `python tools/schedule/main.py list` after `Daemon.start` shows exactly one row with `seed_key=vault_auto_commit`, `cron="0 3 * * *"`, `tz="Europe/Moscow"`.
- [ ] `python tools/schedule/main.py rm <id>` → response JSON includes `tombstoned_seed_key`. Daemon restart → no new seed row.
- [ ] `python tools/schedule/main.py revive-seed vault_auto_commit` → response `{"revived": true}`. Daemon restart → seed row recreated.
- [ ] `python tools/gh/main.py vault-commit-push` on a vault with real changes → `git log` on remote shows exactly the vault files (NO `data/media/`, NO `data/assistant.db`, NO `data/run/`).
- [ ] `git push --force`, `gh pr create`, `gh issue close`, `--no-verify`, `--amend` — all denied by `_validate_gh_argv`.
- [ ] `auto_commit_enabled=False` → no seed row created.
- [ ] Missing `GH_VAULT_SSH_KEY_PATH` file → exit 10 with actionable message.
- [ ] Unauthed `gh` → exit 4.
- [ ] `_ARTEFACT_RE v3` false-positive rate = 0 on `tests/fixtures/gh_responses.txt`.
- [ ] Phase-7 invariants preserved: `make_subagent_hooks` signature unchanged (I-7.4), `_DedupLedger` TTL=300/cap=256 (I-7.1), `dispatch_reply` unchanged, `make_pretool_hooks(project_root, data_dir=None)` backward-compat (I-7.5).
- [ ] `tools/gh/` mypy clean.
- [ ] `docs/ops/github-setup.md` covers all 12 sections.
- [ ] `README.md` / `CLAUDE.md` mention phase-8 shipped + `gh` skill listed.

---

## Commit order summary

- **C0** (Wave 0) — genimage X-1/X-2 hotfix. 1 commit.
- **C1** — GitHubSettings + validators. 1 commit.
- **C2** — tools/gh/ scaffolding + auth-status + SKILL.md. 1 commit.
- **C3** — issue/pr/repo read-only + allow-list. 1 commit.
- **C4** — vault-commit-push + git_ops + flock + bootstrap. 1 commit (largest, ~500 LOC src + 400 LOC tests — may split if over budget).
- **C5** — migration 0005 + 0006 + seed helper + schedule CLI `rm` + `revive-seed`. 1 commit.
- **C6** — Daemon.start integration + `_validate_gh_argv`. 1 commit.
- **C7** — docs + E2E + README polish. 1 commit.

Total: **8 commits**. Expected diff ~1800 LOC src + ~1600 LOC tests.

Parallelisable (Wave A / Wave B later via parallel-split agent):

- C2 and C5 are DISJOINT file sets — could run in parallel.
- C3 depends on C2.
- C4 depends on C1 + C3.
- C6 depends on C4 + C5.

Exact parallel-split packaging is out-of-scope for this document; see the parallel-split agent's output in `wave-plan.md` (next pipeline step).

---

## File references

- Spike probes: `/Users/agent2/Documents/0xone-assistant/spikes/phase8/spike_*.py`
- Spike reports: `/Users/agent2/Documents/0xone-assistant/spikes/phase8/spike_*_report.json`
- Spike findings: `/Users/agent2/Documents/0xone-assistant/plan/phase8/spike-findings.md`
- Detailed plan r2: `/Users/agent2/Documents/0xone-assistant/plan/phase8/detailed-plan.md`
- Phase-7 style reference: `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md`
- Phase-7 invariants: `/Users/agent2/Documents/0xone-assistant/plan/phase7/summary.md`

This is implementation v2 — ready for parallel-split agent (wave-plan.md) and multi-wave coder execution.

---

## §Appendix — Fix-pack v2 changelog (from devil wave 2)

Closed **6 blockers + 20 should-fix items**. Each entry links the item ID to its prescription location. All changes are in-place edits to v1 sections (no wholesale rewrites); line-count went from 2013 → ~2200.

### Blockers (6)

- **B-A1 pydantic-settings 2.13 tuple parsing** — `allowed_repos` annotation is now `Annotated[tuple[str, ...], NoDecode]`. Without `NoDecode`, the settings framework JSON-decodes typed tuple env values BEFORE the field validator runs and raises `SettingsError` on `"a/b,c/d"`. Verified against pydantic-settings 2.13.1 in repo (`uv run python -c "from pydantic_settings import NoDecode"` → OK). §C1 import + annotation updated; test case 14 added.
- **B-A2 tools/gh/main.py can't use `get_settings()` standalone** — all CLI entry points now instantiate `GitHubSettings()` directly (sub-model has no required fields) plus a local `_data_dir()` / `_vault_dir()` pair mirroring `tools/schedule/main.py` + `tools/memory/main.py`. CLI works without `TELEGRAM_BOT_TOKEN` + `OWNER_CHAT_ID` in env. §C2 skeleton + §C3 handlers + §C4 `_cmd_vault_commit_push` rewritten; tests drop the `TELEGRAM_BOT_TOKEN` monkeypatch. Pitfall 17 added.
- **B-A3 missing vault_dir causes bootstrap fail** — `_cmd_vault_commit_push` step 2 now does `vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)` BEFORE any `is_dir()` check. Handles fresh install + user-deletes-vault race. §C4 execution flow updated; `test_gh_vault_commit_push_mkdir_fresh.py` added.
- **B-B2 data loss on push divergence** — new `unpushed_commit_count(vault_dir)` helper + `reset_soft_head_one(vault_dir)` helper in `git_ops.py`. `_cmd_vault_commit_push` detects unpushed commits BEFORE porcelain check and does a push-only retry; divergence response path calls `reset --soft HEAD~1` so working-tree changes are preserved for the next run. §C4 helpers + execution flow; `test_gh_vault_commit_push_unpushed_retry.py` + `test_gh_vault_commit_push_diverged_resets.py` added. Pitfall 20 added.
- **B-D1 plan contradicts tools/schedule/main.py pattern** — `tools/schedule/main.py::cmd_rm` stays **sync sqlite3 + soft-delete** (`UPDATE enabled=0`). Tombstone insert is co-located in the same `BEGIN IMMEDIATE` transaction iff row has `seed_key IS NOT NULL`. `cmd_revive_seed` follows the same sync pattern. `SchedulerStore.delete_schedule` is LEFT UNCHANGED (callers are tests only; signature stable). §C5 rewritten; `test_gh_seed_tombstone.py` rewritten to exercise the subprocess path; `test_gh_seed_tombstone_nullable_branch.py` added. Pitfall 21 added.
- **B-D6 E2E test missing concrete fixture** — `test_gh_e2e_scheduler_to_commit.py` removed from acceptance. Component tests (happy / no-changes / diverged / unpushed / path-isolation / flock / ssh-key / bootstrap / seed / tombstone / migration / validate-argv / hook-integration + manual smoke) cover the functional path. §C7 converted to docs-only commit + future-work note.

### Should-fix items (20)

- **A4** §C4 `commit()` docstring explains `-c KEY=VALUE` must precede the git subcommand. Pitfall 16 added.
- **A5** §C3 `_cmd_pr` flattens nested `{"author": {"login": X}}` → `"author": X`; `test_gh_pr_view_flattens_author.py` added.
- **A6** §C3 `_is_unauth_stderr` matches both `"authenticated"` AND `"not logged into"` substrings.
- **A7** §C2 `gh_auth_status` + §C3 `run_gh_json` catch `subprocess.TimeoutExpired`; CLI maps to exit 1 `gh_timeout` JSON.
- **B1** §C5 new `store.ensure_seed_row(...)` helper wraps tombstone-check + find + insert in `BEGIN IMMEDIATE`; `scheduler/seed.py` simplified to call it.
- **B3** §C4 `_cmd_vault_commit_push` dry-run path bypasses flock entirely (read-only). `test_gh_vault_commit_push_dry_run_no_flock.py` added.
- **C1** §C1 ssh URL regex tightened: owner = `[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?` (GitHub-accurate). Tests 10-12 added.
- **C2** §C2 `build_gh_env` + §C4 `_base_env` scrub `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GH_HOST`, `GH_CONFIG_DIR`.
- **C3** §C4 `_base_env` additionally pops `GIT_SSH_COMMAND` (reset by `push()`) and `SSH_ASKPASS`; keeps `SSH_AUTH_SOCK` (neutralized by `IdentitiesOnly=yes`).
- **C6** §C6 `_validate_gh_argv` adds `_GH_REPO_ALLOWED_SUBSUB = {"view"}` check; `_GH_ISSUE_FORBIDDEN` extended with `develop`, `pin`, `unpin`, `status`; `_GH_FORBIDDEN_FLAGS` adds `--body-file`. 8 new DENY test rows.
- **D2** §C4 test 8 corrects `ARTEFACT_RE` import: `from assistant.media.artefacts import ARTEFACT_RE` (was incorrectly written as `adapters/` in v1; verified path in repo).
- **D3** §C5 migration test now asserts `WHERE seed_key IS NOT NULL` appears in the index DDL by querying `sqlite_master`.
- **D5** §C5 `test_gh_seed_tombstone_nullable_branch.py` added to cover the `seed_key IS NULL` branch (user-created rows never get a tombstone).
- **D7** §C4 bootstrap writes a `.gitignore` containing `.tmp/` + `*.tmp`; path-isolation test seeds `<vault>/.tmp/junk` and verifies it's NOT in the commit.
- **E1** §C6 `_verify_gh_config_accessible_for_daemon` is now `async` — uses asyncio subprocess API + `asyncio.wait_for`; callable from `Daemon.start()` with `await`, cancellable on SIGTERM.
- **F1** §C7 docs step 11 (TOFU): uses isolated `UserKnownHostsFile=$DATA_DIR/run/gh-vault-known-hosts` so manual pinning matches what the daemon will trust.
- **F2** §C5 `_apply_v5` / `_apply_v6` execute individual statements (`await conn.execute(...)`) inside a `BEGIN IMMEDIATE`; no `executescript` which would bypass the transaction on aiosqlite. Pitfall 19 added.
- **F3** §C1 adds `mode="before"` validator `_expand_ssh_key_path` that calls `Path(v).expanduser()`; §C7 `.env` template renamed `GH_VAULT_SSH_KEY` → `GH_VAULT_SSH_KEY_PATH` (matches field name so pydantic-settings picks it up via `env_prefix="GH_"`).
- **S1** §C4 test suite adds `test_gh_flock_released_on_parent_sigkill.py` — real `Popen` + `SIGKILL`, verify lock released in <10 ms (`close_fds=True` default).
- **S2** §C4 test 8 (`test_gh_dispatch_reply_no_artefact_match.py`) extended corpus: Russian/emoji text, markdown code-block bodies, `.png` substrings embedded inside JSON payloads from `gh issue view`.

### Verification checklist (v2 self-check)

1. No `from assistant.config import get_settings` in any prescription for `tools/gh/main.py` — replaced with `from assistant.config import GitHubSettings`.
2. `allowed_repos` annotation = `Annotated[tuple[str, ...], NoDecode]` in §C1.
3. `_cmd_vault_commit_push` step 2 = `vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)`.
4. §C4 has `unpushed_commit_count` + `reset_soft_head_one` helpers, and the execution flow has the unpushed-detection step BEFORE porcelain check.
5. §C5 `cmd_rm` pseudocode uses **sync sqlite3** + `UPDATE enabled=0` + conditional tombstone insert.
6. §C5 adds `cmd_revive_seed` (sync sqlite3).
7. §C6 `_validate_gh_argv` has `_GH_REPO_ALLOWED_SUBSUB` and blocks `--body-file`, `develop`, `pin`, `unpin`, `status` on issues.
8. §C4 regression test imports `from assistant.media.artefacts import ARTEFACT_RE`.
9. §C7 `.env` template uses `GH_VAULT_SSH_KEY_PATH` (not `GH_VAULT_SSH_KEY`).

v2 is ready for the parallel-split agent (to produce `wave-plan.md`) and multi-wave coder execution.
