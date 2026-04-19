"""Thin `subprocess.run` wrappers around `gh` CLI with env-wipe (Q2, SF-C2/SF-C3).

C2 scope: `build_gh_env` + `gh_auth_status`. `run_gh_json` (general JSON
wrapper) is added in C3 along with the issue/pr/repo subcommands. This
module is stdlib-only so the CLI keeps working on fresh installs without
the assistant's pydantic-settings stack loaded.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# SF-C2 / SF-C3: every variant of GH-scoped token / host / config-dir /
# ssh-override env must be stripped so `gh` and `git` fall back to the
# OAuth session at `~/.config/gh/hosts.yml` and to the explicit
# `GIT_SSH_COMMAND` we set per invocation (with `IdentitiesOnly=yes`).
# Missing any variant would let an old/stale env leak through.
#
# Note: `SSH_AUTH_SOCK` is NOT scrubbed. `IdentitiesOnly=yes` already
# neutralises agent-forwarded keys (ssh will not try any agent identity),
# so leaving the socket intact lets the user keep their dev shell env
# unchanged when running the CLI from a terminal.
_GH_ENV_SCRUB_KEYS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST",
    "GH_CONFIG_DIR",
    "GIT_SSH_COMMAND",
    "SSH_ASKPASS",
)


def build_gh_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env dict with all GH / SSH override variables removed.

    Copies `os.environ` (never mutates it), drops the scrub keys
    enumerated in ``_GH_ENV_SCRUB_KEYS``, then applies ``extra`` overrides
    on top. Callers that need to set `GIT_SSH_COMMAND` (vault-commit-push)
    pass it via ``extra`` so they pick the exact invariants they want.
    """
    env = dict(os.environ)
    for key in _GH_ENV_SCRUB_KEYS:
        env.pop(key, None)
    if extra:
        env.update(extra)
    return env


def gh_auth_status(timeout_s: float = 10.0) -> tuple[int, str, str]:
    """Run ``gh auth status --hostname github.com``; return (rc, stdout, stderr).

    Special return codes:

    - ``rc == 127``: ``gh`` binary not found on ``PATH`` (``FileNotFoundError``
      from `subprocess.run`, or pre-flight `shutil.which` miss). Stderr is
      set to ``"gh not on PATH"`` so the CLI can emit a stable JSON error.
    - ``rc == -1``: ``subprocess.TimeoutExpired`` fired. Stderr is
      ``"timeout"``; stdout empty.

    Callers map ``rc != 0`` to ``GH_NOT_AUTHED`` (exit 4) unless stderr is
    the timeout sentinel (SF-A7 — currently only surfaces in C2 as exit 1
    with ``error: "gh_timeout"``; the Daemon preflight in C6 treats it as
    a soft-warn rather than a hard fail).
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
    except FileNotFoundError:
        # Race: shutil.which saw `gh`, but PATH changed between then and
        # exec, or the binary was rm'd mid-call. Collapse to the same
        # "not on PATH" signal so callers have one branch to handle.
        return (127, "", "gh not on PATH")
    return proc.returncode, proc.stdout, proc.stderr


__all__ = ["build_gh_env", "gh_auth_status"]
