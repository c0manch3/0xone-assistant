from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk import (
    query as _raw_query,
)
from claude_agent_sdk.types import SystemPromptPreset

from assistant.bridge.history import history_to_sdk_envelopes
from assistant.bridge.hooks import (
    FILE_TOOL_NAMES,
    make_bash_hook,
    make_file_hook,
    make_webfetch_hook,
)
from assistant.bridge.skills import build_manifest
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("bridge.claude")


async def _safe_query(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    """Wrapper around ``claude_agent_sdk.query`` that survives unknown message
    types.

    Fix D (incident S13): future SDK / CLI versions may emit new message
    types (e.g. ``rate_limit_event`` in newer Claude Code CLI builds) that
    our parser does not know. The raw SDK raises ``Unknown message type``
    and aborts the generator. We log and gracefully end the stream —
    future-proofing against SDK / CLI minor-version bumps.

    Any other exception propagates normally so the bridge's existing
    ``except Exception`` branch still converts it to ``ClaudeBridgeError``.
    """
    try:
        async for message in _raw_query(*args, **kwargs):
            yield message
    except Exception as exc:
        if "Unknown message type" in str(exc):
            log.warning("sdk_unknown_message_type", error=str(exc))
            return  # graceful end-of-stream, don't crash
        raise

# Keyed against the SDK's full hook-event Literal union so
# ``ClaudeAgentOptions.hooks`` accepts our dict. We populate only the
# ``PreToolUse`` key for phase 2.
type HookEventName = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "PreCompact",
    "Notification",
    "SubagentStart",
    "PermissionRequest",
]


class ClaudeBridgeError(RuntimeError):
    """Raised for any failure of the underlying SDK call.

    ``ask`` is the only site that raises this; callers pattern-match on it
    to distinguish "bridge-level" failures from their own logic errors.
    """


