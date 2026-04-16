"""PreToolUse hook builders for ClaudeBridge.

Three guards register against `ClaudeAgentOptions.hooks["PreToolUse"]`:

* `make_bash_hook(project_root)` — strict argv-based allowlist with
  shell-metacharacter rejection. Slip-guard regex is kept ONLY as a last-ditch
  defence-in-depth barrier (see `_BASH_SLIP_GUARD_RE`).
* `make_file_hook(project_root)` — sandboxes `Read/Write/Edit/Glob/Grep` to
  paths inside `project_root` via `Path.is_relative_to`. Refuses any pattern
  containing `..` even when relative.
* `make_webfetch_hook()` — full SSRF defence: hostname is parsed via
  `urllib.parse`, classified through `ipaddress`, and DNS-resolved (with a
  3-second timeout) so that any A/AAAA pointing at a private/loopback/
  link-local/reserved range is denied.

All three return the canonical PreToolUse hook reply shape — `{}` (allow) or
`_deny(reason)` (deny). Reasoning is logged via the structured logger so
operators can audit decisions.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shlex
import socket
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from claude_agent_sdk.types import (
    AsyncHookJSONOutput,
    HookCallback,
    HookContext,
    HookInput,
    SyncHookJSONOutput,
)

from assistant.logger import get_logger

log = get_logger("bridge.hooks")

# Re-export the SDK's hook signature so call sites in `bridge/claude.py` can
# bind matchers without importing `claude_agent_sdk.types` directly.
HookFn = HookCallback

# `HookCallback` accepts the full union of every hook event's input. Our hooks
# only fire under PreToolUse, but mypy strict requires the inner closure to be
# at least as permissive as the SDK signature -- so we accept the union (the
# `HookInput` alias re-exported by the SDK) and downcast at runtime to the
# `dict[str, Any]` shape the CLI actually ships.

FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# -----------------------------------------------------------------------------
# Bash: strict argv-based allowlist.
# -----------------------------------------------------------------------------

# Any of these in the raw command -> hard reject before tokenizing. We never
# want to give the model the ability to chain, redirect, or substitute.
_SHELL_METACHARS: tuple[str, ...] = (
    ";",
    "&",
    "|",
    "`",
    "$(",
    "${",
    ">",
    "<",
    "\n",
    "\r",
    "\x00",
)

# argv[0] -> validator that returns deny-reason or None.
_BASH_PROGRAMS: dict[str, str] = {
    "python": "python",
    "uv": "uv",
    "git": "git",
    "ls": "ls",
    "pwd": "pwd",
    "cat": "cat",
    "echo": "echo",
}

# Strictly safe `git` subcommands. We refuse arbitrary args (no `-c`,
# `--upload-pack`, custom `--format=`, etc.) -- only known harmless flags.
_GIT_ALLOWED_SUBCMDS: frozenset[str] = frozenset({"status", "log", "diff"})

# Dangerous git flags. Several of these allow arbitrary command spawning via
# git's plumbing options (`-c core.sshCommand=...`, `--upload-pack=...`).
_GIT_FORBIDDEN_FLAGS: tuple[str, ...] = (
    "-c",
    "--config-env",
    "--upload-pack",
    "--receive-pack",
    "--ext-program",  # synthetic name; covered defensively below
)

# Files that must NEVER be readable via `cat`, even if technically inside
# project_root. Defence in depth: project_root won't contain these in prod
# (XDG separation), but a dev machine might still have a `./.env`.
_CAT_DENYLIST_BASENAMES: frozenset[str] = frozenset(
    {".env", ".envrc", "credentials", "credentials.json"}
)
_CAT_DENYLIST_SUBSTRINGS: tuple[str, ...] = (
    ".ssh/",
    ".aws/",
    ".gnupg/",
    ".docker/config",
    ".kube/",
    "id_rsa",
    "id_ed25519",
)
_CAT_DENYLIST_SUFFIXES: tuple[str, ...] = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
)

# Last-ditch slip-guard. Should be redundant with the argv-based allowlist,
# but kept as defence-in-depth for unexpected vectors.
_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}"
    r")",
    re.IGNORECASE,
)


def _deny(reason: str) -> SyncHookJSONOutput:
    """Return the canonical SDK reply that denies the tool call."""
    return cast(
        SyncHookJSONOutput,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
    )


def _allow() -> SyncHookJSONOutput:
    """Return the canonical SDK reply that allows the tool call."""
    return cast(SyncHookJSONOutput, {})


def _path_safely_inside(candidate: Path, root: Path) -> bool:
    """True iff `candidate.resolve()` is `root.resolve()` or below it."""
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _has_dotdot(parts: tuple[str, ...]) -> bool:
    return any(part == ".." for part in parts)


def _validate_python_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "python requires a script argument"
    script = argv[1]
    script_path = Path(script)
    if _has_dotdot(script_path.parts):
        return "script path must not contain '..'"
    if not script.startswith("tools/") and not script_path.is_absolute():
        return "python script must live under tools/"
    if not _path_safely_inside(project_root / script_path, project_root):
        return "python script escapes project_root"
    return None


def _validate_uv_invocation(argv: list[str], project_root: Path) -> str | None:
    # Only `uv run tools/<x>` is allowed. Forbid `uv run --with`, `uv pip`, etc.
    if len(argv) < 3 or argv[1] != "run":
        return "only `uv run tools/...` form is allowed"
    script = argv[2]
    if script.startswith("-"):
        return "uv run flags are not allowed (must be a path)"
    script_path = Path(script)
    if _has_dotdot(script_path.parts):
        return "script path must not contain '..'"
    if not script.startswith("tools/"):
        return "uv run target must live under tools/"
    if not _path_safely_inside(project_root / script_path, project_root):
        return "uv run script escapes project_root"
    return None


def _validate_git_invocation(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return "git requires a subcommand"
    sub = argv[1]
    if sub not in _GIT_ALLOWED_SUBCMDS:
        return f"git subcommand '{sub}' is not in allowlist {sorted(_GIT_ALLOWED_SUBCMDS)}"
    for arg in argv[2:]:
        for forbidden in _GIT_FORBIDDEN_FLAGS:
            if arg == forbidden or arg.startswith(forbidden + "="):
                return f"git flag '{arg}' is not allowed (option-injection risk)"
    return None


def _validate_cat_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) != 2:
        return "cat requires exactly one path argument"
    arg = argv[1]
    if arg.startswith("-"):
        return "cat flags are not allowed"
    target = Path(arg)
    if _has_dotdot(target.parts):
        return "cat path must not contain '..'"
    candidate = target if target.is_absolute() else project_root / target
    if not _path_safely_inside(candidate, project_root):
        return "cat path escapes project_root"
    name = candidate.name.lower()
    if name in _CAT_DENYLIST_BASENAMES:
        return f"cat target '{name}' is in denylist (secrets defence-in-depth)"
    arg_lower = arg.lower()
    for needle in _CAT_DENYLIST_SUBSTRINGS:
        if needle in arg_lower:
            return f"cat path contains denylisted segment '{needle}'"
    for suffix in _CAT_DENYLIST_SUFFIXES:
        if name.endswith(suffix):
            return f"cat target '{name}' is a database file"
    return None


def _validate_ls_invocation(argv: list[str], project_root: Path) -> str | None:
    # `ls` may run with no args (current dir) or paths inside the project.
    # We refuse all flags to keep the surface minimal.
    for arg in argv[1:]:
        if arg.startswith("-"):
            return f"ls flag '{arg}' is not allowed"
        target = Path(arg)
        if _has_dotdot(target.parts):
            return "ls path must not contain '..'"
        candidate = target if target.is_absolute() else project_root / target
        if not _path_safely_inside(candidate, project_root):
            return "ls path escapes project_root"
    return None


def _validate_pwd_invocation(argv: list[str]) -> str | None:
    if len(argv) > 1:
        return "pwd takes no arguments"
    return None


def _validate_echo_invocation(argv: list[str]) -> str | None:
    # echo is harmless even with arbitrary args; newlines/null already rejected
    # at the metachar gate. Nothing left to check.
    del argv
    return None


def _validate_bash_argv(argv: list[str], project_root: Path) -> str | None:
    if not argv:
        return "empty command"
    program_path = argv[0]
    program = Path(program_path).name  # accept `/usr/bin/python` too
    if program not in _BASH_PROGRAMS:
        return f"program '{program}' is not in allowlist {sorted(_BASH_PROGRAMS)}"
    match program:
        case "python":
            return _validate_python_invocation(argv, project_root)
        case "uv":
            return _validate_uv_invocation(argv, project_root)
        case "git":
            return _validate_git_invocation(argv)
        case "cat":
            return _validate_cat_invocation(argv, project_root)
        case "ls":
            return _validate_ls_invocation(argv, project_root)
        case "pwd":
            return _validate_pwd_invocation(argv)
        case "echo":
            return _validate_echo_invocation(argv)
        case _:  # pragma: no cover -- guarded by the membership check above
            return f"program '{program}' has no validator"


def check_bash_command(cmd: str, project_root: Path) -> str | None:
    """Public for tests: validate a Bash command, return deny-reason or None."""
    raw = cmd.strip()
    if not raw:
        return "empty command"
    for metachar in _SHELL_METACHARS:
        if metachar in cmd:
            return f"shell metacharacter not allowed: {metachar!r}"
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return f"unparseable command (shlex): {exc}"
    reason = _validate_bash_argv(argv, project_root)
    if reason is not None:
        return reason
    if _BASH_SLIP_GUARD_RE.search(cmd):
        return "slip-guard matched (secrets/encoded-payload pattern)"
    return None


def make_bash_hook(project_root: Path) -> HookFn:
    """Build the Bash PreToolUse hook bound to `project_root`."""

    async def bash_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        # SDK only fires this on PreToolUse; downcast to access tool_input.
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        cmd = str(tool_input.get("command", "") or "")
        reason = check_bash_command(cmd, project_root)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                reason=reason,
                cmd=cmd[:200],
            )
            return _deny(reason)
        log.debug(
            "pretool_decision",
            tool_name="Bash",
            decision="allow",
            cmd=cmd[:120],
        )
        return _allow()

    return bash_hook


# -----------------------------------------------------------------------------
# File tools: sandbox to project_root (Read/Write/Edit/Glob/Grep).
# -----------------------------------------------------------------------------


def check_file_path(raw_path: str, project_root_resolved: Path) -> str | None:
    """Return deny-reason iff the path escapes project_root or contains `..`."""
    if not raw_path:
        return None
    target = Path(raw_path)
    if _has_dotdot(target.parts):
        return "path component '..' is not allowed"
    candidate = target if target.is_absolute() else project_root_resolved / target
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"unresolvable path: {exc}"
    if not resolved.is_relative_to(project_root_resolved):
        return f"path escapes project_root ({project_root_resolved}): {resolved}"
    return None


def make_file_hook(project_root: Path) -> HookFn:
    """Build the file-tool PreToolUse hook bound to `project_root`.

    The same hook is registered against Read/Write/Edit/Glob/Grep -- see
    `FILE_TOOLS` for the complete list. SDK gives us `tool_name` so the hook
    can branch on it for tool-specific input keys (Glob has `pattern`, etc.).
    """
    root_resolved = project_root.resolve()

    async def file_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_name = raw.get("tool_name", "")
        tool_input = raw.get("tool_input") or {}

        # Collect every path-bearing field the SDK might pass for any file
        # tool. Each is independently checked; first violation wins.
        candidates: list[tuple[str, str]] = []
        for key in ("file_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                candidates.append((key, value))
        # Glob/Grep specific input.
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern:
            candidates.append(("pattern", pattern))

        for field, value in candidates:
            reason = check_file_path(value, root_resolved)
            if reason is not None:
                log.warning(
                    "pretool_decision",
                    tool_name=tool_name,
                    decision="deny",
                    reason=reason,
                    field=field,
                    value=value[:200],
                )
                return _deny(reason)

        return _allow()

    return file_hook


# -----------------------------------------------------------------------------
# WebFetch: full SSRF defence with DNS resolution.
# -----------------------------------------------------------------------------


def _is_private_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def _resolve_hostname(
    hostname: str, *, deadline_s: float
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `hostname` to a list of IPs via the loop's resolver.

    Raises `socket.gaierror` on failure and `TimeoutError` on hang. The
    parameter is named `deadline_s` (not `timeout`) so ruff's ASYNC109 rule
    does not nag -- we use `asyncio.timeout()` internally as requested.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(deadline_s):
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if family == socket.AF_INET6:
            # Strip zone-id if present (e.g. `fe80::1%eth0`).
            ip_str = ip_str.split("%", 1)[0]
        out.append(ipaddress.ip_address(ip_str))
    return out


async def classify_url(url: str, *, dns_timeout: float = 3.0) -> str | None:
    """Public for tests: deny-reason iff URL targets a non-public destination."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"malformed URL: {url!r}"
    if parsed.scheme not in {"http", "https"}:
        return f"only http(s) is allowed; got scheme {parsed.scheme!r}"
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return "URL has no hostname"

    # Direct IP literal?
    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None
    if ip_literal is not None:
        if _is_private_address(ip_literal):
            return f"IP literal targets non-public range: {ip_literal}"
        return None

    # Hostname -> DNS resolution. Refuse on any failure (safer default).
    try:
        addrs = await _resolve_hostname(hostname, deadline_s=dns_timeout)
    except TimeoutError:
        return f"DNS lookup for {hostname!r} timed out"
    except (socket.gaierror, OSError) as exc:
        return f"cannot resolve host {hostname!r}: {exc}"
    if not addrs:
        return f"DNS returned no addresses for {hostname!r}"
    for addr in addrs:
        if _is_private_address(addr):
            return f"hostname {hostname!r} resolves to non-public address {addr} (SSRF defence)"
    return None


