from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from claude_agent_sdk import (
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
)

from assistant.logger import get_logger

log = get_logger("bridge.hooks")

# ---------------------------------------------------------------------------
# Hook callable signature.
#
# The SDK exposes ``HookCallback`` — an alias over ``Callable[[HookInput,
# str | None, HookContext], Awaitable[HookJSONOutput]]``. We reuse it so
# ``ClaudeAgentOptions.hooks`` typechecks without casts.
# ---------------------------------------------------------------------------
Hook = HookCallback


# ---------------------------------------------------------------------------
# Bash allowlist-first (R8 hardened slip-guard; 36/36 bypass matrix passes).
#
# BW2 invariant: every entry in ``BASH_ALLOWLIST_PREFIXES`` MUST end with a
# space (argument separator) or a ``/`` (path separator). Bare tokens like
# ``"ls"``/``"pwd"`` match unrelated utilities via ``str.startswith`` —
# ``lsof``/``lsblk``/``lslocks``/``ls-files`` and ``pwdx``/``pwdgen`` would
# all slip through. The former can leak ``/proc/<pid>/environ`` (real
# secrets vector); the latter leaks cwd of other processes. Exact-match
# tokens (``"ls"``, ``"pwd"``) live in ``BASH_ALLOWLIST_EXACT`` below.
# ---------------------------------------------------------------------------
BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "python3 tools/",
    "uv run tools/",
    # NEW phase-3 entries — Anthropic skill-creator ships scripts/*.py
    "python skills/",
    "python3 skills/",
    "uv run skills/",
    "git status ",
    "git log ",
    "git diff ",
    "ls ",
    "echo ",
    # "cat <path>..." — handled by dedicated _cat_targets_ok below.
)

# Exact-match allowlist: ``stripped == token`` (no suffix permitted).
# Keeps ``ls``/``pwd``/``git status`` usable alone without re-admitting
# ``lsof``/``pwdx``/``git status-pickaxe``.
BASH_ALLOWLIST_EXACT: frozenset[str] = frozenset(
    {
        "ls",
        "pwd",
        "git status",
        "git log",
        "git diff",
    }
)

# Invariant check — fail fast if anyone adds a bare token without a
# trailing space/slash. Runs at import time so misconfiguration never
# reaches production.
for _p in BASH_ALLOWLIST_PREFIXES:
    assert _p.endswith((" ", "/")), (
        f"BASH_ALLOWLIST_PREFIXES entry {_p!r} must end with ' ' or '/' — "
        "bare tokens match unrelated utilities like lsof/pwdx. Use "
        "BASH_ALLOWLIST_EXACT for standalone commands."
    )
del _p

BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}|"
    r"[;&|`]|\$\(|<\(|>\(|"
    # B2 (wave-3): reject ``$VAR`` / ``${VAR}`` expansion — without this,
    # ``cat $HOME/.ssh/config`` slips past the literal-path allowlist because
    # ``$HOME`` is a string token, not an expanded path. The guard is
    # applied universally (after the per-command allowlist), so even
    # ``echo $PATH`` is denied — the owner can use ``printenv`` via an
    # explicit tools/ entry if env-dumping is genuinely wanted.
    r"\$\{|\$[A-Za-z_]|"
    r"\\x[0-9a-f]{2}|\\[0-7]{3}"
    r")",
    re.IGNORECASE,
)

FILE_TOOL_NAMES: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# SW1: WebFetch Layer-1 literal blocks.
#
# Previous implementation used ``host.startswith(needle)`` with needles like
# ``"10."`` and ``"localhost"`` plus a ``needle in url`` full-URL scan. That
# produced false positives on legitimate public hostnames (``10example.com``,
# ``1000guns.com``) and on URL paths containing ``/v10.0/...``. Layer 2 (DNS
# resolve + ``ipaddress`` category check) is still the authoritative guard;
# Layer 1 exists only to short-circuit obvious literal attempts without DNS.
#
# We now split Layer 1 into two precise categories:
#   - ``WEBFETCH_BLOCKED_HOSTNAMES`` — exact hostname match OR subdomain
#     (``host == name or host.endswith("." + name)``).
#   - IP literals are parsed through ``ipaddress.ip_address`` and checked
#     against the standard private/loopback/link-local/reserved/multicast/
#     unspecified categories — the same criteria as Layer 2.
WEBFETCH_BLOCKED_HOSTNAMES: tuple[str, ...] = (
    "localhost",
    "metadata.google.internal",  # GCE metadata
    "metadata",  # bare hostname shortcut used in some cloud images
)


