"""Phase 6: Daemon.start subagent wiring integration."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from assistant.config import Settings
from assistant.state.db import apply_schema, connect
from assistant.subagent.store import SubagentStore


def _settings(tmp_path: Path) -> Settings:
    return cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )


async def test_recover_orphans_runs_at_start_with_interrupted(tmp_path: Path) -> None:
    """Pre-seed an orphan, run apply_schema + recover_orphans, verify."""
    db = tmp_path / "assistant.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    # Seed a Branch-2 orphan.
    await store.record_started(
        sdk_agent_id="prev-agent",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    # Simulate Daemon.start invoking recover_orphans.
    rec = await store.recover_orphans()
    assert rec.interrupted == 1
    assert rec.dropped_no_sdk == 0
    assert rec.dropped_stale == 0
    job = await store.get_by_agent_id("prev-agent")
    assert job is not None
    assert job.status == "interrupted"
    assert job.finished_at is not None
    await conn.close()


async def test_subagent_settings_defaults() -> None:
    """SubagentSettings ships defaults that match research RQ4 / RQ2."""
    from assistant.config import SubagentSettings

    s = SubagentSettings()
    assert s.enabled is True
    assert s.picker_tick_s == 1.0
    assert s.orphan_stale_s == 3600
    assert s.max_depth == 1
    assert s.notify_throttle_ms == 500
    assert s.claude_subagent_timeout == 900


async def test_settings_has_subagent_attribute(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    assert hasattr(s, "subagent")
    assert s.subagent.enabled is True


async def test_configure_subagent_reads_store(tmp_path: Path) -> None:
    """configure_subagent must populate the @tool surface ctx."""
    from assistant.tools_sdk.subagent import (
        configure_subagent,
        reset_subagent_for_tests,
    )

    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    settings = _settings(tmp_path)
    reset_subagent_for_tests()
    configure_subagent(
        store=store,
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    from assistant.tools_sdk import subagent as mod

    assert mod._CONFIGURED is True
    assert mod._CTX["store"] is store
    assert mod._CTX["owner_chat_id"] == settings.owner_chat_id


async def test_reconfigure_with_different_owner_raises(tmp_path: Path) -> None:
    from assistant.tools_sdk.subagent import (
        configure_subagent,
        reset_subagent_for_tests,
    )

    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    settings = _settings(tmp_path)
    reset_subagent_for_tests()
    configure_subagent(
        store=store, owner_chat_id=42, settings=settings
    )
    with pytest.raises(RuntimeError, match="different owner_chat_id"):
        configure_subagent(
            store=store, owner_chat_id=99, settings=settings
        )


async def test_picker_constructed_when_subagent_enabled(tmp_path: Path) -> None:
    """Smoke: SubagentRequestPicker accepts the bridge + settings."""
    from assistant.bridge.claude import ClaudeBridge
    from assistant.subagent.picker import SubagentRequestPicker

    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    settings = _settings(tmp_path)
    bridge = ClaudeBridge(settings)
    picker = SubagentRequestPicker(store, bridge, settings=settings)
    # Confirm the stop event is fresh.
    assert picker._stop.is_set() is False
    picker.request_stop()
    assert picker._stop.is_set() is True
