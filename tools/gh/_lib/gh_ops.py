"""Thin `subprocess.run` wrappers around `gh` CLI with env-wipe (Q2, SF-C2/SF-C3).

C2 scope: `build_gh_env` + `gh_auth_status`.
C3 scope (this file): adds `run_gh_json` — general JSON wrapper used by
the issue/pr/repo subcommands. Still stdlib-only so the CLI keeps working
on fresh installs without the assistant's pydantic-settings stack loaded.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

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


# ---------------------------------------------------------------------------
# SF-A5: JSON flattening.
#
# `gh` emits several nested-object shapes that add no information for our
# downstream consumers (the assistant, tests, the model):
# ``{"author": {"login": X}}`` and ``{"defaultBranchRef": {"name": X}}``.
# We flatten both so every ``tools/gh/main.py`` subcommand returns a flat
# mapping — the rest of the codebase can treat these as simple strings
# without learning GitHub's graphql-derived nesting conventions.
#
# The flatten table is keyed by JSON field name (at any depth). We
# intentionally do NOT attempt a generic "collapse any {k: {v}} dict"
# pass — that would silently mangle future schema additions. Instead, we
# enumerate exactly the two shapes we care about. New entries should be
# added here rather than at call-sites.


def _flatten_gh_json(obj: Any) -> Any:
    """Recursively flatten well-known nested single-key GitHub JSON shapes.

    Currently flattens:

    - ``{"author": {"login": X, ...}}`` → ``{"author": X}``
    - ``{"defaultBranchRef": {"name": X, ...}}`` → ``{"default_branch": X}``

    Non-dict / non-list inputs (strings, numbers, None) pass through
    unchanged. Lists are walked element-wise so a list of PRs each with
    a nested ``author`` gets flattened uniformly.

    The function is a pure transform — it copies rather than mutating so
    callers can log the original `gh` payload alongside the flattened
    form for debugging.
    """
    if isinstance(obj, list):
        return [_flatten_gh_json(item) for item in obj]
    if not isinstance(obj, dict):
        return obj

    result: dict[str, Any] = {}
    for key, value in obj.items():
        if (
            key == "author"
            and isinstance(value, dict)
            and isinstance(value.get("login"), str)
        ):
            result[key] = value["login"]
            continue
        if (
            key == "defaultBranchRef"
            and isinstance(value, dict)
            and isinstance(value.get("name"), str)
        ):
            # Rename to snake_case for consistency with other top-level
            # fields our callers expect (``name``, ``description``, etc.).
            result["default_branch"] = value["name"]
            continue
        result[key] = _flatten_gh_json(value)
    return result


def run_gh_json(
    args: list[str], timeout_s: float = 30.0
) -> tuple[int, Any, str]:
    """Run ``gh <args>`` with env-wipe; return (rc, parsed_json_or_None, stderr).

    Sentinel return tuples:

    - ``(-1, None, "timeout")`` — ``subprocess.TimeoutExpired`` fired
      (SF-A7). Handlers map this to exit 1 with ``{"error": "gh_timeout"}``.
    - ``(-1, None, "gh_not_found")`` — ``FileNotFoundError`` from
      ``subprocess.run`` (``gh`` not on ``PATH``). Handlers map to exit 4.
    - ``(rc, None, "gh_returned_invalid_json")`` — ``gh`` exited 0 but
      stdout wasn't parseable JSON. Extremely rare; retained as a
      distinct sentinel because the correct fix is different from a
      transient network failure.

    Normal returns:

    - ``(0, parsed, stderr)`` on success. ``parsed`` is whatever
      ``json.loads`` produced (dict / list / scalar) post-SF-A5 flatten.
      If ``gh`` emitted no stdout (``--json`` with empty result set
      behaves this way in some edge cases), ``parsed`` is ``None``.
    - ``(rc, None, stderr)`` on non-zero rc — caller decides whether
      that's auth failure (``_is_unauth_stderr``) or generic error.

    Environment is sourced from ``build_gh_env()`` so GH_TOKEN /
    GH_CONFIG_DIR / GIT_SSH_COMMAND / SSH_ASKPASS never leak in. The
    subprocess runs with ``capture_output=True`` and ``text=True`` — any
    huge payload returned by `gh` is bounded by ``gh --limit`` flags on
    the caller side, so we don't risk unbounded memory here.
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
    except FileNotFoundError:
        return (-1, None, "gh_not_found")

    if proc.returncode != 0:
        # Propagate stderr verbatim so handlers can classify auth vs
        # generic errors via ``_is_unauth_stderr``.
        return proc.returncode, None, proc.stderr

    if not proc.stdout.strip():
        # ``gh`` returned no output (empty JSON result set, or a
        # command that doesn't emit anything on success). Caller treats
        # ``None`` as "no data" rather than an error.
        return proc.returncode, None, proc.stderr

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # Stable sentinel so callers can differentiate from legitimate
        # "gh stderr said X" failures.
        return proc.returncode, None, "gh_returned_invalid_json"

    # SF-A5: flatten nested {"author": {"login": X}} /
    # {"defaultBranchRef": {"name": X}} shapes before handing the
    # payload to the CLI handler.
    return proc.returncode, _flatten_gh_json(payload), proc.stderr


__all__ = ["build_gh_env", "gh_auth_status", "run_gh_json"]
