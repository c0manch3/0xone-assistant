#!/usr/bin/env python3
"""Phase-8 thin CLI wrapper for `gh` + vault auto-commit.

Stdlib-first + direct sub-model instantiation so the CLI runs standalone
(fresh install, manual smoke, cron) WITHOUT ``TELEGRAM_BOT_TOKEN`` /
``OWNER_CHAT_ID`` present in the environment. Mirrors
``tools/schedule/main.py`` + ``tools/genimage/main.py`` (see §0
blocker B-A2).

Wired subcommands:

- ``auth-status`` (C2) — probe gh OAuth session.
- ``issue create|list|view`` (C3) — read-only GH issues, allow-list gated.
- ``pr list|view`` (C3) — read-only GH pull requests, allow-list gated.
- ``repo view`` (C3) — read-only GH repo metadata, allow-list gated.
- ``vault-commit-push`` (stub until C4) — returns ``not_implemented``.

Every subcommand that touches the network (``issue*`` / ``pr*`` /
``repo view``) runs the repo allow-list check BEFORE any subprocess
call so a misconfigured invocation never reaches GitHub (I-8.5 defence
in depth). Exit code 6 ``repo_not_allowed`` is emitted locally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# sys.path pragma for cwd + module invocation parity (phase-7 Q9a pattern —
# see `tools/__init__.py`). The project root hosts both `src/` and `tools/`;
# adding it lets us do `from tools.gh._lib...` regardless of how the CLI
# was launched.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.gh._lib import exit_codes as ec  # noqa: E402 — sys.path pragma above
from tools.gh._lib import repo_allowlist  # noqa: E402
from tools.gh._lib.gh_ops import gh_auth_status, run_gh_json  # noqa: E402
from tools.gh._lib.git_ops import (  # noqa: E402
    commit as _git_commit,
    diff_cached_empty,
    files_changed_count,
    is_inside_work_tree,
    porcelain_status,
    push as _git_push,
    reset_soft_head_one,
    stage_all,
    unpushed_commit_count,
)
from tools.gh._lib.lock import LockBusyError, flock_exclusive_nb  # noqa: E402
from tools.gh._lib.vault_git_init import bootstrap as _vault_bootstrap  # noqa: E402

# B-A2: instantiate ``GitHubSettings`` directly (the sub-model has no
# required fields), so the CLI works without ``TELEGRAM_BOT_TOKEN`` /
# ``OWNER_CHAT_ID`` in the environment — required for fresh-install and
# cron contexts. We import lazily inside handlers to keep module import
# cost low (pydantic-settings + env_file parsing happens only when a
# subcommand is actually invoked).

# ---------------------------------------------------------------------------
# Module-local path helpers (B-A2). Identical semantics to
# ``tools/schedule/main.py::_data_dir`` + ``tools/memory/main.py::_resolve_vault_dir``
# so CLI invocations stay consistent with daemon behaviour WITHOUT importing
# ``assistant.config.get_settings`` (which would require TELEGRAM_BOT_TOKEN).


def _data_dir() -> Path:
    """Resolve ``<data_dir>`` without instantiating ``Settings``.

    Precedence (matches ``assistant.config._default_data_dir``):
      1. ``ASSISTANT_DATA_DIR`` (explicit override)
      2. ``$XDG_DATA_HOME/0xone-assistant``
      3. ``~/.local/share/0xone-assistant``
    """
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "0xone-assistant"
    return Path.home() / ".local" / "share" / "0xone-assistant"


def _vault_dir() -> Path:
    """Resolve vault dir (mirrors ``MemorySettings.vault_dir`` default)."""
    override = os.environ.get("MEMORY_VAULT_DIR")
    if override:
        return Path(override).expanduser()
    return _data_dir() / "vault"


# ---------------------------------------------------------------------------
# Subcommand handlers


def _cmd_auth_status(_args: argparse.Namespace) -> int:
    """Probe the `gh` OAuth session. One-line JSON on stdout.

    Maps the tuple returned by ``gh_auth_status`` to exit codes:

    - rc ==  0  → OK, ``{"ok": true}``
    - rc == -1 + stderr=="timeout" → exit 1, ``{"ok": false, "error": "gh_timeout"}``
    - rc == 127                    → exit 4, ``{"ok": false, "error": "gh_not_found"}``
    - any other rc                 → exit 4, ``{"ok": false, "error": "not_authenticated"}``

    The ``"not logged into"`` substring in stderr is the canonical
    unauthenticated marker emitted by `gh auth status` on a box without
    an active hosts.yml entry (SF-A6). We don't require the substring
    here — any non-zero rc from a working `gh` binary is treated as
    "not authenticated" because the subcommand's only purpose is a
    binary ok/not-ok probe; callers can re-read stderr if they want
    the exact reason.
    """
    rc, _out, err = gh_auth_status()
    if rc == 0:
        print(json.dumps({"ok": True}))
        return ec.OK
    if rc == -1 and err == "timeout":
        print(json.dumps({"ok": False, "error": "gh_timeout"}))
        return 1
    if rc == 127:
        print(json.dumps({"ok": False, "error": "gh_not_found"}))
        return ec.GH_NOT_AUTHED
    print(json.dumps({"ok": False, "error": "not_authenticated"}))
    return ec.GH_NOT_AUTHED


def _cmd_stub(name: str) -> int:
    """Shared not-implemented response for C4 stubs.

    Emits a structured error on stderr so the model + tests can detect
    "subcommand registered but not wired yet" deterministically. Exit
    code is ``ARGV`` (2) because, from the CLI contract's point of view,
    calling a not-yet-implemented subcommand is a usage error at this
    phase — the command isn't part of the supported surface yet.
    """
    sys.stderr.write(
        json.dumps({"ok": False, "error": "not_implemented", "cmd": name}) + "\n"
    )
    return ec.ARGV


# SF-A6: substring hit-list for the "unauthenticated" exit-4 branch.
# We match both GH's "not logged into" wording (``gh auth status`` with
# no session) and the more generic "not authenticated" / "authentication
# required" phrasings that gh emits for API-level 401s. All lowercased
# before comparison so stderr casing doesn't matter.
_GH_UNAUTH_SUBSTRINGS: tuple[str, ...] = (
    "not logged into",
    "not authenticated",
    "authentication required",
)


def _is_unauth_stderr(stderr: str) -> bool:
    """Classify ``gh`` stderr as an auth failure vs generic error.

    Used by every C3 handler when ``run_gh_json`` returns non-zero rc:
    auth failures map to exit 4 (``GH_NOT_AUTHED``) so downstream
    tooling can surface "run ``gh auth login``" to the operator,
    whereas generic errors map to exit 1.
    """
    lo = stderr.lower()
    return any(sub in lo for sub in _GH_UNAUTH_SUBSTRINGS)


def _fail_repo_not_allowed(repo: str, allowed: tuple[str, ...]) -> int:
    """Emit a structured ``repo_not_allowed`` JSON and return exit code 6.

    The allow-list is echoed back so the caller can immediately see
    which slugs WOULD be accepted — easier than grepping config files.
    This is security-safe because the allow-list isn't a secret; it's
    already pinned in ``.env`` or in the operator's shell profile.
    """
    print(
        json.dumps(
            {
                "ok": False,
                "error": "repo_not_allowed",
                "repo": repo,
                "allowed": list(allowed),
            }
        )
    )
    return ec.REPO_NOT_ALLOWED


def _handle_gh_error(rc: int, stderr: str) -> int:
    """Map ``run_gh_json`` non-zero rc to a CLI exit code + emit JSON.

    Truncates stderr to 200 chars so we don't dump unbounded `gh`
    output to the caller's stdout. The truncation is safe — the full
    stderr is still visible in any wrapping log files; the JSON here
    is meant to be a one-liner machine-parseable summary.

    Special-cases:
    - ``rc == -1 and stderr == "timeout"`` → exit 1, ``gh_timeout``.
    - ``rc == -1 and stderr == "gh_not_found"`` → exit 4.
    - ``_is_unauth_stderr`` → exit 4.
    - otherwise → exit 1 (generic).
    """
    if rc == -1 and stderr == "timeout":
        print(json.dumps({"ok": False, "error": "gh_timeout"}))
        return 1
    if rc == -1 and stderr == "gh_not_found":
        print(json.dumps({"ok": False, "error": "gh_not_found"}))
        return ec.GH_NOT_AUTHED
    if _is_unauth_stderr(stderr):
        print(json.dumps({"ok": False, "error": "not_authenticated"}))
        return ec.GH_NOT_AUTHED
    print(json.dumps({"ok": False, "error": stderr[:200]}))
    return 1


def _github_settings() -> tuple[str, ...]:
    """Return ``GitHubSettings.allowed_repos`` (direct instantiation).

    B-A2: we instantiate the sub-model directly rather than going
    through ``get_settings()`` so the CLI stays usable on a fresh
    install without ``TELEGRAM_BOT_TOKEN`` / ``OWNER_CHAT_ID`` in env.
    Any import / validation error is surfaced as a raw exception —
    the caller's argparse layer has already validated CLI args, so
    a config error at this point is genuinely a user-environment
    problem that deserves a stack trace.

    The ``type: ignore[import-untyped]`` is required because
    ``assistant.config`` lives under ``src/`` and the repo doesn't
    ship a ``py.typed`` marker for the ``assistant`` package (it is
    a first-party module registered via ``pyproject.toml`` but
    without the marker mypy treats it as untyped from outside its
    own src tree). Running ``uv run mypy tools/gh --strict`` alone
    — per the C3 acceptance contract — doesn't include ``src/`` in
    its source list, so the import fails the `import-untyped` check.
    The return is explicitly cast to ``tuple[str, ...]`` via a local
    binding rather than ``typing.cast`` to keep the dependency surface
    stdlib-only.
    """
    from assistant.config import GitHubSettings  # type: ignore[import-untyped]

    allowed: tuple[str, ...] = GitHubSettings().allowed_repos
    return allowed


def _cmd_issue(args: argparse.Namespace) -> int:
    """Handle ``issue create|list|view`` with allow-list gate.

    All three subcommands share the same pre-subprocess check:
    ``repo`` (the ``--repo OWNER/REPO`` arg) must be in
    ``GitHubSettings.allowed_repos``, otherwise exit 6 with no gh
    invocation (I-8.5). This keeps the security boundary at the
    argparse layer — only vetted repos ever see a subprocess call.
    """
    sub = args.issue_cmd
    if sub is None:
        return _cmd_stub("issue")

    allowed = _github_settings()
    repo: str = args.repo
    if not repo_allowlist.is_repo_allowed(repo, allowed):
        return _fail_repo_not_allowed(repo, allowed)

    if sub == "create":
        gh_args: list[str] = [
            "issue", "create",
            "--repo", repo,
            "--title", args.title,
            "--body", args.body,
        ]
        for label in args.label or []:
            gh_args.extend(["--label", label])
        # ``gh issue create`` needs ``--json`` to produce machine-readable
        # output; the supported fields for `issue create` are
        # ``url,number``. See gh v2.x docs.
        gh_args.extend(["--json", "url,number"])
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            return _handle_gh_error(rc, stderr)
        payload: dict[str, object] = {"ok": True}
        if isinstance(data, dict):
            payload.update(data)
        print(json.dumps(payload))
        return ec.OK

    if sub == "list":
        gh_args = [
            "issue", "list",
            "--repo", repo,
            "--json", "number,title,state,labels",
        ]
        if args.state:
            gh_args.extend(["--state", args.state])
        if args.limit:
            # SF-5 hard-cap: never request more than 100 items per call.
            # Above that gh paginates anyway, and we want bounded
            # stdout size for the assistant's context window.
            gh_args.extend(["--limit", str(min(args.limit, 100))])
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            return _handle_gh_error(rc, stderr)
        # ``gh issue list --json`` returns a JSON array (possibly empty).
        items = data if isinstance(data, list) else []
        print(json.dumps({"ok": True, "issues": items}))
        return ec.OK

    if sub == "view":
        gh_args = [
            "issue", "view", str(args.number),
            "--repo", repo,
            "--json", "number,title,body,state,labels,author",
        ]
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            return _handle_gh_error(rc, stderr)
        payload = {"ok": True}
        if isinstance(data, dict):
            payload.update(data)
        print(json.dumps(payload))
        return ec.OK

    # Unknown subsub — argparse's choices should prevent this, but be
    # defensive (matches ``_dispatch`` fallback style).
    return _cmd_stub(f"issue {sub}")


def _cmd_pr(args: argparse.Namespace) -> int:
    """Handle ``pr list|view`` with allow-list gate.

    ``pr view`` flattens the nested ``{"author": {"login": X}}`` shape
    via ``run_gh_json`` → ``_flatten_gh_json`` (SF-A5) so downstream
    consumers see ``"author": "octocat"`` consistently.
    """
    sub = args.pr_cmd
    if sub is None:
        return _cmd_stub("pr")

    allowed = _github_settings()
    repo: str = args.repo
    if not repo_allowlist.is_repo_allowed(repo, allowed):
        return _fail_repo_not_allowed(repo, allowed)

    if sub == "list":
        gh_args: list[str] = [
            "pr", "list",
            "--repo", repo,
            "--json", "number,title,state,author",
        ]
        if args.limit:
            gh_args.extend(["--limit", str(min(args.limit, 100))])
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            return _handle_gh_error(rc, stderr)
        items = data if isinstance(data, list) else []
        print(json.dumps({"ok": True, "prs": items}))
        return ec.OK

    if sub == "view":
        gh_args = [
            "pr", "view", str(args.number),
            "--repo", repo,
            "--json", "number,title,body,state,mergeable,author",
        ]
        rc, data, stderr = run_gh_json(gh_args)
        if rc != 0:
            return _handle_gh_error(rc, stderr)
        payload: dict[str, object] = {"ok": True}
        if isinstance(data, dict):
            payload.update(data)
        print(json.dumps(payload))
        return ec.OK

    return _cmd_stub(f"pr {sub}")


def _cmd_repo(args: argparse.Namespace) -> int:
    """Handle ``repo view`` with allow-list gate.

    Only ``view`` is supported; ``create``/``clone``/``delete`` are
    explicitly out of scope for phase 8 (SF-C6). ``defaultBranchRef``
    flattens to ``default_branch`` via ``_flatten_gh_json``.
    """
    sub = args.repo_cmd
    if sub is None:
        return _cmd_stub("repo")

    if sub != "view":
        return _cmd_stub(f"repo {sub}")

    allowed = _github_settings()
    repo: str = args.repo
    if not repo_allowlist.is_repo_allowed(repo, allowed):
        return _fail_repo_not_allowed(repo, allowed)

    gh_args: list[str] = [
        "repo", "view", repo,
        "--json", "name,description,defaultBranchRef,visibility",
    ]
    rc, data, stderr = run_gh_json(gh_args)
    if rc != 0:
        return _handle_gh_error(rc, stderr)
    payload: dict[str, object] = {"ok": True}
    if isinstance(data, dict):
        payload.update(data)
    print(json.dumps(payload))
    return ec.OK


# ---------------------------------------------------------------------------
# C4: vault-commit-push handler + helpers.
#
# Execution flow per implementation.md §C4 (v2 with B-A2 / B-A3 / B-B2 /
# SF-B3 / SF-D7 fixes):
#
#   1.  Instantiate ``GitHubSettings()`` directly (B-A2).
#   2.  ``vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)`` (B-A3).
#   3.  allow-list check (I-8.5).
#   4.  ssh key readability + permission probe (exit 10).
#   5.  prepare lock path + known-hosts path; ``--dry-run`` bypasses flock (SF-B3).
#   6.  acquire flock exclusively (I-8.2).
#   7.  bootstrap the vault if not yet a git repo (Q9 / R-8).
#   8.  B-B2 unpushed-commit detection → push-only retry path if > 0.
#   9.  ``git status --porcelain`` change detection (B2 / R-9).
#   10. render commit message in ``auto_commit_tz`` (B4 / R-10).
#   11. ``_do_push_cycle(stage=True)`` — stage, commit, push, classify outcome.
#
# Divergence in step 11 triggers ``reset --soft HEAD~1`` inside
# ``_do_push_cycle`` (B-B2) so the working tree stays dirty for the next
# run to retry cleanly.


def _render_message(template: str, tz_name: str) -> str:
    """Render ``{date}`` placeholder in the commit-message template.

    Uses ``ZoneInfo(tz_name)`` so the date reflects the owner's timezone
    (B4). ``strftime("%Y-%m-%d")`` is ASCII-safe and matches the default
    template ``"vault sync {date}"`` → ``"vault sync 2026-04-19"``.
    """
    date_str = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    return template.format(date=date_str)


def _ssh_key_readable(path: Path) -> tuple[bool, str]:
    """Probe the ssh key file.

    Returns ``(ok, note)``:

    - ``(False, "not a file: <path>")`` — doesn't exist or is a dir.
    - ``(False, "not readable: <path>")`` — exists but not r-- for our uid.
    - ``(False, "stat failed: ...")`` — syscall error (rare).
    - ``(True,  "")`` — fine, proceed.
    - ``(True,  "permissive_mode:0oNNN")`` — usable but group/other bits
      are set; caller logs a warning but proceeds. This matches the
      openssh client behaviour (accepts the key, just complains on
      stderr for non-0o600 files on operating systems that care).

    Only fatal (ok=False) cases return exit 10. The "permissive_mode"
    warning path returns ok=True so an owner who intentionally uses a
    group-readable key on a trusted host isn't blocked from backups.
    """
    if not path.is_file():
        return False, f"not a file: {path}"
    if not os.access(path, os.R_OK):
        return False, f"not readable: {path}"
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return True, f"permissive_mode:{oct(mode)}"
    except OSError as exc:
        return False, f"stat failed: {exc}"
    return True, ""


def _do_push_cycle(
    *,
    vault_dir: Path,
    gh: object,  # GitHubSettings, but pydantic isn't on this module's type-sight
    known_hosts: Path,
    stage: bool,
    message: str | None,
) -> tuple[int, dict[str, object]]:
    """Execute a single commit-then-push cycle. Returns ``(exit_code, payload)``.

    Two entry modes controlled by ``stage``:

    - ``stage=True`` (normal): runs :func:`stage_all`, a race-window
      recheck via :func:`diff_cached_empty`, :func:`_git_commit`, then
      :func:`_git_push`.
    - ``stage=False`` (B-B2 retry): HEAD already points at the commit we
      want to ship (a previous run made it). We skip staging+commit and
      push directly. If push fails with "diverged" we DO NOT
      ``reset --soft`` — the commit may be salvageable manually; the
      owner inspects and decides.

    Payload shape on success: ``{"ok": true, "commit_sha": ..., "files_changed":
    N, "retried_unpushed": <bool>}``. On failure, ``ok=False`` with an
    ``error`` string. ``stderr`` is truncated to 300 chars to keep the JSON
    a reasonable size for logging.
    """
    # Type shim: we accept ``gh`` as ``object`` so this module doesn't
    # force every mypy run to import pydantic. The attribute access below
    # relies on the instantiation contract in :func:`_cmd_vault_commit_push`.
    vault_remote_name: str = gh.vault_remote_name  # type: ignore[attr-defined]
    vault_branch: str = gh.vault_branch  # type: ignore[attr-defined]
    vault_ssh_key_path: Path = gh.vault_ssh_key_path  # type: ignore[attr-defined]
    commit_author_email: str = gh.commit_author_email  # type: ignore[attr-defined]

    if stage:
        stage_all(vault_dir)
        # Race-window recheck: porcelain said "dirty" but between then and
        # `git add` something may have reverted the tree. If `git diff
        # --cached --quiet` returns 0 now, there's nothing to commit.
        if diff_cached_empty(vault_dir):
            return ec.NO_CHANGES, {
                "ok": True,
                "no_changes": True,
                "race": True,
            }
        sha = _git_commit(
            vault_dir,
            message=message or "",
            author_email=commit_author_email,
        )
    else:
        # Push-only retry path: HEAD is already the commit we want to
        # ship. Import `_run_git` here (not at module top) to keep the
        # hot-import surface of `main.py` minimal for `auth-status` /
        # `issue` / `pr` callers that don't touch git at all.
        from tools.gh._lib.git_ops import _run_git

        head = _run_git(["rev-parse", "HEAD"], vault_dir, check=True)
        sha = head.stdout.strip()

    files_n = files_changed_count(vault_dir)

    push_result, verdict = _git_push(
        vault_dir,
        remote=vault_remote_name,
        branch=vault_branch,
        ssh_key_path=vault_ssh_key_path,
        known_hosts_path=known_hosts,
    )
    if verdict == "ok":
        return ec.OK, {
            "ok": True,
            "commit_sha": sha,
            "files_changed": files_n,
            "retried_unpushed": not stage,
        }
    if verdict == "diverged":
        # B-B2: protect next run from silent data loss. If we staged a
        # new commit in THIS cycle, roll it back via `reset --soft
        # HEAD~1` so the working tree stays dirty — the next invocation
        # will re-stage + re-commit (presumably against an up-to-date
        # remote by then). Push-only retries DO NOT reset; the commit
        # pre-exists and may deserve manual inspection.
        reset_ok = True
        reset_error: str | None = None
        if stage:
            try:
                reset_soft_head_one(vault_dir)
            except Exception as exc:  # pragma: no cover — last-resort
                reset_ok = False
                reset_error = repr(exc)
        payload: dict[str, object] = {
            "ok": False,
            "error": "remote_has_diverged",
            "commit_sha": sha,
            "reset": stage and reset_ok,
            "stderr": push_result.stderr[:300],
        }
        if reset_error is not None:
            payload["reset_error"] = reset_error
        return ec.DIVERGED, payload
    return ec.PUSH_FAILED, {
        "ok": False,
        "error": "push_failed",
        "commit_sha": sha,
        "stderr": push_result.stderr[:300],
    }


def _cmd_vault_commit_push(args: argparse.Namespace) -> int:
    """``vault-commit-push`` — commit and push the vault to the backup remote.

    See the module-top comment for the numbered execution flow. Exit
    codes (matching :mod:`tools.gh._lib.exit_codes`):

    - 0  ``OK`` (commit+push succeeded, or `--dry-run` noop).
    - 3  ``VALIDATION`` (unset URL, bad URL, mkdir failed).
    - 5  ``NO_CHANGES`` (porcelain empty, or race-window recheck empty).
    - 6  ``REPO_NOT_ALLOWED`` (url parses but slug is not in allow-list).
    - 7  ``DIVERGED`` (remote has diverged; local commit reset).
    - 8  ``PUSH_FAILED`` (other push failure).
    - 9  ``LOCK_BUSY`` (another vault-commit-push is active).
    - 10 ``SSH_KEY_ERROR`` (key file missing / unreadable).
    """
    # Step 1: direct sub-model instantiation (B-A2). The import is local
    # so `auth-status` / `issue` / `pr` callers don't pay the
    # pydantic-settings load cost.
    from assistant.config import GitHubSettings

    gh = GitHubSettings()
    data_dir = _data_dir()
    vault_dir = _vault_dir()

    # Step 2 (B-A3): create vault_dir unconditionally. `mode=0o700` mirrors
    # the memory tool's assumptions about vault privacy. This survives
    # both fresh installs (nothing created yet) and the
    # user-deletes-vault-then-runs-CLI race.
    try:
        vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "vault_mkdir_failed",
                    "path": str(vault_dir),
                    "detail": repr(exc),
                }
            )
        )
        return ec.VALIDATION

    # Step 3 (I-8.5): allow-list check BEFORE any subprocess / ssh.
    if not gh.vault_remote_url:
        print(json.dumps({"ok": False, "error": "vault_remote_url_unset"}))
        return ec.VALIDATION
    try:
        slug = repo_allowlist.extract_owner_repo_from_ssh_url(
            gh.vault_remote_url
        )
    except ValueError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "bad_remote_url",
                    "url": gh.vault_remote_url,
                }
            )
        )
        return ec.VALIDATION
    if not repo_allowlist.is_repo_allowed(slug, gh.allowed_repos):
        return _fail_repo_not_allowed(slug, gh.allowed_repos)

    # Step 4: ssh key sanity. We only check when the remote is an
    # ssh-style URL — a `file://` URL (used in tests) bypasses ssh
    # entirely so the key file is irrelevant. In production,
    # `GitHubSettings._validate_remote_url` ensures the URL starts with
    # `git@github.com:`, so this branch is always taken.
    if gh.vault_remote_url.startswith("git@"):
        key_ok, key_note = _ssh_key_readable(gh.vault_ssh_key_path)
        if not key_ok:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "ssh_key_error",
                        "path": str(gh.vault_ssh_key_path),
                        "detail": key_note,
                    }
                )
            )
            return ec.SSH_KEY_ERROR
        if key_note.startswith("permissive_mode:"):
            # Not fatal — see `_ssh_key_readable` docstring. Emit the
            # warning on stderr so log aggregators pick it up; the
            # success JSON on stdout stays clean.
            sys.stderr.write(f"warning: ssh key {key_note}\n")

    # Step 5: path plumbing for the lock + the isolated known-hosts file.
    # SF-B3: `--dry-run` is a read-only operation — it does NOT touch the
    # flock, so a stuck lock-holder can't make `vault-commit-push
    # --dry-run` fail for the owner who's just trying to inspect state.
    data_dir.mkdir(parents=True, exist_ok=True)
    run_dir = data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / "gh-vault-commit.lock"
    known_hosts = run_dir / "gh-vault-known-hosts"

    if args.dry_run:
        if not is_inside_work_tree(vault_dir):
            # Fresh vault: nothing to diff; next non-dry-run will
            # bootstrap. Report that intent so the owner knows.
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "would_bootstrap": True,
                        "vault_dir": str(vault_dir),
                    }
                )
            )
            return ec.OK
        porcelain = porcelain_status(vault_dir)
        print(
            json.dumps(
                {
                    "ok": True,
                    "dry_run": True,
                    "porcelain": porcelain,
                    "unpushed_commits": unpushed_commit_count(
                        vault_dir,
                        remote=gh.vault_remote_name,
                        branch=gh.vault_branch,
                    ),
                    "planned_message": _render_message(
                        gh.commit_message_template, gh.auto_commit_tz
                    ),
                }
            )
        )
        return ec.OK

    # Step 6: acquire the cross-process lock (write path only).
    try:
        with flock_exclusive_nb(lock_path):
            # Step 7: bootstrap if needed (Q9 / R-8). A fresh vault_dir
            # (just mkdir'd in step 2, or a pre-existing empty dir) has
            # no `.git/` — we initialise + add remote + seed
            # `.gitignore` + make an empty bootstrap commit here.
            if not is_inside_work_tree(vault_dir):
                _vault_bootstrap(
                    vault_dir,
                    remote_name=gh.vault_remote_name,
                    remote_url=gh.vault_remote_url,
                    branch=gh.vault_branch,
                    author_email=gh.commit_author_email,
                )

            # Step 8 (B-B2): detect unpushed commits FIRST. A non-zero
            # count means a prior run committed locally but the push
            # failed; retry ONLY the push (no re-stage, no new commit).
            # T6.1: explicit remote+branch args — the refspec-based
            # check (``<remote>/<branch>..HEAD``) works without any
            # upstream-tracking config, which our ``push()`` never sets.
            unpushed = unpushed_commit_count(
                vault_dir,
                remote=gh.vault_remote_name,
                branch=gh.vault_branch,
            )
            if unpushed > 0:
                rc, payload = _do_push_cycle(
                    vault_dir=vault_dir,
                    gh=gh,
                    known_hosts=known_hosts,
                    stage=False,
                    message=None,
                )
                payload["retried_unpushed_count"] = unpushed
                print(json.dumps(payload))
                return rc

            # Step 9: change detection via porcelain (R-9 / B2 — `git
            # diff --quiet` misses untracked files).
            porcelain = porcelain_status(vault_dir)
            if not porcelain.strip():
                print(json.dumps({"ok": True, "no_changes": True}))
                return ec.NO_CHANGES

            # Step 10: render the commit message in the owner's tz.
            message = args.message or _render_message(
                gh.commit_message_template, gh.auto_commit_tz
            )

            # Step 11: stage + commit + push. Divergence-reset logic
            # lives inside `_do_push_cycle` (B-B2).
            rc, payload = _do_push_cycle(
                vault_dir=vault_dir,
                gh=gh,
                known_hosts=known_hosts,
                stage=True,
                message=message,
            )
            print(json.dumps(payload))
            return rc
    except LockBusyError:
        print(json.dumps({"ok": False, "error": "lock_busy"}))
        return ec.LOCK_BUSY


# ---------------------------------------------------------------------------
# Argparse wiring


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser.

    All subparsers are registered at this commit so ``--help`` shows the
    complete phase-8 CLI surface. Handlers for ``auth-status`` /
    ``issue`` / ``pr`` / ``repo`` are wired in C2-C3; ``vault-commit-push``
    remains a stub until C4.
    """
    parser = argparse.ArgumentParser(prog="tools/gh/main.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # auth-status — no flags; C2.
    sub.add_parser("auth-status", help="probe `gh` OAuth session")

    # issue — list/view/create, all require --repo for allow-list gate.
    issue_p = sub.add_parser("issue", help="GitHub issues (read-only in phase 8)")
    issue_sub = issue_p.add_subparsers(dest="issue_cmd")

    issue_list = issue_sub.add_parser("list", help="list issues in a repo")
    issue_list.add_argument("--repo", required=True, help="OWNER/REPO slug")
    issue_list.add_argument(
        "--state",
        choices=["open", "closed", "all"],
        default=None,
        help="filter by state",
    )
    issue_list.add_argument(
        "--limit",
        type=int,
        default=30,
        help="max items (hard-capped at 100 by handler)",
    )

    issue_view = issue_sub.add_parser("view", help="view a single issue")
    issue_view.add_argument("number", type=int, help="issue number")
    issue_view.add_argument("--repo", required=True, help="OWNER/REPO slug")

    issue_create = issue_sub.add_parser("create", help="create a new issue")
    issue_create.add_argument("--repo", required=True, help="OWNER/REPO slug")
    issue_create.add_argument("--title", required=True)
    issue_create.add_argument("--body", required=True)
    issue_create.add_argument(
        "--label", action="append", default=[], help="may be repeated"
    )

    # pr — list/view only (no merge / no create in phase 8).
    pr_p = sub.add_parser("pr", help="GitHub pull requests (read-only in phase 8)")
    pr_sub = pr_p.add_subparsers(dest="pr_cmd")

    pr_list = pr_sub.add_parser("list", help="list PRs in a repo")
    pr_list.add_argument("--repo", required=True, help="OWNER/REPO slug")
    pr_list.add_argument(
        "--limit", type=int, default=30, help="max items (hard-capped at 100)"
    )

    pr_view = pr_sub.add_parser("view", help="view a single PR")
    pr_view.add_argument("number", type=int, help="PR number")
    pr_view.add_argument("--repo", required=True, help="OWNER/REPO slug")

    # repo — view only.
    repo_p = sub.add_parser("repo", help="GitHub repo metadata (read-only in phase 8)")
    repo_sub = repo_p.add_subparsers(dest="repo_cmd")
    repo_view = repo_sub.add_parser("view", help="view repo metadata")
    repo_view.add_argument("--repo", required=True, help="OWNER/REPO slug")

    # vault-commit-push — full handler lands in C4.
    vcp = sub.add_parser(
        "vault-commit-push",
        help="commit+push `<data_dir>/vault` to backup remote (C4)",
    )
    vcp.add_argument("--message", default=None)
    vcp.add_argument("--dry-run", action="store_true")

    return parser


def _dispatch(cmd: str, args: argparse.Namespace) -> int:
    """Route ``args.cmd`` to its handler. ``parser.error`` runs first for
    argv-level problems; this function only sees valid subcommands."""
    if cmd == "auth-status":
        return _cmd_auth_status(args)
    if cmd == "issue":
        return _cmd_issue(args)
    if cmd == "pr":
        return _cmd_pr(args)
    if cmd == "repo":
        return _cmd_repo(args)
    if cmd == "vault-commit-push":
        return _cmd_vault_commit_push(args)
    # argparse with required=True should prevent reaching this branch,
    # but be defensive — the CLI never raises NotImplementedError to the
    # shell, it always exits with a documented code.
    sys.stderr.write(
        json.dumps({"ok": False, "error": "unknown_cmd", "cmd": cmd}) + "\n"
    )
    return ec.ARGV


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _dispatch(args.cmd, args)


if __name__ == "__main__":
    sys.exit(main())