def _deny(reason: str) -> HookJSONOutput:
    """Build the PreToolUse deny payload the SDK expects.

    The return annotation is the SDK's ``HookJSONOutput`` union but
    structurally this is ``SyncHookJSONOutput`` (TypedDict with
    ``hookSpecificOutput.hookEventName/permissionDecision/...``). We cast
    to satisfy mypy; the runtime shape is what the SDK eats.
    """
    return cast(
        HookJSONOutput,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
    )


def _truncate_strings(obj: Any, *, max_len: int = 2048) -> Any:
    """Recursively truncate every ``str`` leaf in ``obj`` to ``max_len`` chars.

    Used by the memory PostToolUse audit hook (Fix 1 / C4-W3) to cap
    the size of each ``tool_input`` field written to ``memory-audit.log``.
    Keeps dict/list structure intact; non-container non-string values
    pass through unchanged.

    Truncated strings get a ``...<truncated>`` suffix so operators can
    tell at-a-glance that the entry was abridged.
    """
    if isinstance(obj, str):
        if len(obj) <= max_len:
            return obj
        return obj[:max_len] + "...<truncated>"
    if isinstance(obj, dict):
        return {k: _truncate_strings(v, max_len=max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_strings(v, max_len=max_len) for v in obj]
    return obj


def _allow() -> HookJSONOutput:
    """No-op allow response (an empty SyncHookJSONOutput)."""
    return cast(HookJSONOutput, {})


# ---------------- Bash ----------------


def _cat_targets_ok(args: list[str], project_root: Path) -> tuple[bool, str | None]:
    """Validate ALL positional ``cat`` args resolve inside ``project_root`` (B7).

    Returns ``(ok, deny_reason)``. ``ok=True, reason=None`` → allow.
    Any ``-`` flag-style arg → deny (conservative — no ``-n``, ``-A``, etc.).

    BW1: containment is checked with ``Path.is_relative_to`` — NOT
    ``str.startswith``. A sibling project directory shares the root's path
    prefix as a string (``/foo/project-other`` starts with ``/foo/project``)
    and ``startswith`` would silently admit cross-project reads.
    """
    root = project_root.resolve()
    if not args:
        return False, "cat with no arguments"
    if any(a.startswith("-") for a in args):
        return False, "cat flags (-n, -A, etc.) not allowed"
    for target in args:
        try:
            p = Path(target).expanduser()
            resolved = (project_root / p).resolve() if not p.is_absolute() else p.resolve()
        except OSError as exc:
            return False, f"cat target {target!r}: {exc}"
        if not resolved.is_relative_to(root):
            return False, f"cat target {target!r} resolves outside project_root"
    return True, None


# ---------------------------------------------------------------------------
# Phase 3 argv-level validators for gh / git clone / uv sync.
#
# The allowlist-prefix layer (``BASH_ALLOWLIST_PREFIXES``) is string-level;
# it admits ``gh api ...`` as a prefix, but the model could smuggle flags
# like ``--hostname evil.com`` into the tail. B11 (wave-2) closes this with
# an argv-level FLAG ALLOW-LIST: only ``-H Accept:*`` headers are permitted
# alongside the endpoint path. Every other flag is denied by default.
# ---------------------------------------------------------------------------
_GH_AUTH_ALLOWED_SUBSUB: frozenset[str] = frozenset({"status"})
# S9 (wave-3): negative lookahead against ``..`` anywhere in the path. The
# owner-controlled ``[A-Za-z0-9_.-]+`` classes admit literal ``..`` (it
# matches dot-dot because ``.`` is in the class), so without the lookahead
# a crafted endpoint like ``/repos/owner/repo/contents/a/../b`` would pass
# validation and let the server resolve outside the intended subtree. The
# same regex is duplicated in ``_installer_core._GH_API_SAFE_ENDPOINT_RE``
# and MUST be kept in sync.
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/(?!.*\.\.)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)
_GH_API_ALLOWED_HEADER_PREFIX = "accept:"

