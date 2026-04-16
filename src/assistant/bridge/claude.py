from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

from assistant.bridge.history import history_to_user_envelopes
from assistant.bridge.hooks import make_pretool_hooks
from assistant.bridge.skills import build_manifest
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("bridge.claude")


class ClaudeBridgeError(RuntimeError):
    """Raised when the SDK call times out or fails irrecoverably."""


# Sentinel emitted before any block when the SDK announces the model name in
# its `init` SystemMessage. Handler unpacks `payload["model"]` into
# `turns.meta_json` so phase-8 health/admin can attribute cost per model.
class InitMeta:
    """Lightweight carrier for `SystemMessage(subtype='init').data`.

    Yielded as the very first item of `ClaudeBridge.ask`. Handler matches it
    via isinstance and folds `model` into the turn meta. Not persisted as a
    `conversations` row.
    """

    __slots__ = ("cwd", "model", "session_id", "skills")

    def __init__(
        self,
        *,
        model: str | None,
        skills: list[str],
        cwd: str | None,
        session_id: str | None,
    ) -> None:
        self.model = model
        self.skills = skills
        self.cwd = cwd
        self.session_id = session_id


class ClaudeBridge:
    """Streams Blocks (+ a final ResultMessage) for a single model turn.

    Auth is OAuth via the user's `claude` CLI -- no API key. The bridge is
    stateless per call; persistence is the handler's responsibility.

    Lifecycle invariants:
    * The very first yielded item is an `InitMeta` (carries `model` etc.).
    * On success, the LAST yielded item is `ResultMessage`, then the
      generator returns cleanly.
    * On timeout / SDK error, the underlying async generator is `aclose()`d
      in `finally` so we don't leak a CLI subprocess; we then raise
      `ClaudeBridgeError`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    # ------------------------------------------------------------------

    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        pr = self._settings.project_root
        hooks: dict[Any, Any] = {"PreToolUse": make_pretool_hooks(pr)}
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
        """Yield InitMeta, then Blocks, then one final ResultMessage.

        Handler contract: ResultMessage is the success sentinel.
        If the stream aborts (TimeoutError / SDK exception) we raise
        `ClaudeBridgeError` and NEVER yield a ResultMessage; the handler's
        `finally` calls `turns.interrupt`. The underlying SDK async-gen is
        always closed in `finally`, even on timeout, so the CLI subprocess
        is reaped.
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
            sdk_iter = query(prompt=prompt_stream(), options=opts)
            try:
                async with asyncio.timeout(self._settings.claude.timeout):
                    async for message in sdk_iter:
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            data = message.data or {}
                            init = InitMeta(
                                model=data.get("model"),
                                skills=list(data.get("skills") or []),
                                cwd=data.get("cwd"),
                                session_id=data.get("session_id"),
                            )
                            log.info(
                                "sdk_init",
                                model=init.model,
                                skills=init.skills,
                                cwd=init.cwd,
                            )
                            yield init
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
                        # SystemMessage(other), RateLimitEvent, UserMessage -- skip.
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
            finally:
                # Always close the SDK async-gen so the CLI subprocess does not
                # become a zombie on timeout/exception/early return.
                aclose = getattr(sdk_iter, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception as close_exc:
                        log.warning("sdk_aclose_failed", error=repr(close_exc))
