"""Phase-4 Q-R10 debt: ClaudeBridge.ask must invoke
``mcp__memory__memory_search`` via the live SDK when the model
reasonably needs to look up a stored fact.

Gated on ``ENABLE_CLAUDE_INTEGRATION=1`` because it spawns a real
``claude`` CLI subprocess and costs tokens.

Owner's seed vault (``~/.local/share/0xone-assistant/vault``)
contains a few project notes the model can match against. We check
that at least ONE of the streamed blocks carries an expected marker
(the model invokes ``memory_search`` or produces text clearly
grounded in the seed vault).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from claude_agent_sdk import ToolUseBlock

from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.tools_sdk import memory as _memory_mod


def _integration_skip() -> bool:
    return os.environ.get("ENABLE_CLAUDE_INTEGRATION") != "1"


@pytest.mark.skipif(
    _integration_skip(), reason="set ENABLE_CLAUDE_INTEGRATION=1 to run"
)
async def test_ask_invokes_memory_search_for_owner_seed_data() -> None:
    # Point memory at the real owner vault — this test is deliberately
    # read-only and passes even if memory_search returns 0 hits.
    vault = Path.home() / ".local" / "share" / "0xone-assistant" / "vault"
    idx = (
        Path.home()
        / ".local"
        / "share"
        / "0xone-assistant"
        / "memory-index.db"
    )
    if not vault.exists():
        pytest.skip(f"owner vault not present at {vault}")
    _memory_mod.reset_memory_for_tests()
    _memory_mod.configure_memory(vault_dir=vault, index_db_path=idx)

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        claude=ClaudeSettings(timeout=300, max_concurrent=1, history_limit=0),
    )
    bridge = ClaudeBridge(settings)
    prompt = (
        "Перечисли названия проектов, которые ты знаешь обо мне из "
        "своей long-term memory. Обязательно вызови memory_search "
        "или memory_list перед ответом."
    )
    saw_memory_tool = False
    async for block in bridge.ask(1, prompt, []):
        if isinstance(block, ToolUseBlock) and block.name.startswith(
            "mcp__memory__"
        ):
            saw_memory_tool = True
            break
    assert saw_memory_tool, "model did not call any mcp__memory__* tool"
