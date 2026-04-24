"""Fix 9 / QA H4: dispatcher with zero-byte handler output reverts to
pending (bumping attempts) instead of silently ``mark_acked``-ing.

Handlers can complete cleanly without emitting text (model refused,
max_turns_exceeded, tool-only response). Treating that as success
hides the failure while the trigger state advances. Owner sees
nothing in Telegram but history says ``acked`` — a silent lost
reminder.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage

from assistant.adapters.base import MessengerAdapter
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.scheduler.dispatcher import SchedulerDispatcher
from assistant.scheduler.loop import ScheduledTrigger
from assistant.scheduler.store import SchedulerStore
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _Adapter(MessengerAdapter):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _EmptyBridge(ClaudeBridge):
    """Yields a ResultMessage only — no TextBlock. Simulates the
    ``max_turns_exceeded`` / model-refused / tool-only happy path.
    """

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, system_notes
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s1",
            total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 0},
        )


def _settings(tmp_path: Path, *, dead_threshold: int = 5) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(dead_attempts_threshold=dead_threshold),
    )


async def test_empty_output_reverts_to_pending(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    st = SchedulerStore(conn)
    settings = _settings(tmp_path)
    handler = ClaudeHandler(settings, conv, _EmptyBridge(settings))
    adapter = _Adapter()

    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue()
    dispatcher = SchedulerDispatcher(
        queue=q,
        store=st,
        handler=handler,
        adapter=adapter,
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    await dispatcher._process(        ScheduledTrigger(
            trigger_id=tid,
            schedule_id=sid,
            prompt="p",
            scheduled_for_utc=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    cur = await conn.execute(
        "SELECT status, attempts, last_error FROM triggers WHERE id=?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending", (
        "empty-output trigger must NOT be acked; "
        "revert_to_pending is required so retry + dead-letter run"
    )
    assert row[1] == 1
    assert row[2] is not None and "empty" in row[2].lower()
    # No send_text: we do not spam the owner on a retryable retry.
    assert adapter.sent == []


async def test_empty_output_dead_letters_after_threshold(
    tmp_path: Path,
) -> None:
    db = tmp_path / "x.db"
    conn = await connect(db)
    await apply_schema(conn)
    conv = ConversationStore(conn)
    st = SchedulerStore(conn)
    settings = _settings(tmp_path, dead_threshold=2)
    handler = ClaudeHandler(settings, conv, _EmptyBridge(settings))
    adapter = _Adapter()

    sid = await st.add_schedule(
        cron="* * * * *", prompt="p", tz="UTC", max_schedules=64
    )
    when = dt.datetime(2026, 4, 21, 9, 0, tzinfo=dt.UTC)
    tid = await st.try_materialize_trigger(sid, "p", when)
    assert tid is not None

    q: asyncio.Queue[ScheduledTrigger] = asyncio.Queue()
    dispatcher = SchedulerDispatcher(
        queue=q,
        store=st,
        handler=handler,
        adapter=adapter,
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
    )
    # First attempt: revert_to_pending, attempts=1.
    await dispatcher._process(        ScheduledTrigger(
            trigger_id=tid,
            schedule_id=sid,
            prompt="p",
            scheduled_for_utc=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    dispatcher._lru.clear()    # Second attempt with threshold=2 → dead + notify.
    await dispatcher._process(        ScheduledTrigger(
            trigger_id=tid,
            schedule_id=sid,
            prompt="p",
            scheduled_for_utc=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    cur = await conn.execute(
        "SELECT status, attempts FROM triggers WHERE id=?", (tid,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "dead"
    assert row[1] == 2
    assert len(adapter.sent) == 1
    assert "dead-lettered" in adapter.sent[0][1]
    assert "empty" in adapter.sent[0][1]
