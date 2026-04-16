from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    query,
)

from assistant.bridge.history import history_to_user_envelopes
from assistant.bridge.skills import build_manifest
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("bridge.claude")

# -----------------------------------------------------------------------------
# Bash prefilter (allowlist-first, slip-guard regex as defence-in-depth).
# -----------------------------------------------------------------------------
_BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "uv run tools/",
    "git status",
    "git log",
    "git diff",
    "ls ",
    "ls\n",
    "pwd",
    "echo ",
)

_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}"
    r")",
    re.IGNORECASE,
)

_FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# WebFetch SSRF — defence in depth. Real ACLs belong at the OS/firewall layer.
_WEBFETCH_BLOCKED_NEEDLES: tuple[str, ...] = (
    "localhost",
    "127.",
    "0.0.0.0",
    "169.254.",
    "10.",
    "192.168.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "[::1]",
    "[fc",
    "[fd",
)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    """Return a deny-reason iff `cmd` is NOT allowed. `None` → allow."""
    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if any(stripped.startswith(p) for p in _BASH_ALLOWLIST_PREFIXES):
        return None
    # Special-case `cat <path>` — allow iff path resolves inside project_root.
    if stripped.startswith("cat "):
        rest = stripped[4:].strip()
        # Reject flags (`cat -A`, etc.) — too open.
        first = rest.split()[0] if rest else ""
        if first and not first.startswith("-"):
            try:
                target = Path(first)
                resolved = (
                    target.resolve() if target.is_absolute() else (project_root / target).resolve()
                )
                root = project_root.resolve()
                if str(resolved).startswith(str(root) + "/") or resolved == root:
                    return None
            except OSError:
                pass
    return (
        "not in allowlist; if you genuinely need this, ask the owner to add it "
        "to a skill's allowed-tools with explicit approval."
    )


def _make_bash_hook(project_root: Path) -> Any:
    async def bash_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        tool_input = input_data.get("tool_input") or {}
        cmd = str(tool_input.get("command", "") or "")
        reason = _bash_allowlist_check(cmd, project_root)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                reason="allowlist",
                cmd=cmd[:200],
            )
            return _deny(reason)
        if _BASH_SLIP_GUARD_RE.search(cmd):
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                reason="slip_guard",
                cmd=cmd[:200],
            )
            return _deny(
                "Command matched a secrets/encoded-payload pattern. "
                "Reading .env/.ssh/.aws/tokens/encoded blobs via Bash is blocked."
            )
        log.debug("pretool_decision", tool_name="Bash", decision="allow", cmd=cmd[:120])
        return {}

    return bash_hook


def _make_file_hook(project_root: Path) -> Any:
    root = project_root.resolve()

    async def file_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        tool_input = input_data.get("tool_input") or {}
        candidate = (
            tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern") or ""
        )
        if not candidate:
            return {}
        try:
            p = Path(str(candidate))
            if p.is_absolute():
                resolved = p.resolve()
                if not (str(resolved).startswith(str(root) + "/") or resolved == root):
                    log.warning(
                        "pretool_decision",
                        tool_name=input_data.get("tool_name"),
                        decision="deny",
                        reason="outside_project_root",
                        path=str(resolved),
                    )
                    return _deny(f"Path outside project_root ({root}) is not allowed: {resolved}")
        except OSError as exc:
            return _deny(f"invalid path {candidate!r}: {exc}")
        return {}

    return file_hook


def _make_webfetch_hook() -> Any:
    async def webfetch_hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        ctx: Any,
    ) -> dict[str, Any]:
        tool_input = input_data.get("tool_input") or {}
        url = str(tool_input.get("url", "") or "").strip()
        if not url:
            return {}
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return _deny(f"malformed URL: {url!r}")
        raw = url.lower()
        for needle in _WEBFETCH_BLOCKED_NEEDLES:
            bare = needle.rstrip(".").rstrip("]")
            if host.startswith(bare) or needle in raw:
                log.warning(
                    "pretool_decision",
                    tool_name="WebFetch",
                    decision="deny",
                    reason="ssrf_private_ip",
                    url=url[:200],
                )
                return _deny(f"WebFetch to private/link-local/metadata host is blocked: {host!r}.")
        return {}

    return webfetch_hook


