"""Phase 4 (S-B.2): bridge must yield ToolResultBlocks from UserMessage.

Spike S-B.2 proved that `ToolResultBlock` arrives inside `UserMessage.content`,
NOT `AssistantMessage.content`. Phase 2/3 bridge had an explicit
`UserMessage -- skip` comment on the message loop, which silently dropped
every tool_result. Phase 4 adds a `UserMessage` branch that surfaces
`ToolResultBlock` instances so the handler's `_classify(ToolResultBlock)`
branch (which has always existed) actually fires.
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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
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


async def _drain(it: AsyncIterator[Any]) -> list[Any]:
    return [x async for x in it]


async def test_bridge_yields_tool_result_from_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _env in prompt:
            pass
        yield SystemMessage(subtype="init", data={"model": "m", "skills": []})
        # Assistant asks to run Bash.
        yield AssistantMessage(
            content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "echo hi"})],
            model="m",
            parent_tool_use_id=None,
        )
        # CLI reply arrives inside a UserMessage (spike S-B.2 layout).
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu1",
                    content='{"ok": true}',
                    is_error=False,
                )
            ],
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    bridge = ClaudeBridge(_make_settings(tmp_path))
    items = await _drain(bridge.ask(chat_id=1, user_text="hi", history=[]))

    # InitMeta + ToolUseBlock + ToolResultBlock + ResultMessage.
    assert len(items) == 4
    assert isinstance(items[0], InitMeta)
    assert isinstance(items[1], ToolUseBlock)
    assert isinstance(items[2], ToolResultBlock)
    assert items[2].tool_use_id == "tu1"
    assert items[2].content == '{"ok": true}'
    assert items[2].is_error is False
    assert isinstance(items[3], ResultMessage)


async def test_bridge_ignores_plain_string_user_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UserMessage with str content (SDK echo of our own envelope) is a no-op.

    We never re-persist the user's text — it was already written by the
    handler before the turn started.
    """
    _write_system_prompt(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _env in prompt:
            pass
        yield SystemMessage(subtype="init", data={"model": "m", "skills": []})
        yield UserMessage(content="hi")  # SDK echo; must be ignored
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)
    bridge = ClaudeBridge(_make_settings(tmp_path))
    items = await _drain(bridge.ask(chat_id=1, user_text="hi", history=[]))

    # InitMeta + ResultMessage only.
    assert len(items) == 2
    assert isinstance(items[0], InitMeta)
    assert isinstance(items[1], ResultMessage)


async def test_bridge_skips_non_tool_result_blocks_in_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ToolResultBlock is yielded from UserMessage — other block types
    (none expected today, but the branch must stay defensive) are skipped.
    """
    _write_system_prompt(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _env in prompt:
            pass
        yield SystemMessage(subtype="init", data={"model": "m", "skills": []})
        yield UserMessage(
            content=[
                {"type": "text", "text": "unexpected"},  # not a ToolResultBlock
                ToolResultBlock(tool_use_id="tu1", content="x", is_error=False),
            ],
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)
    bridge = ClaudeBridge(_make_settings(tmp_path))
    items = await _drain(bridge.ask(chat_id=1, user_text="hi", history=[]))

    tr = [i for i in items if isinstance(i, ToolResultBlock)]
    assert len(tr) == 1
    assert tr[0].content == "x"
