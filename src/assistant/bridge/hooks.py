from __future__ import annotations

import asyncio
import ipaddress
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


def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    """Return a deny-reason iff ``cmd`` is NOT allowed. ``None`` → allow.

    BW2: exact matches checked separately so ``ls``/``pwd`` don't re-admit
    ``lsof``/``pwdx``/``lsblk`` via naive ``startswith``.
    """
    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if stripped in BASH_ALLOWLIST_EXACT:
        return None
    if any(stripped.startswith(p) for p in BASH_ALLOWLIST_PREFIXES):
        return None
    # Special-case `cat <path>...` — allow iff ALL args resolve inside project_root.
    if stripped.startswith("cat "):
        args = stripped[4:].strip().split()
        ok, reason = _cat_targets_ok(args, project_root)
        if ok:
            return None
        return reason or "cat target outside project_root"
    return (
        "Bash command not in allowlist. If you need this operation, ask the "
        "owner to add it to tools/<name>/main.py or expand the allowlist."
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
