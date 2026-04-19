#!/usr/bin/env python3
"""Phase-8 thin CLI wrapper for `gh` + vault auto-commit.

Stdlib-first + direct sub-model instantiation so the CLI runs standalone
(fresh install, manual smoke, cron) WITHOUT ``TELEGRAM_BOT_TOKEN`` /
``OWNER_CHAT_ID`` present in the environment. Mirrors
``tools/schedule/main.py`` + ``tools/genimage/main.py`` (see §0
blocker B-A2).

C2 scope:

- ``auth-status`` — fully wired (gh OAuth session probe).
- ``issue`` / ``pr`` / ``repo`` / ``vault-commit-push`` — argparse
  subparsers registered so ``--help`` lists them and argv validation
  rejects unknown commands, but handlers are C3/C4 territory; they exit
  with a clear ``"not_implemented"`` JSON error instead of a traceback.
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
from tools.gh._lib.gh_ops import gh_auth_status  # noqa: E402

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
    """Shared not-implemented response for C3/C4 stubs.

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


def _cmd_issue(args: argparse.Namespace) -> int:
    return _cmd_stub(f"issue {args.issue_cmd}" if args.issue_cmd else "issue")


def _cmd_pr(args: argparse.Namespace) -> int:
    return _cmd_stub(f"pr {args.pr_cmd}" if args.pr_cmd else "pr")


def _cmd_repo(args: argparse.Namespace) -> int:
    return _cmd_stub(f"repo {args.repo_cmd}" if args.repo_cmd else "repo")


def _cmd_vault_commit_push(_args: argparse.Namespace) -> int:
    return _cmd_stub("vault-commit-push")


# ---------------------------------------------------------------------------
# Argparse wiring


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the top-level parser.

    All subparsers are registered at this commit so ``--help`` shows the
    complete phase-8 CLI surface. Handler logic is wired only for
    ``auth-status``; the rest delegate to ``_cmd_stub`` until C3/C4.
    """
    parser = argparse.ArgumentParser(prog="tools/gh/main.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # auth-status — no flags; C2 only.
    sub.add_parser("auth-status", help="probe `gh` OAuth session")

    # issue — register read + (future) write subparsers, all stubbed.
    issue_p = sub.add_parser("issue", help="GitHub issues (read-only in phase 8)")
    issue_sub = issue_p.add_subparsers(dest="issue_cmd")
    issue_sub.add_parser("list")
    issue_sub.add_parser("view")
    issue_sub.add_parser("create")

    # pr — list/view only (no merge / no create in phase 8).
    pr_p = sub.add_parser("pr", help="GitHub pull requests (read-only in phase 8)")
    pr_sub = pr_p.add_subparsers(dest="pr_cmd")
    pr_sub.add_parser("list")
    pr_sub.add_parser("view")

    # repo — view only.
    repo_p = sub.add_parser("repo", help="GitHub repo metadata (read-only in phase 8)")
    repo_sub = repo_p.add_subparsers(dest="repo_cmd")
    repo_sub.add_parser("view")

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
