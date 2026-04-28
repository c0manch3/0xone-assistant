from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, Literal, cast

from claude_agent_sdk import (
    AgentDefinition,
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
    make_posttool_hooks,
    make_webfetch_hook,
)
from assistant.bridge.skills import (
    build_manifest,
    invalidate_manifest_cache,
    touch_skills_dir,
)
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.tools_sdk.installer import (
    INSTALLER_SERVER,
    INSTALLER_TOOL_NAMES,
)
from assistant.tools_sdk.memory import MEMORY_SERVER, MEMORY_TOOL_NAMES
from assistant.tools_sdk.scheduler import (
    SCHEDULER_SERVER,
    SCHEDULER_TOOL_NAMES,
)
from assistant.tools_sdk.subagent import (
    SUBAGENT_SERVER,
    SUBAGENT_TOOL_NAMES,
)

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

    def __init__(
        self,
        settings: Settings,
        *,
        extra_hooks: dict[str, list[HookMatcher]] | None = None,
        agents: dict[str, AgentDefinition] | None = None,
    ) -> None:
        """Phase 6 (research RQ1 + RQ2): ``extra_hooks`` is a dict
        keyed by SDK hook event name (``"SubagentStart"``,
        ``"SubagentStop"``, ``"PreToolUse"``, ...) merged into the
        bridge's own hook registry. ``"PreToolUse"`` matchers are
        UNIONED with the existing phase-3 sandbox; other keys overwrite
        any prior entry.

        ``agents`` is the per-kind :class:`AgentDefinition` registry
        from :func:`build_agents`. When non-None, ``"Task"`` is added
        to ``allowed_tools`` so the model can delegate; otherwise the
        Task tool is hidden (avoids a "no targets" model confusion).
        """
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)
        self._extra_hooks = extra_hooks or {}
        self._agents = agents

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
        dd = self._settings.data_dir
        base_pretool: list[HookMatcher] = [
            HookMatcher(matcher="Bash", hooks=[make_bash_hook(pr)]),
            *[
                HookMatcher(matcher=t, hooks=[make_file_hook(pr)])
                for t in FILE_TOOL_NAMES
            ],
            HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
        ]
        hooks: dict[HookEventName, list[HookMatcher]] = {
            "PreToolUse": base_pretool,
            "PostToolUse": make_posttool_hooks(pr, dd),
        }
        # Phase 6 / research RQ1: merge subagent / future hooks.
        # PreToolUse is UNIONED so the cancel-flag-poll layers on top of
        # the phase-3 sandbox; other event keys are extended with
        # setdefault so multiple producers can co-exist.
        for event, matchers in self._extra_hooks.items():
            if not matchers:
                continue
            if event == "PreToolUse":
                hooks["PreToolUse"] = list(hooks["PreToolUse"]) + list(matchers)
            else:
                key = cast(HookEventName, event)
                existing = hooks.get(key, [])
                hooks[key] = list(existing) + list(matchers)
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
        # Phase 6: subagent surface — @tool always advertised; native
        # ``Task`` tool added ONLY when an AgentDefinition registry is
        # passed (research RQ1, pitfall #6).
        allowed_tools: list[str] = [
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
            "Skill",
            *INSTALLER_TOOL_NAMES,
            *MEMORY_TOOL_NAMES,
            *SCHEDULER_TOOL_NAMES,
            *SUBAGENT_TOOL_NAMES,
        ]
        if self._agents:
            allowed_tools.append("Task")
        mcp_servers = {
            "installer": INSTALLER_SERVER,
            "memory": MEMORY_SERVER,
            "scheduler": SCHEDULER_SERVER,
            "subagent": SUBAGENT_SERVER,
        }
        opts_kwargs: dict[str, Any] = {
            "cwd": str(pr),
            "setting_sources": ["project"],
            "max_turns": self._settings.claude.max_turns,
            "allowed_tools": allowed_tools,
            "mcp_servers": mcp_servers,
            "hooks": hooks,
            "system_prompt": system_prompt_preset,
            **thinking_kwargs,
        }
        if self._agents:
            opts_kwargs["agents"] = self._agents
        return ClaudeAgentOptions(**opts_kwargs)

    def _render_system_prompt(self) -> str:
        self._check_skills_sentinel()
        template = (
            self._settings.project_root / "src" / "assistant" / "bridge" / "system_prompt.md"
        ).read_text(encoding="utf-8")
        manifest = build_manifest(self._settings.project_root / "skills")
        return template.format(
            project_root=str(self._settings.project_root),
            skills_manifest=manifest,
        )

    def _check_skills_sentinel(self) -> None:
        """Hot-reload path: if ``data/run/skills.dirty`` exists, drop the
        manifest cache + bump the skills-dir mtime so the next call to
        ``build_manifest`` sees the freshly installed/removed skills.

        Two concurrent turns both observing the sentinel both invoke
        ``invalidate_manifest_cache`` (``dict.clear`` — idempotent) and
        ``touch_skills_dir`` (``os.utime`` — idempotent); one wins the
        ``unlink`` race, the other catches ``FileNotFoundError``.
        """
        sentinel = self._settings.data_dir / "run" / "skills.dirty"
        if not sentinel.exists():
            return
        invalidate_manifest_cache()
        touch_skills_dir(self._settings.project_root / "skills")
        with contextlib.suppress(FileNotFoundError):
            sentinel.unlink()
        log.info("skills_cache_invalidated_via_sentinel")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
        timeout_override: int | None = None,
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

        Phase 5 / H-7: ``system_notes`` are ephemeral directives the caller
        (ClaudeHandler) wants the model to see alongside the user turn
        without persisting them into ``conversations``. We concatenate them
        as ``[system-note: ...]`` text blocks appended to ``user_text`` in
        the live envelope.

        Phase 6b: when ``image_blocks`` is non-None, the live envelope
        switches to the streaming-input ``list[dict]`` content shape —
        image blocks first (per Anthropic perf guidance "images come
        before text"), followed by a single text block carrying
        ``user_text`` + system-note suffix. The ``list[dict]`` form is
        VERIFIED for image content via the RQ0 spike at
        ``plan/phase6b/spikes/rq0_multimodal/probe.py`` (PASS
        2026-04-27). Non-vision callers retain the plain-string path.

        Phase 6c (C3 closure): ``timeout_override`` lets the audio
        handler raise the ``asyncio.timeout`` ceiling above the default
        ``settings.claude.timeout`` (300s) for voice/url turns whose
        auto-summary may run 5-15 minutes on a 1-hour transcript. When
        ``None`` the default is used; non-voice paths must NOT pass it.
        The semaphore (``settings.claude.max_concurrent``) is unchanged
        — voice turns simply hold the slot longer.
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        if system_notes:
            joined = "\n\n".join(f"[system-note: {n}]" for n in system_notes)
            user_text_for_envelope = f"{user_text}\n\n{joined}"
        else:
            user_text_for_envelope = user_text
        log.info(
            "query_start",
            chat_id=chat_id,
            prompt_len=len(user_text),
            envelope_len=len(user_text_for_envelope),
            history_rows=len(history),
            system_notes=len(system_notes or []),
            image_blocks=len(image_blocks or []),
        )

        async def prompt_stream() -> AsyncIterable[dict[str, Any]]:
            for envelope in history_to_sdk_envelopes(history, chat_id):
                yield envelope
            content: str | list[dict[str, Any]]
            if image_blocks:
                # Image blocks BEFORE text per Anthropic perf guidance;
                # verified by RQ0 spike (PASS 2026-04-27).
                content = [
                    *image_blocks,
                    {"type": "text", "text": user_text_for_envelope},
                ]
            else:
                content = user_text_for_envelope
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
                # R10: cosmetic — SDK assigns its own UUID.
                "session_id": f"chat-{chat_id}",
            }

        last_model: str | None = None
        timeout_s = (
            timeout_override
            if timeout_override is not None
            else self._settings.claude.timeout
        )
        if (
            timeout_override is not None
            and timeout_override != self._settings.claude.timeout
        ):
            log.info(
                "claude_ask_timeout_override",
                chat_id=chat_id,
                timeout_s=timeout_s,
                default_timeout_s=self._settings.claude.timeout,
            )
        async with self._sem:
            try:
                async with asyncio.timeout(timeout_s):
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
                    timeout_s=timeout_s,
                )
                raise ClaudeBridgeError("timeout") from exc
            except Exception as exc:
                log.error("sdk_error", error=repr(exc))
                raise ClaudeBridgeError(f"sdk error: {exc}") from exc