# DEPRECATED: _GH_FORBIDDEN_FLAGS is no longer consulted.
# Retained only as a breadcrumb for the pre-B11 deny-list approach.
# The whitelist in ``_validate_gh_argv`` is authoritative.
_GH_FORBIDDEN_FLAGS: frozenset[str] = frozenset()


def _validate_gh_argv(argv: list[str]) -> str | None:
    """Return error string if argv is not a safe read-only gh invocation.

    Only two invocations pass:
        gh auth status
        gh api [<-H Accept:...> ...] /<read-only endpoint>

    Every other gh subcommand and every non-Accept ``-H`` header is denied.
    Any unknown flag (``--hostname``, ``--paginate``, ``-X``, ...) is denied
    by default — the allow-list is authoritative.
    """
    if len(argv) < 2 or argv[0] != "gh":
        return "not gh command"
    sub = argv[1]
    if sub == "auth":
        if len(argv) == 3 and argv[2] in _GH_AUTH_ALLOWED_SUBSUB:
            return None
        return "only `gh auth status` is allowed"
    if sub != "api":
        return f"gh {sub!r}: only `gh api` and `gh auth status` are whitelisted"
    # sub == "api" — walk argv[2:] and accept only endpoint + -H Accept:*.
    i = 2
    saw_endpoint = False
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("/"):
            if saw_endpoint:
                return "gh api: multiple endpoints not allowed"
            if not _GH_API_SAFE_ENDPOINT_RE.match(tok):
                return f"gh api: endpoint {tok!r} not in read-only whitelist"
            saw_endpoint = True
            i += 1
            continue
        if tok == "-H":
            if i + 1 >= len(argv):
                return "gh api: -H requires header argument"
            hdr = argv[i + 1]
            if not hdr.lower().startswith(_GH_API_ALLOWED_HEADER_PREFIX):
                return f"gh api: -H {hdr!r}: only Accept headers allowed"
            i += 2
            continue
        return f"gh api: flag {tok!r} not in read-only allow-list"
    if not saw_endpoint:
        return "gh api requires endpoint path"
    return None