class ClaudeBridgeError(RuntimeError):
    """Raised when the SDK call times out or fails irrecoverably."""


class ClaudeBridge:
    """Streams Blocks (+ final ResultMessage) for a single model turn.

    Auth is OAuth via the user's `claude` CLI — no API key. The bridge is
    stateless per call; persistence is the handler's responsibility.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    # ------------------------------------------------------------------

    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        pr = self._settings.project_root
        pretool_matchers: list[HookMatcher] = [
            HookMatcher(matcher="Bash", hooks=[_make_bash_hook(pr)]),
            *[HookMatcher(matcher=t, hooks=[_make_file_hook(pr)]) for t in _FILE_TOOLS],
            HookMatcher(matcher="WebFetch", hooks=[_make_webfetch_hook()]),
        ]
        hooks: dict[Any, list[HookMatcher]] = {"PreToolUse": pretool_matchers}
        thinking_kwargs: dict[str, Any] = {}
        if self._settings.claude.thinking_budget > 0:
            thinking_kwargs["max_thinking_tokens"] = self._settings.claude.thinking_budget
            thinking_kwargs["effort"] = self._settings.claude.effort
        return ClaudeAgentOptions(
            cwd=str(pr),
            setting_sources=["project"],
            max_turns=self._settings.claude.max_turns,
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"],
            hooks=hooks,
            system_prompt=system_prompt,
            **thinking_kwargs,
        )

    def _render_system_prompt(self) -> str:
        template_path = (
            self._settings.project_root / "src" / "assistant" / "bridge" / "system_prompt.md"
        )
        template = template_path.read_text(encoding="utf-8")
        manifest = build_manifest(self._settings.project_root / "skills")
        log.info("manifest_rebuilt")
        return template.format(
            project_root=str(self._settings.project_root),
            skills_manifest=manifest,
        )

    # ------------------------------------------------------------------

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Yield Blocks, then one final ResultMessage, then return cleanly.

        Handler contract: the ResultMessage is the success sentinel.
        If the stream aborts (TimeoutError / SDK exception) we raise
        ClaudeBridgeError and NEVER yield a ResultMessage — handler's
        `finally` must call `turns.interrupt`.
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        log.info(
            "query_start",
            chat_id=chat_id,
            prompt_len=len(user_text),
            history_rows=len(history),
        )

        async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
            for envelope in history_to_user_envelopes(history, chat_id):
                yield envelope
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": f"chat-{chat_id}",
            }

        async with self._sem:
            try:
                async with asyncio.timeout(self._settings.claude.timeout):
                    async for message in query(prompt=prompt_stream(), options=opts):
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            data = message.data or {}
                            log.info(
                                "sdk_init",
                                model=data.get("model"),
                                skills=list(data.get("skills") or []),
                                cwd=data.get("cwd"),
                            )
                            continue
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                log.debug("block_received", type=type(block).__name__)
                                yield block
                            continue
                        if isinstance(message, ResultMessage):
                            log.info(
                                "result_received",
                                stop_reason=message.stop_reason,
                                cost_usd=message.total_cost_usd,
                                duration_ms=message.duration_ms,
                                num_turns=message.num_turns,
                            )
                            yield message
                            return
                        # SystemMessage(other), RateLimitEvent, UserMessage — ignore.
            except TimeoutError as exc:
                log.warning(
                    "timeout",
                    chat_id=chat_id,
                    timeout_s=self._settings.claude.timeout,
                )
                raise ClaudeBridgeError("timeout") from exc
            except ClaudeBridgeError:
                raise
            except Exception as exc:
                log.error("sdk_error", error=repr(exc))
                raise ClaudeBridgeError(f"sdk error: {exc}") from exc
