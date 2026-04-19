"""S-1: `Daemon.stop()` drains `_bg_tasks` via asyncio.gather."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings
from assistant.main import Daemon


class _DummyAdapter:
    def __init__(self, settings: Any, *, dedup_ledger: Any = None) -> None:
        # Phase 7 fix-pack C1: daemon threads the shared ledger.
        del settings, dedup_ledger
        self._handler: Any = None

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id, text


async def _noop_preflight(log: Any) -> None:
    del log


@pytest.mark.asyncio
async def test_stop_awaits_bg_tasks_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)

    flag = {"done": False}

    async def _slow_bootstrap(self: Daemon) -> None:
        await asyncio.sleep(0.1)
        flag["done"] = True

    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", _slow_bootstrap)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(
        Settings(
            telegram_bot_token="t",
            owner_chat_id=1,
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            claude=ClaudeSettings(),
        )
    )
    await daemon.start()
    assert flag["done"] is False
    await daemon.stop()
    assert flag["done"] is True
    assert not daemon._bg_tasks  # fully drained — done callbacks removed refs


@pytest.mark.asyncio
async def test_stop_does_not_propagate_task_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "skills").mkdir()
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)

    async def _raising_bootstrap(self: Daemon) -> None:
        raise RuntimeError("bootstrap blew up")

    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", _raising_bootstrap)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(
        Settings(
            telegram_bot_token="t",
            owner_chat_id=1,
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            claude=ClaudeSettings(),
        )
    )
    await daemon.start()
    # stop() must swallow the RuntimeError via return_exceptions=True.
    await daemon.stop()
