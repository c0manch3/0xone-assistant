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
from pathlib import Path

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


def _cmd_vault_commit_push(_args: argparse.Namespace) -> int:
    return _cmd_stub("vault-commit-push")


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
