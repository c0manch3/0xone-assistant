"""End-to-end scheduler smoke: insert a trigger at scheduled_for=now,
spin up the dispatcher against a real OAuth ``claude`` CLI, and assert
``adapter.send_text`` is called within ~30 seconds.

Gated on ``ENABLE_SCHEDULER_INTEGRATION=1``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import ScheduledTrigger
from assistant.scheduler.store import SchedulerStore
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _CapturingAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._event = asyncio.Event()

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        self._event.set()

    async def wait_for_delivery(self, deadline_s: float) -> bool:
        try:
            async with asyncio.timeout(deadline_s):
                await self._event.wait()
            return True
        except TimeoutError:
            return False


def _integration_skip() -> bool:
    return os.environ.get("ENABLE_SCHEDULER_INTEGRATION") != "1"


@pytest.mark.skipif(
    _integration_skip(),
    reason="set ENABLE_SCHEDULER_INTEGRATION=1 to run",
)
async def test_dispatcher_delivers_trigger_via_real_oauth(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    st = SchedulerStore(conn)

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=60, max_concurrent=1, history_limit=0),
        scheduler=SchedulerSettings(),
    )
    bridge = ClaudeBridge(settings)
    handler = ClaudeHandler(settings, conv, bridge)
    adapter = _CapturingAdapter()

    sid = await st.add_schedule(
        cron="* * * * *",
        prompt="Reply with the single word 'PONG' and nothing else.",
        tz="UTC",
        max_schedules=64,
    )
    when = dt.datetime.now(dt.UTC)
    tid = await st.try_materialize_trigger(
        sid, "Reply with the single word 'PONG' and nothing else.", when
    )
    assert tid is not None

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue()
    q.put_nowait(
        ScheduledTrigger(
            trigger_id=tid,
            schedule_id=sid,
            prompt="Reply with the single word 'PONG' and nothing else.",
            scheduled_for_utc=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    dispatcher = SchedulerDispatcher(
        queue=q,
        store=st,
        handler=handler,
        adapter=adapter,
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    task = asyncio.create_task(dispatcher.run())
    try:
        delivered = await adapter.wait_for_delivery(deadline_s=90.0)
        assert delivered, "adapter.send_text never called within 90s"
    finally:
        dispatcher.stop()
        await asyncio.wait_for(task, timeout=5.0)