def _validate_git_clone_argv(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 4:
        return "git clone requires --depth=1 URL DEST"
    if argv[2] != "--depth=1":
        return "only --depth=1 is allowed for git clone"
    url = argv[3]
    if not (url.startswith("https://github.com/") or url.startswith("git@github.com:")):
        return "only https://github.com/ or git@github.com: URLs are allowed"
    if len(argv) < 5:
        return "git clone requires DEST"
    dest = argv[4]
    try:
        dp = Path(dest).expanduser()
        resolved = (project_root / dp).resolve() if not dp.is_absolute() else dp.resolve()
    except OSError as exc:
        return f"invalid dest path: {exc}"
    if not resolved.is_relative_to(project_root.resolve()):
        return "git clone dest must be inside project_root"
    return None


def _validate_uv_sync_argv(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "uv requires subcommand"
    if argv[1] != "sync":
        return None  # other uv subcommands covered by existing `uv run` prefix
    dir_arg = next((a for a in argv[2:] if a.startswith("--directory")), None)
    if not dir_arg:
        return "uv sync requires --directory=tools/<name> or skills/<name>"
    if "=" in dir_arg:
        path_val = dir_arg.split("=", 1)[1]
    else:
        idx = argv.index(dir_arg)
        if idx + 1 >= len(argv):
            return "uv sync --directory requires a value"
        path_val = argv[idx + 1]
    try:
        dp = Path(path_val).expanduser()
        full = (project_root / dp).resolve() if not dp.is_absolute() else dp.resolve()
    except OSError as exc:
        return f"invalid directory path: {exc}"
    pr = project_root.resolve()
    if not (full.is_relative_to(pr / "tools") or full.is_relative_to(pr / "skills")):
        return "uv sync --directory must target tools/ or skills/"
    return None


def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    """Return a deny-reason iff ``cmd`` is NOT allowed. ``None`` → allow.

    BW2: exact matches checked separately so ``ls``/``pwd`` don't re-admit
    ``lsof``/``pwdx``/``lsblk`` via naive ``startswith``.

    Phase 3: ``gh`` / ``git clone`` / ``uv sync`` are gated through
    dedicated argv validators (NH-10: ``shlex.split`` may raise on
    unbalanced quotes — we translate that to a deny-reason).
    """
    import shlex

    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if stripped in BASH_ALLOWLIST_EXACT:
        return None
    if any(stripped.startswith(p) for p in BASH_ALLOWLIST_PREFIXES):
        return None
    try:
        argv = shlex.split(stripped)
    except ValueError as exc:
        return f"unparseable Bash command: {exc}"
    if not argv:
        return "empty argv"
    if argv[0] == "gh":
        return _validate_gh_argv(argv)
    if argv[0] == "git" and len(argv) > 1 and argv[1] == "clone":
        return _validate_git_clone_argv(argv, project_root)
    if argv[0] == "uv" and len(argv) > 1 and argv[1] == "sync":
        return _validate_uv_sync_argv(argv, project_root)
    # Special-case `cat <path>...` — allow iff ALL args resolve inside project_root.
    if stripped.startswith("cat "):
        args = stripped[4:].strip().split()
        ok, reason = _cat_targets_ok(args, project_root)
        if ok:
            return None
        return reason or "cat target outside project_root"
    return (
        "Bash command not in allowlist. If you need this operation, ask the "
        "owner to add it to tools/<name>/main.py or expand the allowlist. "
        "Note: installer @tool calls bypass this allowlist entirely "
        "(enforced at @tool function arg-validation time)."
    )


def make_bash_hook(project_root: Path) -> Hook:
    """Allowlist-first Bash guard + R8 slip-guard defence-in-depth."""

    async def bash_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        # PreToolUse inputs carry tool_input; other variants don't. Since
        # we only register this on PreToolUse, a missing tool_input is
        # unexpected but defensively tolerated.
        data = cast(dict[str, Any], input_data)
        cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
        reason = _bash_allowlist_check(cmd, project_root)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                subreason="allowlist",
                cmd=cmd[:200],
            )
            return _deny(reason)
        if BASH_SLIP_GUARD_RE.search(cmd):
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                subreason="slip_guard",
                cmd=cmd[:200],
            )
            return _deny(
                "Command matched a secrets/encoded-payload pattern. Reading "
                ".env/.ssh/.aws/tokens/encoded blobs via Bash is blocked."
            )
        log.debug("pretool_decision", tool_name="Bash", decision="allow", cmd=cmd[:120])
        return _allow()

    return bash_hook


# ---------------- File-tools (Read/Write/Edit/Glob/Grep) ----------------


