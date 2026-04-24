"""Fix 6 / QA H1 / spec §B.2: ``schedule_list`` wraps every prompt in
an ``<untrusted-scheduler-prompt-NONCE>…</…>`` envelope before
returning it to the model.

Defence-in-depth: stored prompts are model-authored and must be
treated as replay-of-owner-voice, not live directives.
"""

from __future__ import annotations

import re
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


async def test_list_wraps_prompts_in_sentinel(tmp_path: Path) -> None:
    st = await _configure(tmp_path)
    await st.add_schedule(
        cron="0 9 * * *", prompt="morning summary", tz="UTC",
        max_schedules=64,
    )
    out = await sched_mod.schedule_list.handler({})  # type: ignore[attr-defined]
    assert out["count"] == 1
    schedules = out["schedules"]
    assert len(schedules) == 1
    wrapped = schedules[0]["prompt"]
    # Nonce-tagged open and close sentinel wrap the body.
    assert re.search(
        r"<untrusted-scheduler-prompt-[0-9a-f]+>", wrapped
    ), f"expected open tag; got: {wrapped!r}"
    assert re.search(
        r"</untrusted-scheduler-prompt-[0-9a-f]+>", wrapped
    ), f"expected close tag; got: {wrapped!r}"
    assert "morning summary" in wrapped


async def test_list_text_surface_also_wraps(tmp_path: Path) -> None:
    """The human-readable ``content[0].text`` surface must also carry
    the wrapped prompt — the model may prefer the text pane over the
    structured ``schedules`` list.
    """
    st = await _configure(tmp_path)
    await st.add_schedule(
        cron="0 9 * * *",
        prompt="read vault note 'weekly.md'",
        tz="UTC",
        max_schedules=64,
    )
    out = await sched_mod.schedule_list.handler({})  # type: ignore[attr-defined]
    text = out["content"][0]["text"]
    assert "untrusted-scheduler-prompt-" in text


async def test_list_empty_still_ok(tmp_path: Path) -> None:
    """Regression guard: no schedules = no wrapping work; no crash."""
    await _configure(tmp_path)
    out = await sched_mod.schedule_list.handler({})  # type: ignore[attr-defined]
    assert out["count"] == 0
    assert out["schedules"] == []
