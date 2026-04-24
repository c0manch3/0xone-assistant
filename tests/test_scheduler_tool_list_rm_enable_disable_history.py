"""schedule_list / _rm / _enable / _disable / _history handlers."""

from __future__ import annotations

from pathlib import Path

from assistant.config import SchedulerSettings
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect
from assistant.tools_sdk import scheduler as sched_mod


async def _configure(tmp_path: Path) -> SchedulerStore:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    st = SchedulerStore(conn)
    sched_mod.configure_scheduler(
        data_dir=tmp_path,
        owner_chat_id=1,
        settings=SchedulerSettings(max_schedules=64),
        store=st,
    )
    return st


async def _seed(tmp_path: Path) -> tuple[SchedulerStore, int]:
    st = await _configure(tmp_path)
    sid = await st.add_schedule(
        cron="0 9 * * *", prompt="p", tz="UTC", max_schedules=64
    )
    return st, sid


async def test_list_default_includes_disabled(tmp_path: Path) -> None:
    st, sid = await _seed(tmp_path)
    await st.disable_schedule(sid)
    out = await sched_mod.schedule_list.handler({})  # type: ignore[attr-defined]
    assert out.get("count") == 1


async def test_list_enabled_only_filter(tmp_path: Path) -> None:
    st, sid = await _seed(tmp_path)
    await st.disable_schedule(sid)
    out = await sched_mod.schedule_list.handler(  # type: ignore[attr-defined]
        {"enabled_only": True}
    )
    assert out.get("count") == 0


async def test_rm_requires_confirmation(tmp_path: Path) -> None:
    _, sid = await _seed(tmp_path)
    out = await sched_mod.schedule_rm.handler(  # type: ignore[attr-defined]
        {"id": sid, "confirmed": False}
    )
    assert out.get("is_error")
    assert out.get("code") == 8


async def test_rm_soft_deletes(tmp_path: Path) -> None:
    st, sid = await _seed(tmp_path)
    out = await sched_mod.schedule_rm.handler(  # type: ignore[attr-defined]
        {"id": sid, "confirmed": True}
    )
    assert not out.get("is_error")
    rows = await st.list_schedules()
    assert rows[0]["enabled"] is False


async def test_rm_not_found(tmp_path: Path) -> None:
    await _configure(tmp_path)
    out = await sched_mod.schedule_rm.handler(  # type: ignore[attr-defined]
        {"id": 999, "confirmed": True}
    )
    assert out.get("is_error")
    assert out.get("code") == 6


async def test_enable_disable_roundtrip(tmp_path: Path) -> None:
    _, sid = await _seed(tmp_path)
    out = await sched_mod.schedule_disable.handler({"id": sid})  # type: ignore[attr-defined]
    assert out.get("changed") is True
    out = await sched_mod.schedule_enable.handler({"id": sid})  # type: ignore[attr-defined]
    assert out.get("changed") is True
    # Idempotent: second enable = no-op.
    out = await sched_mod.schedule_enable.handler({"id": sid})  # type: ignore[attr-defined]
    assert out.get("changed") is False


async def test_history_empty(tmp_path: Path) -> None:
    await _configure(tmp_path)
    out = await sched_mod.schedule_history.handler({"limit": 10})  # type: ignore[attr-defined]
    assert out.get("count") == 0


async def test_history_limit_clamping(tmp_path: Path) -> None:
    await _configure(tmp_path)
    out = await sched_mod.schedule_history.handler({"limit": 10000})  # type: ignore[attr-defined]
    # No triggers yet, so count=0 either way; this exercises the clamp path.
    assert not out.get("is_error")
