from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,
    UserMessage,
    query,
)

from assistant.bridge.history import history_to_user_envelopes
from assistant.bridge.hooks import make_posttool_hooks, make_pretool_hooks
from assistant.bridge.skills import (
    build_manifest,
    invalidate_manifest_cache,
    touch_skills_dir,
)
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
        dd = self._settings.data_dir
        hooks: dict[Any, Any] = {
            "PreToolUse": make_pretool_hooks(pr),
            "PostToolUse": make_posttool_hooks(pr, dd),
        }
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

    def _check_skills_sentinel(self) -> None:
        """Drop + rebuild the manifest cache when the PostToolUse sentinel is set.

        The sentinel is touched by `bridge/hooks.make_posttool_sentinel_hook`
        whenever Write/Edit lands under `skills/` or `tools/`. Reading it
        here (before every turn's system-prompt render) closes the
        feedback loop — the same turn that installed a new skill won't
        see it, but the very next one will.

        Race (S-8 formal invariants): two concurrent chats entering this
        method see `exists() is True`, both invalidate + touch (both
        idempotent), one wins `unlink`, the other sees FileNotFoundError
        which we swallow.
        """
        sentinel = self._settings.data_dir / "run" / "skills.dirty"
        if not sentinel.exists():
            return
        invalidate_manifest_cache()
        touch_skills_dir(self._settings.project_root / "skills")
        # Cross-chat race with another turn also clearing the sentinel:
        # benign — both observe the dirty state, both invalidate (idempotent),
        # and whichever loses the unlink gets `FileNotFoundError` here.
        with contextlib.suppress(FileNotFoundError):
            sentinel.unlink()
        log.info("skills_cache_invalidated_via_sentinel")

    def _render_system_prompt(self) -> str:
        self._check_skills_sentinel()
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
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        """Yield InitMeta, then Blocks, then one final ResultMessage.

        Handler contract: ResultMessage is the success sentinel.
        If the stream aborts (TimeoutError / SDK exception) we raise
        `ClaudeBridgeError` and NEVER yield a ResultMessage; the handler's
        `finally` calls `turns.interrupt`. The underlying SDK async-gen is
        always closed in `finally`, even on timeout, so the CLI subprocess
        is reaped.

        `system_notes` is an optional list of ephemeral hints to attach to
        the *current* user envelope's `content` as extra `{type: text}`
        blocks. The notes never touch `ConversationStore` — the handler
        writes the raw `user_text` unchanged, so history stays honest.
        Phase 3 uses this for the URL-detector nudge (S-4).
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        log.info(
            "query_start",
            chat_id=chat_id,
            prompt_len=len(user_text),
            history_rows=len(history),
            system_notes=len(system_notes or []),
        )

        async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
            for envelope in history_to_user_envelopes(history, chat_id):
                yield envelope
            if system_notes:
                # Mixed-block content: original text + one text-block per note.
                content_blocks: list[dict[str, str]] = [
                    {"type": "text", "text": user_text},
                ]
                for note in system_notes:
                    content_blocks.append({"type": "text", "text": f"[system-note: {note}]"})
                user_content: str | list[dict[str, str]] = content_blocks
            else:
                user_content = user_text
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_content},
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
                                log.debug(
                                    "block_received",
                                    type=type(block).__name__,
                                    enclosing="AssistantMessage",
                                )
                                yield block
                            continue
                        if isinstance(message, UserMessage):
                            # Phase 4 (spike S-B.2 fix): surface ToolResultBlocks
                            # so ConversationStore persists them. Phase 2/3 silently
                            # dropped every tool_result because this branch was a
                            # no-op; without it synthetic history-summary (Q1) has
                            # nothing to summarise. Plain-str content (SDK echo of
                            # our own envelope) is skipped — already persisted as
                            # the original user row by the handler.
                            content = message.content
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, ToolResultBlock):
                                        log.debug(
                                            "block_received",
                                            type=type(block).__name__,
                                            enclosing="UserMessage",
                                        )
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
                        # SystemMessage(other), RateLimitEvent -- skip.
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