def make_file_hook(project_root: Path) -> Hook:
    """Single factory used for all 5 file-tool HookMatcher entries.

    B8 fix: ALWAYS resolve against ``project_root`` (relative OR absolute).
      The SDK's ``cwd=project_root`` would resolve relatives at fetch time,
      so we MUST match that behaviour when validating — a naive
      "only resolve if absolute" check lets ``../../../etc/passwd`` through.
    B9 fix: ``Read``/``Write``/``Edit`` require ``file_path`` (empty ⇒ deny).
      ``Glob``/``Grep`` allow empty ``path`` (defaults to the project root,
      which is the intended behaviour for whole-repo searches).
    """
    root = project_root.resolve()

    async def file_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        data = cast(dict[str, Any], input_data)
        tool_name = data.get("tool_name") or ""
        ti = data.get("tool_input", {}) or {}

        candidate: str
        if tool_name in ("Read", "Write", "Edit"):
            raw = ti.get("file_path")
            if not raw:
                log.warning(
                    "pretool_decision",
                    tool_name=tool_name,
                    decision="deny",
                    subreason="missing_file_path",
                )
                return _deny(f"{tool_name} requires file_path")
            candidate = str(raw)
        elif tool_name in ("Glob", "Grep"):
            candidate = str(ti.get("path") or ".")
        else:
            # Hook should only be registered for the 5 tools above;
            # defensively allow for any accidentally-registered tool.
            return _allow()

        # Path resolution is pure-Python (no async I/O) — the SDK awaits
        # this hook but we're not blocking anything observable.
        try:
            p = Path(candidate).expanduser()  # noqa: ASYNC240
            resolved = p.resolve() if p.is_absolute() else (project_root / p).resolve()
        except OSError as exc:
            return _deny(f"invalid path {candidate!r}: {exc}")

        # BW1: ``is_relative_to`` — NOT ``str.startswith``. ``startswith``
        # treats a sibling project (``/foo/project-other``) as inside
        # ``/foo/project`` because the string prefix matches, admitting
        # cross-project secret reads via ``../project-other/.env``.
        if not resolved.is_relative_to(root):
            log.warning(
                "pretool_decision",
                tool_name=tool_name,
                decision="deny",
                subreason="outside_project_root",
                path=str(resolved),
            )
            return _deny(f"Path outside project_root ({root}) is not allowed: {resolved}")
        return _allow()

    return file_hook


# ---------------- WebFetch SSRF (R9 two-layer: string + DNS → ipaddress) ----------------


