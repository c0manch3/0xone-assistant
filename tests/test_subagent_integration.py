"""Phase 6 / commit 8 — lightweight Daemon wiring smoke tests.

A full `Daemon.start()` path requires OAuth + TelegramAdapter; that's
gated by `RUN_SDK_INT`. These tests verify the integration surface
area (imports, hook factory wiring, bridge construction) without
touching the SDK or Telegram.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.bridge.claude import ClaudeBridge
from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.state.db import apply_schema, connect
from assistant.subagent.definitions import build_agents
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.store import SubagentStore


class _FakeAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    # Phase 7 (commit 4) abstract-compliance stubs. This test file does
    # not exercise the media delivery path; full fakes live alongside the
    # Wave 7A / Wave B tests that actually invoke dispatch_reply.
    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(),
    )


async def test_daemon_wiring_builds_two_bridges_with_shared_hooks(
    tmp_path: Path,
) -> None:
    """B-W2-6 invariant: user-chat bridge and picker bridge are DISTINCT
    ClaudeBridge instances with shared hook factory + agent registry."""
    conn = await connect(tmp_path / "d.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SubagentStore(conn, lock=lock)

    adapter = _FakeAdapter()
    settings = _settings(tmp_path)
    pending: set[asyncio.Task[Any]] = set()
    sub_hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
        dedup_ledger=_DedupLedger(),
    )
    sub_agents = build_agents(settings)

    # Build BOTH bridges from the same factory. Each owns its own
    # Semaphore; no cross-contention.
    bridge = ClaudeBridge(settings, extra_hooks=sub_hooks, agents=sub_agents)
    picker_bridge = ClaudeBridge(settings, extra_hooks=sub_hooks, agents=sub_agents)
    assert bridge is not picker_bridge
    # Semaphores are distinct objects (per-instance, not shared).
    assert bridge._sem is not picker_bridge._sem  # type: ignore[attr-defined]

    # Both bridges resolve options successfully with the shared hooks.
    opts_a = bridge._build_options(system_prompt="sp")
    opts_b = picker_bridge._build_options(system_prompt="sp")
    hooks_a = dict(opts_a.hooks or {})
    hooks_b = dict(opts_b.hooks or {})
    assert "SubagentStart" in hooks_a
    assert "SubagentStop" in hooks_a
    assert "PreToolUse" in hooks_a
    assert hooks_a.keys() == hooks_b.keys()

    # Both advertise Task in allowed_tools (agents registered).
    allowed_a = list(opts_a.allowed_tools or [])
    allowed_b = list(opts_b.allowed_tools or [])
    assert "Task" in allowed_a
    assert "Task" in allowed_b

    await conn.close()


async def test_hooks_factory_sees_both_bridges(tmp_path: Path) -> None:
    """Q6 cross-bridge: the same hooks dict is passed to two bridges.
    When either bridge's options are constructed, the hook list length
    under each event key is identical (no shared-list mutation)."""
    conn = await connect(tmp_path / "d.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    store = SubagentStore(conn, lock=lock)

    adapter = _FakeAdapter()
    settings = _settings(tmp_path)
    pending: set[asyncio.Task[Any]] = set()
    sub_hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
        dedup_ledger=_DedupLedger(),
    )
    agents = build_agents(settings)

    bridge_a = ClaudeBridge(settings, extra_hooks=sub_hooks, agents=agents)
    bridge_b = ClaudeBridge(settings, extra_hooks=sub_hooks, agents=agents)

    # Call _build_options on each sequentially; check they don't mutate
    # the shared `sub_hooks` dict.
    len_before_pre = len(sub_hooks["PreToolUse"])
    _ = bridge_a._build_options(system_prompt="x")
    _ = bridge_b._build_options(system_prompt="x")
    len_after_pre = len(sub_hooks["PreToolUse"])
    assert len_before_pre == len_after_pre

    await conn.close()


async def test_subagent_settings_knobs_honored(tmp_path: Path) -> None:
    """`SubagentSettings` tuning flows through to `build_agents`."""
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(
            max_turns_general=99,
            max_turns_worker=99,
            max_turns_researcher=99,
        ),
    )
    agents = build_agents(settings)
    assert agents["general"].maxTurns == 99
    assert agents["worker"].maxTurns == 99
    assert agents["researcher"].maxTurns == 99
