from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import ClaudeSettings, Settings


def _settings(project_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=project_root / "data",
        claude=ClaudeSettings(timeout=1, max_concurrent=1),
    )


async def _never_yielding(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
    """Stand-in for ``claude_agent_sdk.query`` that hangs forever."""
    # Keep loop alive; never emit a message.
    await asyncio.Event().wait()
    # Unreachable — but the function must be an async generator so we
    # can `async for` it in the bridge.
    yield  # pragma: no cover


async def test_timeout_raises_bridge_error(tmp_path: Path) -> None:
    """S9: a stalled `query()` must surface as `ClaudeBridgeError("timeout")`
    within the configured timeout, and the semaphore must release so a
    subsequent call can proceed."""
    (tmp_path / "skills").mkdir()
    # Minimal system_prompt.md inside the tmp project_root so
    # _render_system_prompt doesn't try to read the real repo file.
    sp_dir = tmp_path / "src" / "assistant" / "bridge"
    sp_dir.mkdir(parents=True)
    (sp_dir / "system_prompt.md").write_text(
        "root={project_root}\nskills:\n{skills_manifest}\n",
        encoding="utf-8",
    )

    bridge = ClaudeBridge(_settings(tmp_path))

    with (
        patch("assistant.bridge.claude.query", _never_yielding),
        pytest.raises(ClaudeBridgeError, match="timeout"),
    ):
        async for _ in bridge.ask(1, "hello", history=[]):
            pass

    # Semaphore should be released — a second call must not block forever.
    # (If it did, this test would hang and pytest would kill it.)
    async with asyncio.timeout(5):
        with (
            patch("assistant.bridge.claude.query", _never_yielding),
            pytest.raises(ClaudeBridgeError, match="timeout"),
        ):
            async for _ in bridge.ask(1, "again", history=[]):
                pass