class ClaudeBridge:
    """Thin facade over ``claude_agent_sdk.query``.

    Owns the concurrency semaphore and yields Block instances followed by
    the terminal ``ResultMessage``. Contract with the handler:

      - Only ``ResultMessage`` signals success. Any other termination path
        (timeout, exception, caller break) leaves the turn uncompleted —
        the handler must call ``interrupt_turn`` on its ``finally`` path.
      - Model-level metadata that the caller wants to log
        (``sdk_session_id``, ``stop_reason``, …) rides on the
        ``ResultMessage``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    # ------------------------------------------------------------------
    # Options assembly
    # ------------------------------------------------------------------
    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        """Assemble ``ClaudeAgentOptions`` for a single query.

        The ``system_prompt`` argument is APPENDED to the ``claude_code``
        preset rather than replacing it. Passing a raw string for
        ``ClaudeAgentOptions.system_prompt`` would discard the default
        preset — including the built-in directive that tells the model
        to follow the body of an auto-injected ``Skill`` invocation —
        which is why phase 2 skills failed to execute end-to-end.
        Per the SDK docs for ``SystemPromptPreset``, using
        ``{"type": "preset", "preset": "claude_code", "append": ...}``
        keeps default tools + safety rules intact and layers our
        project-specific instructions on top.
        """
        pr = self._settings.project_root
        hooks: dict[HookEventName, list[HookMatcher]] = {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[make_bash_hook(pr)]),
                *[HookMatcher(matcher=t, hooks=[make_file_hook(pr)]) for t in FILE_TOOL_NAMES],
                HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
            ]
        }
        thinking_kwargs: dict[str, Any] = {}
        if self._settings.claude.thinking_budget > 0:
            thinking_kwargs["max_thinking_tokens"] = self._settings.claude.thinking_budget
            thinking_kwargs["effort"] = self._settings.claude.effort
        system_prompt_preset: SystemPromptPreset = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
            "exclude_dynamic_sections": True,  # stable system prompt → stable cache
        }
        return ClaudeAgentOptions(
            cwd=str(pr),
            setting_sources=["project"],
            max_turns=self._settings.claude.max_turns,
            allowed_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebFetch",
                "Skill",
            ],
            hooks=hooks,
            system_prompt=system_prompt_preset,
            **thinking_kwargs,
        )

    def _render_system_prompt(self) -> str:
        template = (
            self._settings.project_root / "src" / "assistant" / "bridge" / "system_prompt.md"
        ).read_text(encoding="utf-8")
        manifest = build_manifest(self._settings.project_root / "skills")
        return template.format(
            project_root=str(self._settings.project_root),
            skills_manifest=manifest,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Stream blocks + the terminal ``ResultMessage`` for one turn.

        R10 note: ``session_id=f"chat-{chat_id}"`` in envelopes is cosmetic;
        the SDK/CLI reassigns its own UUID per query. No collision possible
        for concurrent ``chat_id``.

        R13 note: assistant envelopes ARE honored by the SDK —
        ``history_to_sdk_envelopes`` replays both user AND assistant turns
        verbatim.

        S2 note: ``model`` is captured from ``AssistantMessage.model`` — it
        does NOT exist on ``ResultMessage`` in SDK 0.1.59's types.py. We log
        the last seen model alongside the ResultMessage line.
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        log.info(
            "query_start",
            chat_id=chat_id,
            prompt_len=len(user_text),
            history_rows=len(history),
        )

        async def prompt_stream() -> AsyncIterable[dict[str, Any]]:
            for envelope in history_to_sdk_envelopes(history, chat_id):
                yield envelope
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                # R10: cosmetic — SDK assigns its own UUID.
                "session_id": f"chat-{chat_id}",
            }

        last_model: str | None = None
        async with self._sem:
            try:
                async with asyncio.timeout(self._settings.claude.timeout):
                    async for message in _safe_query(prompt=prompt_stream(), options=opts):
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            log.info(
                                "sdk_init",
                                model=message.data.get("model"),
                                skills=list(message.data.get("skills") or []),
                                cwd=message.data.get("cwd"),
                            )
                            continue
                        if isinstance(message, AssistantMessage):
                            # S2: capture model here — ResultMessage has no .model.
                            last_model = getattr(message, "model", None) or last_model
                            for block in message.content:
                                log.debug("block_received", type=type(block).__name__)
                                yield block
                            continue
                        if isinstance(message, ResultMessage):
                            usage = message.usage or {}
                            log.info(
                                "result_received",
                                model=last_model,
                                stop_reason=getattr(message, "stop_reason", None),
                                cost_usd=message.total_cost_usd,
                                duration_ms=getattr(message, "duration_ms", None),
                                num_turns=getattr(message, "num_turns", None),
                                input_tokens=usage.get("input_tokens"),
                                output_tokens=usage.get("output_tokens"),
                                cache_read=usage.get("cache_read_input_tokens"),
                                cache_creation=usage.get("cache_creation_input_tokens"),
                                sdk_session_id=getattr(message, "session_id", None),
                            )
                            yield message
                            # Fix A (incident S13): do NOT return here.
                            # SDK streaming-input with multi-envelope history
                            # (or tool_use iterations) can run multiple API
                            # iterations inside a single ``query()`` call. Each
                            # iteration ends with its own ``ResultMessage``;
                            # subsequent iterations' tool_use / text blocks
                            # would be silently dropped if we returned. We
                            # keep consuming until the SDK closes the stream
                            # normally (``async for`` exits).
                            continue
                        # Other message types (RateLimitEvent, UserMessage
                        # echoes, unknown SystemMessage subtypes) are skipped.
            except TimeoutError as exc:
                log.warning(
                    "timeout",
                    chat_id=chat_id,
                    timeout_s=self._settings.claude.timeout,
                )
                raise ClaudeBridgeError("timeout") from exc
            except Exception as exc:
                log.error("sdk_error", error=repr(exc))
                raise ClaudeBridgeError(f"sdk error: {exc}") from exc
