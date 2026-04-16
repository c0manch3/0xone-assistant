"""Bridge lifecycle tests: timeout aclose + ResultMessage model propagation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

import assistant.bridge.claude as bridge_module
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError, InitMeta
from assistant.config import ClaudeSettings, Settings


def _make_settings(tmp_path: Path, *, timeout: int = 30) -> Settings:
    return Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(max_concurrent=1, timeout=timeout, max_turns=3, history_limit=20),
    )


def _write_system_prompt(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "src" / "assistant" / "bridge"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "system_prompt.md").write_text(
        "root={project_root}\nskills:\n{skills_manifest}\n", encoding="utf-8"
    )
    (tmp_path / "skills").mkdir(exist_ok=True)


# ---------------------------------------------------------------- timeout aclose


class _HangingGen:
    """Async-gen stand-in that never yields a ResultMessage and tracks closes."""

    def __init__(self, hang_seconds: float) -> None:
        self._hang = hang_seconds
        self.aclose_count = 0

    def __aiter__(self) -> _HangingGen:
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(self._hang)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclose_count += 1


async def test_bridge_closes_sdk_iter_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)
    hanger = _HangingGen(hang_seconds=10.0)

    def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> _HangingGen:
        del prompt, options
        return hanger

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path, timeout=1))
    with pytest.raises(ClaudeBridgeError, match="timeout"):
        async for _ in bridge.ask(chat_id=1, user_text="hi", history=[]):
            pass

    # The SDK async-gen MUST be closed exactly once -- otherwise the CLI
    # subprocess becomes a zombie.
    assert hanger.aclose_count == 1


# ---------------------------------------------------------------- model propagation


def _fake_result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage={"input_tokens": 1, "output_tokens": 1},
        result="ok",
        uuid="u",
    )


async def test_bridge_emits_init_meta_with_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _ in prompt:
            pass
        yield SystemMessage(
            subtype="init",
            data={
                "model": "claude-opus-4-6",
                "skills": ["ping"],
                "cwd": "/repo",
                "session_id": "abc",
            },
        )
        yield AssistantMessage(
            content=[TextBlock(text="hi")],
            model="claude-opus-4-6",
            parent_tool_use_id=None,
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path))
    items = [item async for item in bridge.ask(chat_id=1, user_text="hi", history=[])]

    init_metas = [it for it in items if isinstance(it, InitMeta)]
    assert len(init_metas) == 1
    init = init_metas[0]
    assert init.model == "claude-opus-4-6"
    assert init.skills == ["ping"]
    assert init.cwd == "/repo"
    assert init.session_id == "abc"