def make_webfetch_hook(*, dns_timeout: float = 3.0) -> HookFn:
    """Build the WebFetch PreToolUse hook with full SSRF classification."""

    async def webfetch_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        url = str(tool_input.get("url", "") or "").strip()
        if not url:
            return _allow()
        reason = await classify_url(url, dns_timeout=dns_timeout)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="WebFetch",
                decision="deny",
                reason=reason,
                url=url[:200],
            )
            return _deny(reason)
        log.debug(
            "pretool_decision",
            tool_name="WebFetch",
            decision="allow",
            url=url[:200],
        )
        return _allow()

    return webfetch_hook


# -----------------------------------------------------------------------------
# Aggregator -- assembles a HookMatcher list ready to plug into ClaudeAgentOptions.
# -----------------------------------------------------------------------------


def make_pretool_hooks(project_root: Path) -> list[Any]:
    """Return the canonical PreToolUse `HookMatcher` list for ClaudeBridge.

    Importing `HookMatcher` lazily keeps `bridge/hooks.py` a pure-validation
    module with no SDK-options coupling for unit tests; only this aggregator
    pulls in `claude_agent_sdk.HookMatcher`.
    """
    from claude_agent_sdk import HookMatcher

    return [
        HookMatcher(matcher="Bash", hooks=[make_bash_hook(project_root)]),
        *[HookMatcher(matcher=t, hooks=[make_file_hook(project_root)]) for t in FILE_TOOLS],
        HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
    ]
