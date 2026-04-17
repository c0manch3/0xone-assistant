"""Phase 4 B1 E2E: ToolResultBlocks from UserMessage land in ConversationStore.

Spike S-B.2 proved phase 2/3 silently dropped tool_result rows because the
bridge skipped UserMessage entirely. This test walks the full flow:
bridge yields -> handler._classify(ToolResultBlock) -> conv.append ->
row persists with role='tool' + block_type='tool_result'.
"""

from __future__ import annotations

import json
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
from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


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


async def test_tool_result_row_persisted_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_system_prompt(tmp_path)
    settings = _make_settings(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _env in prompt:
            pass
        yield SystemMessage(subtype="init", data={"model": "m", "skills": []})
        yield AssistantMessage(
            content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "echo hi"})],
            model="m",
            parent_tool_use_id=None,
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu1",
                    content='{"ok": true}',
                    is_error=False,
                ),
            ],
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    conn = await connect(tmp_path / "e2e.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    bridge = ClaudeBridge(settings)
    handler = ClaudeHandler(settings, conv, turns, bridge)

    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    await handler.handle(
        IncomingMessage(chat_id=42, text="run bash", origin="telegram"),
        emit,
    )

    # Exactly one tool_result row was persisted with role='tool'.
    async with conn.execute(
        "SELECT role, content_json, block_type FROM conversations WHERE block_type = 'tool_result'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    role, content_json, block_type = rows[0]
    assert role == "tool"
    assert block_type == "tool_result"
    payload = json.loads(content_json)
    assert isinstance(payload, list)
    assert len(payload) == 1
    block = payload[0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu1"
    assert block["content"] == '{"ok": true}'
    assert block["is_error"] is False

    await conn.close()


async def test_tool_result_error_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_system_prompt(tmp_path)
    settings = _make_settings(tmp_path)

    async def fake_query(*, prompt: AsyncIterator[dict[str, Any]], options: Any) -> Any:
        async for _env in prompt:
            pass
        yield SystemMessage(subtype="init", data={"model": "m", "skills": []})
        yield AssistantMessage(
            content=[ToolUseBlock(id="tu1", name="Bash", input={"command": "false"})],
            model="m",
            parent_tool_use_id=None,
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu1",
                    content="Exit code 1\nboom",
                    is_error=True,
                ),
            ],
        )
        yield _fake_result()

    monkeypatch.setattr(bridge_module, "query", fake_query)

    conn = await connect(tmp_path / "e2e-err.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)
    bridge = ClaudeBridge(settings)
    handler = ClaudeHandler(settings, conv, turns, bridge)

    async def _emit(_t: str) -> None:
        return None

    await handler.handle(
        IncomingMessage(chat_id=99, text="run bash", origin="telegram"),
        _emit,
    )

    async with conn.execute(
        "SELECT content_json FROM conversations WHERE block_type = 'tool_result'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload[0]["is_error"] is True
    assert "Exit code 1" in payload[0]["content"]

    await conn.close()
