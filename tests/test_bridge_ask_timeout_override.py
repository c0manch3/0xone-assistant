"""Phase 6c — bridge.ask timeout_override kwarg plumbing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from assistant.bridge import claude as claude_mod
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


class _SlowQuery:
    """Async-iterator that sleeps for ``delay`` seconds before yielding."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        delay = self._delay

        async def _gen() -> AsyncIterator[Any]:
            # Drain prompt envelopes so the bridge's prompt_stream resolves.
            async for _ in prompt:
                pass
            await asyncio.sleep(delay)
            from claude_agent_sdk import ResultMessage

            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.0,
                usage={},
                stop_reason="end_turn",
            )

        return _gen()


async def test_timeout_override_extends_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0.4s sleep would breach the 0.2s default; with override=2 it
    completes."""
    settings = _settings(tmp_path)
    settings.claude.timeout = 0  # would normally fail any wait

    bridge = ClaudeBridge(settings)
    monkeypatch.setattr(claude_mod, "_safe_query", _SlowQuery(delay=0.05))
    monkeypatch.setattr(
        ClaudeBridge,
        "_render_system_prompt",
        lambda self: "stub",
    )

    seen_result = False
    async for item in bridge.ask(
        chat_id=1,
        user_text="x",
        history=[],
        timeout_override=5,
    ):
        # Just confirm the iteration completes without TimeoutError.
        from claude_agent_sdk import ResultMessage

        if isinstance(item, ResultMessage):
            seen_result = True
    assert seen_result, "ResultMessage should reach caller under override"


async def test_no_override_uses_default_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without override the default settings.claude.timeout is enforced."""
    settings = _settings(tmp_path)
    settings.claude.timeout = 1  # 1 second default

    bridge = ClaudeBridge(settings)
    # 5-second sleep > 1-second default → timeout
    monkeypatch.setattr(claude_mod, "_safe_query", _SlowQuery(delay=5.0))
    monkeypatch.setattr(
        ClaudeBridge,
        "_render_system_prompt",
        lambda self: "stub",
    )

    from assistant.bridge.claude import ClaudeBridgeError

    with pytest.raises(ClaudeBridgeError):
        async for _ in bridge.ask(
            chat_id=1,
            user_text="x",
            history=[],
        ):
            pass
