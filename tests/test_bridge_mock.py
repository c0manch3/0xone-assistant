"""Mock-based bridge test.

No network, no `claude` CLI — we patch `assistant.bridge.claude.query` with a
fake async generator that yields SDK-shaped messages.
"""

from __future__ import annotations

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
from assistant.bridge.claude import ClaudeBridge, InitMeta
from assistant.config import ClaudeSettings, Settings


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(max_concurrent=1, timeout=30, max_turns=3, history_limit=20),
    )


def _write_system_prompt(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "src" / "assistant" / "bridge"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "system_prompt.md").write_text(
        "root={project_root}\nskills:\n{skills_manifest}\n", encoding="utf-8"
    )
    (tmp_path / "skills").mkdir(exist_ok=True)


async def _drain(it: AsyncIterator[Any]) -> list[Any]:
    return [x async for x in it]


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


async def test_bridge_yields_blocks_then_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)
    captured_prompts: list[dict[str, Any]] = []

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for env in prompt:
            captured_prompts.append(env)
        yield SystemMessage(subtype="init", data={"model": "m", "skills": [], "cwd": "/"})
        yield AssistantMessage(
            content=[TextBlock(text="hello")], model="claude-opus-4-6", parent_tool_use_id=None
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path))
    items = await _drain(bridge.ask(chat_id=1, user_text="hi", history=[]))

    # InitMeta (carries `model`) + TextBlock + ResultMessage. The bridge
    # promotes SystemMessage(subtype='init') into an InitMeta sentinel so the
    # handler can fold `model` into `turns.meta_json` (phase 2 unverified item
    # 7 -- proxy through InitMeta).
    assert len(items) == 3
    assert isinstance(items[0], InitMeta)
    assert items[0].model == "m"
    assert isinstance(items[1], TextBlock)
    assert items[1].text == "hello"
    assert isinstance(items[2], ResultMessage)

    # Empty history → only the current user envelope.
    assert len(captured_prompts) == 1
    env = captured_prompts[0]
    assert env["type"] == "user"
    assert env["message"]["role"] == "user"
    assert env["message"]["content"] == "hi"
    assert env["session_id"] == "chat-1"


async def test_bridge_feeds_history_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)
    captured: list[dict[str, Any]] = []

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for env in prompt:
            captured.append(env)
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path))
    history = [
        {
            "turn_id": "t1",
            "role": "user",
            "content": [{"type": "text", "text": "prev user"}],
            "block_type": "text",
        },
        {
            "turn_id": "t1",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}],
            "block_type": "tool_use",
        },
    ]
    items = await _drain(bridge.ask(chat_id=5, user_text="now", history=history))
    assert any(isinstance(x, ResultMessage) for x in items)

    # 1 envelope for t1 (with synthetic tool-note prepended) + 1 current.
    assert len(captured) == 2
    t1_env = captured[0]
    content = t1_env["message"]["content"]
    assert isinstance(content, list)
    assert content[0]["text"].startswith("[system-note:")
    assert "Bash" in content[0]["text"]
    assert content[1]["text"] == "prev user"

    assert captured[1]["message"]["content"] == "now"


async def test_bridge_skips_thinking_from_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)
    captured: list[dict[str, Any]] = []

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for env in prompt:
            captured.append(env)
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path))
    history = [
        {
            "turn_id": "t1",
            "role": "user",
            "content": [{"type": "text", "text": "q"}],
            "block_type": "text",
        },
        {
            "turn_id": "t1",
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "...", "signature": "s"}],
            "block_type": "thinking",
        },
    ]
    await _drain(bridge.ask(chat_id=1, user_text="x", history=history))
    # History envelope for t1 has only the user text (thinking filtered out,
    # no tool_use so no system-note).
    assert len(captured) == 2
    assert captured[0]["message"]["content"] == "q"
