"""PreToolUse + PostToolUse hook builders for ClaudeBridge.

Three PreToolUse guards register against
`ClaudeAgentOptions.hooks["PreToolUse"]`:

* `make_bash_hook(project_root)` — strict argv-based allowlist with
  shell-metacharacter rejection. Slip-guard regex is kept ONLY as a last-ditch
  defence-in-depth barrier (see `_BASH_SLIP_GUARD_RE`).
* `make_file_hook(project_root)` — sandboxes `Read/Write/Edit/Glob/Grep` to
  paths inside `project_root` via `Path.is_relative_to`. Refuses any pattern
  containing `..` even when relative.
* `make_webfetch_hook()` — full SSRF defence: hostname is parsed via
  `urllib.parse`, classified through `ipaddress`, and DNS-resolved (with a
  3-second timeout) so that any A/AAAA pointing at a private/loopback/
  link-local/reserved range is denied. Delegates to `bridge/net.py`.

Phase 3 adds a PostToolUse matcher (`make_posttool_hooks`) that touches
`<data_dir>/run/skills.dirty` whenever Write/Edit lands inside `skills/` or
`tools/` — the sentinel drives hot-reload of the manifest cache.

All return the canonical SDK hook reply shape — `{}` (allow / no-op) or
`_deny(reason)` (PreToolUse deny). Reasoning is logged via the structured
logger so operators can audit decisions.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
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

from assistant.bridge.net import (
    classify_url as _classify_url_via_net,
)
from assistant.bridge.net import (
    is_private_address as _net_is_private_address,
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

# argv[0] -> validator is routed via the match statement in
# `_validate_bash_argv`. The dict is kept as the allowlist membership gate.
_BASH_PROGRAMS: dict[str, str] = {
    "python": "python",
    "uv": "uv",
    "git": "git",
    "ls": "ls",
    "pwd": "pwd",
    "cat": "cat",
    "echo": "echo",
    "gh": "gh",  # phase 3: read-only `gh api` / `gh auth status` only
}

# H-1 (phase 3): both `tools/` and `skills/` are valid roots for `python` /
# `uv run` scripts. Anthropic's `skill-creator` bundle ships `scripts/*.py`
# under `skills/skill-creator/scripts/` that the model is expected to invoke
# directly. Without this prefix, the bootstrap path fails with "python
# script must live under tools/". See plan/phase3/implementation.md §2.9.
_PYTHON_ALLOWED_PREFIXES: tuple[str, ...] = ("tools/", "skills/")
_UV_RUN_ALLOWED_PREFIXES: tuple[str, ...] = ("tools/", "skills/")

# Strictly safe `git` subcommands. We refuse arbitrary args (no `-c`,
# `--upload-pack`, custom `--format=`, etc.) -- only known harmless flags.
_GIT_ALLOWED_SUBCMDS: frozenset[str] = frozenset({"status", "log", "diff", "clone"})

# Dangerous git flags. Several of these allow arbitrary command spawning via
# git's plumbing options (`-c core.sshCommand=...`, `--upload-pack=...`).
_GIT_FORBIDDEN_FLAGS: tuple[str, ...] = (
    "-c",
    "--config-env",
    "--upload-pack",
    "--receive-pack",
    "--ext-program",  # synthetic name; covered defensively below
)

# phase 3: `gh` CLI read-only. Only two subcommands, tightly framed.
_GH_ALLOWED_SUBCMDS: frozenset[str] = frozenset({"api", "auth"})
_GH_AUTH_ALLOWED_SUBSUB: frozenset[str] = frozenset({"status"})

# Endpoints the model is allowed to hit via `gh api`. Must stay read-only —
# `/repos/<owner>/<repo>/contents[...]` and `/repos/<owner>/<repo>/tarball[...]`
# are the only shapes the skill-installer actually needs for marketplace flow.
# Query strings are tolerated (`?ref=main` etc.) but must not contain
# whitespace. Any other endpoint (`/graphql`, `/user`, `/search/...`) — deny.
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

# Flags that turn `gh api` into a write request. Blocked at argv level — we
# do NOT rely on remote-side authz (spike S2.e: the CLI still fires the
# request and only the server returns 403, which leaks intent).
_GH_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "-X",
        "--method",
        "--method-override",
        "-F",
        "--field",
        "-f",
        "--raw-field",
        "--input",
    }
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
    if not script_path.is_absolute() and not any(
        script.startswith(p) for p in _PYTHON_ALLOWED_PREFIXES
    ):
        return (
            f"python script must live under one of {list(_PYTHON_ALLOWED_PREFIXES)}; "
            f"got {script!r}"
        )
    if not _path_safely_inside(project_root / script_path, project_root):
        return "python script escapes project_root"
    return None


def _validate_uv_run(argv: list[str], project_root: Path) -> str | None:
    # `uv run <path>` — no flags, path must live under an allowed prefix.
    if len(argv) < 3:
        return "uv run requires a script argument"
    script = argv[2]
    if script.startswith("-"):
        return "uv run flags are not allowed (must be a path)"
    script_path = Path(script)
    if _has_dotdot(script_path.parts):
        return "script path must not contain '..'"
    if not any(script.startswith(p) for p in _UV_RUN_ALLOWED_PREFIXES):
        return (
            f"uv run target must live under one of {list(_UV_RUN_ALLOWED_PREFIXES)}; "
            f"got {script!r}"
        )
    if not _path_safely_inside(project_root / script_path, project_root):
        return "uv run script escapes project_root"
    return None


def _validate_uv_sync(argv: list[str], project_root: Path) -> str | None:
    # `uv sync --directory tools/<name>` OR `uv sync --directory=tools/<name>`.
    # Rationale: Bash hook cannot observe `cd` (metachars rejected), so the
    # only way to scope `uv sync` is the explicit `--directory` flag.
    directory: str | None = None
    i = 2
    while i < len(argv):
        token = argv[i]
        if token == "--directory":
            if i + 1 >= len(argv):
                return "uv sync --directory requires a value"
            directory = argv[i + 1]
            i += 2
            continue
        if token.startswith("--directory="):
            directory = token.split("=", 1)[1]
            i += 1
            continue
        return f"uv sync flag {token!r} is not allowed"
    if directory is None:
        return "uv sync requires --directory=tools/<name>"
    dir_path = Path(directory)
    if _has_dotdot(dir_path.parts):
        return "uv sync --directory must not contain '..'"
    if not directory.startswith("tools/"):
        return "uv sync --directory must live under tools/"
    if not _path_safely_inside(project_root / dir_path, project_root / "tools"):
        return "uv sync --directory escapes tools/"
    return None


def _validate_uv_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "uv requires a subcommand"
    sub = argv[1]
    if sub == "run":
        return _validate_uv_run(argv, project_root)
    if sub == "sync":
        return _validate_uv_sync(argv, project_root)
    return f"uv subcommand '{sub}' is not allowed"


def _validate_git_clone(argv: list[str], project_root: Path) -> str | None:
    # Shape: `git clone --depth=1 <https-url> <dest>`.
    # Deliberately strict — no LFS, no recurse-submodules, no config override.
    if len(argv) != 5:
        return "git clone must be `git clone --depth=1 <url> <dest>`"
    if argv[2] != "--depth=1":
        return "only `git clone --depth=1 ...` is allowed"
    url = argv[3]
    if not (url.startswith("https://") or url.startswith("git@github.com:")):
        return "git clone: only https:// or git@github.com: URLs are allowed"
    if url.startswith("https://"):
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip()
        if not hostname:
            return "git clone: URL has no hostname"
        try:
            ip_literal = ipaddress.ip_address(hostname)
        except ValueError:
            ip_literal = None
        if ip_literal is not None and _net_is_private_address(ip_literal):
            return (
                f"git clone: URL hostname {hostname!r} is a non-public IP literal"
            )
    dest = argv[4]
    dest_path = Path(dest)
    if _has_dotdot(dest_path.parts):
        return "git clone dest must not contain '..'"
    candidate = dest_path if dest_path.is_absolute() else project_root / dest_path
    if not _path_safely_inside(candidate, project_root):
        return "git clone dest escapes project_root"
    return None


def _validate_git_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "git requires a subcommand"
    sub = argv[1]
    if sub not in _GIT_ALLOWED_SUBCMDS:
        return (
            f"git subcommand '{sub}' is not in allowlist {sorted(_GIT_ALLOWED_SUBCMDS)}"
        )
    # Scan args for option-injection regardless of subcommand. `clone` also
    # needs its positional layout validated below.
    for arg in argv[2:]:
        for forbidden in _GIT_FORBIDDEN_FLAGS:
            if arg == forbidden or arg.startswith(forbidden + "="):
                return f"git flag '{arg}' is not allowed (option-injection risk)"
    if sub == "clone":
        return _validate_git_clone(argv, project_root)
    return None


def _validate_gh_invocation(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return "gh requires a subcommand"
    sub = argv[1]
    if sub not in _GH_ALLOWED_SUBCMDS:
        return f"gh subcommand '{sub}' not allowed"
    if sub == "auth":
        if len(argv) == 3 and argv[2] in _GH_AUTH_ALLOWED_SUBSUB:
            return None
        return "only `gh auth status` is allowed"
    # sub == "api"
    # Reject write-flags BEFORE looking at the endpoint — a request must
    # never leave the host with `-X POST`.
    for flag in argv[2:]:
        if flag in _GH_FORBIDDEN_FLAGS:
            return f"gh api: flag {flag} not allowed (read-only)"
        if any(flag.startswith(f + "=") for f in _GH_FORBIDDEN_FLAGS):
            return f"gh api: flag {flag.split('=', 1)[0]} not allowed (read-only)"
    endpoint = next((a for a in argv[2:] if a.startswith("/")), None)
    if not endpoint:
        return "gh api requires an endpoint path starting with '/'"
    if not _GH_API_SAFE_ENDPOINT_RE.match(endpoint):
        return f"gh api: endpoint {endpoint!r} not in read-only whitelist"
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
            return _validate_git_invocation(argv, project_root)
        case "gh":
            return _validate_gh_invocation(argv)
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
# WebFetch: SSRF defence — delegates to `bridge/net.py` for the shared helpers.
# -----------------------------------------------------------------------------


# Keep the private aliases so phase-2 tests / downstream importers that rely on
# them continue to work. The canonical implementation lives in `bridge/net.py`
# under a public name; re-export here.
_is_private_address = _net_is_private_address


async def classify_url(url: str, *, dns_timeout: float = 3.0) -> str | None:
    """Public for tests: deny-reason iff URL targets a non-public destination.

    Thin delegate to `bridge/net.py::classify_url` — the logic lives once,
    inside the mirrored SSRF block, so the installer mirror stays honest.
    """
    return await _classify_url_via_net(url, dns_timeout=dns_timeout)


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


# -----------------------------------------------------------------------------
# PostToolUse: hot-reload sentinel for Write/Edit inside skills/ or tools/.
# -----------------------------------------------------------------------------


def _is_inside_skills_or_tools(raw_path: str, project_root: Path) -> bool:
    """True iff `raw_path` resolves beneath `<project_root>/skills/` or
    `<project_root>/tools/`. Any `..` or unresolvable path → False.

    Phase-3 §2.2 correction vs detailed-plan: the substring check
    `"/skills/" in path` would match `/tmp/evil/skills/x` even when
    project_root is elsewhere. `Path.is_relative_to` against a resolved
    root is the only honest test.
    """
    if not raw_path:
        return False
    try:
        target = Path(raw_path)
    except ValueError:
        return False
    if _has_dotdot(target.parts):
        return False
    try:
        abs_path = (
            target.resolve() if target.is_absolute() else (project_root / target).resolve()
        )
    except (OSError, ValueError):
        return False
    try:
        root = project_root.resolve()
    except (OSError, ValueError):
        return False
    for sub in ("skills", "tools"):
        base = root / sub
        try:
            if abs_path.is_relative_to(base):
                return True
        except ValueError:  # pragma: no cover -- different anchor on POSIX is impossible
            continue
    return False


def make_posttool_sentinel_hook(project_root: Path, data_dir: Path) -> HookFn:
    """Build the Write/Edit PostToolUse hook that touches the hot-reload sentinel.

    PostToolUse cannot deny — the tool has already run. We only observe
    side effects: if the target lived under `skills/` or `tools/`, touch
    `<data_dir>/run/skills.dirty` so the next `ClaudeBridge._render_system_prompt`
    call will invalidate the manifest cache.

    Any `OSError` is swallowed (logged) — a broken sentinel must not
    propagate into the model's tool-result stream.
    """
    sentinel = data_dir / "run" / "skills.dirty"

    async def posttool_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        file_path = str(tool_input.get("file_path") or "")
        if _is_inside_skills_or_tools(file_path, project_root):
            try:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()
                log.info(
                    "posttool_sentinel_touched",
                    tool_name=raw.get("tool_name"),
                    file_path=file_path[:200],
                )
            except OSError as exc:
                log.warning("posttool_sentinel_touch_failed", error=repr(exc))
        return cast(SyncHookJSONOutput, {})

    return posttool_hook


def make_posttool_hooks(project_root: Path, data_dir: Path) -> list[Any]:
    """Return the phase-3 PostToolUse `HookMatcher` list.

    Two matchers (Write + Edit), sharing the same underlying callback. Any
    tool landing inside `skills/` or `tools/` triggers a sentinel touch —
    the next bridge turn sees the new manifest without a daemon restart.
    """
    from claude_agent_sdk import HookMatcher

    hook = make_posttool_sentinel_hook(project_root, data_dir)
    return [
        HookMatcher(matcher="Write", hooks=[hook]),
        HookMatcher(matcher="Edit", hooks=[hook]),
    ]
