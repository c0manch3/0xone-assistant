"""Phase 7 / commit 14 — SchedulerDispatcher routes through `dispatch_reply`.

When a scheduler-driven handler emits a final reply that mentions an
absolute path under `<data_dir>/media/outbox/`, the dispatcher must
route the file through `adapter.send_photo` (or `send_document`/
`send_audio`) and then deliver the cleaned-tail text via `send_text`.
Before commit 14 this path dumped the raw path via `send_text` and the
Telegram user saw the string path instead of the photo preview.

This is the single integration assertion for the wiring — the deep
semantics of `dispatch_reply` (dedup, path-guard, regex) live in the
sibling `test_dispatch_reply_*` suites.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.media.paths import outbox_dir
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


class _FakeAdapter:
    """Records every send call. Duck-types `MessengerAdapter`."""

    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.photos: list[tuple[int, Path]] = []
        self.documents: list[tuple[int, Path]] = []
        self.audios: list[tuple[int, Path]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.photos.append((chat_id, path))

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.documents.append((chat_id, path))

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.audios.append((chat_id, path))


class _FakeHandler:
    def __init__(self) -> None:
        self.fn: Callable[[Any, Callable[[str], Awaitable[None]]], Awaitable[None]] | None = None

    async def handle(self, msg: Any, emit: Callable[[str], Awaitable[None]]) -> None:
        assert self.fn is not None
        await self.fn(msg, emit)


async def test_scheduled_photo_path_goes_through_dispatch_reply(tmp_path: Path) -> None:
    # Materialise the outbox dir + a real file the dispatcher will send.
    data_dir = tmp_path / "data"
    outbox = outbox_dir(data_dir)
    outbox.mkdir(parents=True)
    photo = outbox / "photo.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal-plausible PNG magic

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=data_dir,
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )

    conn = await connect(tmp_path / "disp.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _FakeAdapter()
    handler = _FakeHandler()
    ledger = _DedupLedger()

    disp = SchedulerDispatcher(
        queue=asyncio.Queue(maxsize=4),
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=ledger,
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="x", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "x", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    )
    assert trig is not None

    async def emit_photo_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"Ready! {photo}")

    handler.fn = emit_photo_reply

    await disp._queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=sid,
            prompt="x",
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    task: asyncio.Task[None] = asyncio.create_task(disp.run(), name="disp")
    # Poll until queue drains; then give the consumer a tick to ack.
    deadline = asyncio.get_event_loop().time() + 2.0
    while not disp._queue.empty() and asyncio.get_event_loop().time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    disp.stop()
    await asyncio.wait_for(task, timeout=2.0)

    # dispatch_reply routed the photo through send_photo...
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())]
    # ...and the cleaned tail went out as text (path stripped).
    assert len(adapter.texts) == 1
    sent_chat, sent_text = adapter.texts[0]
    assert sent_chat == settings.owner_chat_id
    assert str(photo) not in sent_text
    assert "Ready!" in sent_text

    await conn.close()