def _ip_is_blocked(ip_str: str) -> str | None:
    """Return a reason if the IP is in a private/reserved/loopback range."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"invalid IP {ip_str!r}"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link_local"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    return None


def _layer1_block(host: str) -> str | None:
    """SW1: precise Layer-1 check for WebFetch.

    - If ``host`` parses as an IP literal (IPv4 or IPv6), check the
      standard ``ipaddress`` categories (private/loopback/link-local/
      reserved/multicast/unspecified) — same criteria used in Layer 2.
      IPv6 hostnames arrive from ``urlparse.hostname`` with brackets
      already stripped (``[::1]`` → ``::1``), so no bracket-stripping
      dance is needed.
    - Otherwise check ``WEBFETCH_BLOCKED_HOSTNAMES`` with exact-match OR
      suffix-match (``host == name or host.endswith("." + name)``).
      Substring matches are NOT used — they produce false positives on
      ``10example.com`` / ``1000guns.com`` and URL path fragments.

    Returns a human-readable reason if blocked, else ``None`` (defer to
    Layer 2 DNS resolution).
    """
    if not host:
        return "empty host"
    # IP literal?
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        reason = _ip_is_blocked(host)
        if reason:
            return f"literal IP {host} ({reason})"
        return None  # public literal IP — allow without DNS round-trip.
    # Hostname — exact or subdomain match against the block list.
    for forbidden in WEBFETCH_BLOCKED_HOSTNAMES:
        if host == forbidden or host.endswith("." + forbidden):
            return f"forbidden hostname {forbidden!r}"
    return None


def make_webfetch_hook() -> Hook:
    """Two-layer SSRF guard (see spike-findings §R9).

    Layer 1 (SW1 rewrite): precise literal check — IP literal categories
    via ``ipaddress``, hostnames via exact-or-subdomain match. No substring
    matches: ``10example.com`` / ``/v10.0/`` no longer trigger false deny.
    Layer 2: DNS → ``ipaddress`` category check — catches public hostname →
    private IP (the authoritative guard).

    B10 fix: ``getaddrinfo`` runs in ``asyncio.to_thread`` so blocking DNS
    (up to ~5s per Darwin default) does NOT stall the event loop. Exception
    clause widened to ``(gaierror, OSError, timeout)``.

    Residual TOCTOU DNS-rebinding risk → see ``unverified-assumptions.md §U9``.
    """

    async def webfetch_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        data = cast(dict[str, Any], input_data)
        ti = data.get("tool_input", {}) or {}
        url = (ti.get("url") or "").strip()
        if not url:
            return _allow()
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return _deny(f"malformed URL: {url!r}")
        if not host:
            return _deny("WebFetch with empty host is not allowed.")
        # Layer 1: precise literal check (IP categories + hostname block list).
        layer1 = _layer1_block(host)
        if layer1 is not None:
            log.warning(
                "pretool_decision",
                tool_name="WebFetch",
                decision="deny",
                subreason="literal_blocked",
                url=url[:200],
                layer1_reason=layer1,
            )
            return _deny(f"WebFetch to private/local host is blocked: {layer1}.")
        # Layer 2: DNS (off the event loop) + ipaddress category check.
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
        except (socket.gaierror, OSError, TimeoutError):
            # NXDOMAIN / transient / timeout → allow; the CLI will fail the fetch.
            return _allow()
        for _, _, _, _, sockaddr in infos:
            # IPv4 sockaddr is (host, port); IPv6 is (host, port, flowinfo, scopeid).
            # Always string-cast the host field.
            ip_str = str(sockaddr[0])
            reason = _ip_is_blocked(ip_str)
            if reason:
                log.warning(
                    "pretool_decision",
                    tool_name="WebFetch",
                    decision="deny",
                    subreason=f"dns_{reason}",
                    url=url[:200],
                    resolved_ip=ip_str,
                )
                return _deny(f"WebFetch resolved to blocked IP {ip_str} ({reason}).")
        return _allow()

    return webfetch_hook


# ---------------------------------------------------------------------------
# PostToolUse factory — sentinel touch on Write/Edit into skills/tools
# ---------------------------------------------------------------------------
def _is_inside_skills_or_tools(raw_path: str, project_root: Path) -> bool:
    """True iff ``raw_path`` resolves under ``skills/`` or ``tools/``.

    RS-3 (re-verified 2026-04-21): ``HookMatcher.matcher`` is a regex on
    ``tool_name``. There is no per-file-path matcher API, so file-path
    filtering MUST happen inside the hook body.
    """
    if not raw_path:
        return False
    # Reject obvious traversal signals BEFORE filesystem resolution so
    # that a synthetic test input with ``..`` cannot accidentally resolve
    # to a path that happens to be inside project_root.
    if ".." in Path(raw_path).parts:
        return False
    try:
        p = Path(raw_path).expanduser()
        resolved = p.resolve() if p.is_absolute() else (project_root / p).resolve()
    except (OSError, ValueError):
        return False
    root = project_root.resolve()
    for sub in ("skills", "tools"):
        try:
            if resolved.is_relative_to(root / sub):
                return True
        except ValueError:
            continue
    return False


def make_posttool_hooks(project_root: Path, data_dir: Path) -> list[HookMatcher]:
    """Build PostToolUse matchers for Write + Edit → sentinel touch.

    On any Write/Edit whose ``file_path`` resolves under
    ``<project_root>/skills/`` or ``<project_root>/tools/``, touch
    ``<data_dir>/run/skills.dirty`` so that the next
    ``_render_system_prompt`` detects the change and rebuilds the
    manifest (hot-reload path).
    """
    sentinel = data_dir / "run" / "skills.dirty"

    async def on_write_edit(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        ti = raw.get("tool_input") or {}
        file_path = str(ti.get("file_path") or "")
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
                log.warning(
                    "posttool_sentinel_touch_failed",
                    error=repr(exc),
                )
        return cast(HookJSONOutput, {})

    # Phase 4: audit every ``mcp__memory__*`` tool invocation as a JSONL
    # line so owner has a tamper-evident trail of what the model
    # read/wrote/deleted in the vault. No rotation (Q-R4 deferred to
    # phase 9); single-user traffic keeps disk pressure negligible.
    audit_path = data_dir / "memory-audit.log"

    async def on_memory_tool(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del ctx
        raw = cast(dict[str, Any], input_data)
        tool_name = raw.get("tool_name") or ""
        tool_input = raw.get("tool_input") or {}
        tool_response = raw.get("tool_response") or {}
        # Fix 1 / C4-W3: truncate every string value in ``tool_input``
        # to 2048 chars BEFORE ``json.dumps``. The model-controlled
        # ``body`` of ``memory_write`` can be up to ``max_body_bytes``
        # (1 MiB default); ``query`` / ``path`` / etc. are not
        # size-capped until downstream validators reject them, which
        # is after the audit hook has already persisted the raw bytes.
        # Without this cap the audit log is the biggest file on disk.
        tool_input_compact = _truncate_strings(tool_input, max_len=2048)
        # Compact the response so audit log stays small — only keep
        # is_error flag and a body-length signal rather than full
        # snippet text (which can be multi-KB and contain attacker-
        # controlled bytes).
        resp_meta: dict[str, Any] = {}
        if isinstance(tool_response, dict):
            resp_meta["is_error"] = bool(tool_response.get("is_error"))
            content = tool_response.get("content") or []
            if isinstance(content, list) and content:
                first = content[0] if isinstance(content[0], dict) else {}
                text = first.get("text") if isinstance(first, dict) else None
                resp_meta["content_len"] = len(text) if isinstance(text, str) else 0
        entry = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "tool_input": tool_input_compact,
            "response": resp_meta,
        }
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            # Fix 2 / H2: create the file restricted to owner only.
            # ``os.chmod`` is a no-op if the file already exists with
            # 0o600; calling it unconditionally is safe and cheap.
            new_file = not audit_path.exists()
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if new_file:
                try:
                    os.chmod(audit_path, 0o600)
                except OSError as exc:
                    log.warning("memory_audit_chmod_failed", error=repr(exc))
        except OSError as exc:
            log.warning("memory_audit_write_failed", error=repr(exc))
        return cast(HookJSONOutput, {})

    # Phase 5: parallel audit hook for scheduler tools. Same JSONL
    # shape as memory audit (content length only — we don't want
    # 2-KB prompts clogging the audit log on every tick). Distinct
    # file lets the owner keep operational events separate from
    # memory reads.
    scheduler_audit_path = data_dir / "scheduler-audit.log"

    async def on_scheduler_tool(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del ctx
        raw = cast(dict[str, Any], input_data)
        tool_name = raw.get("tool_name") or ""
        tool_input = raw.get("tool_input") or {}
        tool_response = raw.get("tool_response") or {}
        tool_input_compact = _truncate_strings(tool_input, max_len=2048)
        resp_meta: dict[str, Any] = {}
        if isinstance(tool_response, dict):
            resp_meta["is_error"] = bool(tool_response.get("is_error"))
            content = tool_response.get("content") or []
            if isinstance(content, list) and content:
                first = content[0] if isinstance(content[0], dict) else {}
                text = first.get("text") if isinstance(first, dict) else None
                resp_meta["content_len"] = (
                    len(text) if isinstance(text, str) else 0
                )
        entry = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "tool_input": tool_input_compact,
            "response": resp_meta,
        }
        try:
            scheduler_audit_path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not scheduler_audit_path.exists()
            with scheduler_audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if new_file:
                try:
                    os.chmod(scheduler_audit_path, 0o600)
                except OSError as exc:
                    log.warning(
                        "scheduler_audit_chmod_failed", error=repr(exc)
                    )
        except OSError as exc:
            log.warning("scheduler_audit_write_failed", error=repr(exc))
        return cast(HookJSONOutput, {})

    return [
        HookMatcher(matcher="Write", hooks=[on_write_edit]),
        HookMatcher(matcher="Edit", hooks=[on_write_edit]),
        HookMatcher(matcher=r"mcp__memory__.*", hooks=[on_memory_tool]),
        HookMatcher(
            matcher=r"mcp__scheduler__.*", hooks=[on_scheduler_tool]
        ),
    ]
