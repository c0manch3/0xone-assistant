"""CR2.2: SchedulerDispatcher must deliver the prompt that was
snapshot'd into ``triggers.prompt`` at materialise-time — NOT the
current ``schedules.prompt`` which the model may have edited since.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import MessengerAdapter
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import ScheduledTrigger
from assistant.scheduler.store import SchedulerStore
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _CapturingAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _EchoBridge(ClaudeBridge):
    """Bridge that echoes the incoming user_text verbatim so we can
    assert the dispatcher delivered the per-trigger snapshot.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.captured_text: str | None = None

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, history, system_notes
        self.captured_text = user_text
        yield TextBlock(text=f"echo: {user_text[:30]}")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1},
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=99,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


async def test_dispatcher_delivers_triggers_prompt_not_schedules(
    tmp_path: Path,
) -> None:
    """Scenario:
      1. schedule_add with prompt="ORIGINAL".
      2. Loop materialises a trigger — ``triggers.prompt='ORIGINAL'``.
      3. Model UPDATE schedules.prompt='EDITED' mid-tick.
      4. Dispatcher pops the queued trigger.
      5. The delivered user-turn MUST contain 'ORIGINAL', not 'EDITED'.
    """
    db = tmp_path / "h.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = ConversationStore(conn)
    sched_store = SchedulerStore(conn)
    settings = _settings(tmp_path)
    bridge = _EchoBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)
    adapter = _CapturingAdapter()

    sid = await sched_store.add_schedule(
        cron="* * * * *",
        prompt="ORIGINAL",
        tz="UTC",
        max_schedules=64,
    )
    when = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC)
    tid = await sched_store.try_materialize_trigger(sid, "ORIGINAL", when)
    assert tid is not None

    # Simulate the model editing the schedule prompt AFTER materialisation.
    await conn.execute(
        "UPDATE schedules SET prompt='EDITED' WHERE id=?", (sid,)
    )
    await conn.commit()

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue()
    dispatcher = SchedulerDispatcher(
        queue=q,
        store=sched_store,
        handler=handler,
        adapter=adapter,
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    await dispatcher._process(
        ScheduledTrigger(
            trigger_id=tid,
            schedule_id=sid,
            prompt="ORIGINAL",
            scheduled_for_utc=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )

    assert bridge.captured_text is not None
    assert "ORIGINAL" in bridge.captured_text
    assert "EDITED" not in bridge.captured_text
