"""Phase 7 / commit 17 — E2E scheduler trigger → media artefact → send_photo.

Covers the Wave-8 commit 14 glue (SchedulerDispatcher._deliver →
dispatch_reply) from the end-user's perspective: a cron-like trigger
fires, the handler hands the dispatcher a reply containing an
absolute outbox path, and the adapter is expected to deliver the
file via `send_photo` BEFORE (or instead of, if the text reduces
to empty) the cleaned-tail `send_text`.

Scenarios:

  * **Scheduler fires → photo path in reply → send_photo invoked.**
    This is the canonical "cron asked for a daily generation" flow.
    The dispatcher owns the outbox-dir resolution via
    `outbox_dir(settings.data_dir)` so the path-guard permits the
    artefact.
  * **Scheduler fires → document path in reply → send_document
    invoked.** Proves the classify branch fires on suffix — same
    artefact-dispatch code, different extension.
  * **Scheduler fires → artefact path OUTSIDE outbox → send_photo
    NOT invoked (path-guard).** The text is delivered verbatim via
    `send_text`. Mirrors the Phase-7 acceptance §20 path-guard
    guarantee (a traversal attempt never resolves to send_photo).

The test is hermetic: no Telegram network, no Claude SDK. The
scheduler handler is replaced with a callable that synthesises the
model's reply text; the adapter is a recording fake.
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


class _RecordingAdapter:
    """Records every send for after-the-fact assertions.

    Duck-types `MessengerAdapter` because the dispatcher only calls
    `send_text` / `send_photo` / `send_document` / `send_audio`.
    """

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
    """Parameterised handler stub that emits caller-provided text."""

    def __init__(self) -> None:
        self.fn: Callable[[Any, Callable[[str], Awaitable[None]]], Awaitable[None]] | None = None

    async def handle(
        self, msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        assert self.fn is not None
        await self.fn(msg, emit)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )


async def _drive_one_trigger(
    disp: SchedulerDispatcher, trig: int, schedule_id: int, prompt: str
) -> None:
    """Pump the dispatcher queue for exactly one trigger + drain."""
    await disp._queue.put(
        ScheduledTrigger(
            trigger_id=trig,
            schedule_id=schedule_id,
            prompt=prompt,
            scheduled_for=datetime(2026, 4, 15, 9, 0, tzinfo=UTC),
            attempt=1,
        )
    )
    task: asyncio.Task[None] = asyncio.create_task(disp.run(), name="disp")
    deadline = asyncio.get_event_loop().time() + 2.0
    while not disp._queue.empty() and asyncio.get_event_loop().time() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    disp.stop()
    await asyncio.wait_for(task, timeout=2.0)


async def test_scheduler_trigger_delivers_photo_via_send_photo(tmp_path: Path) -> None:
    """Scheduler handler returns text with an outbox PNG path → send_photo fires."""
    data_dir = tmp_path / "data"
    outbox = outbox_dir(data_dir)
    outbox.mkdir(parents=True)
    photo = outbox / "daily.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")

    settings = _settings(tmp_path)
    conn = await connect(tmp_path / "sched.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
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

    async def emit_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"Generated! {photo}")

    handler.fn = emit_reply

    await _drive_one_trigger(disp, trig, sid, "x")

    # Artefact delivered as a photo; cleaned text reaches send_text
    # with the path stripped.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())]
    assert adapter.documents == []
    assert adapter.audios == []
    assert len(adapter.texts) == 1
    sent_chat, sent_text = adapter.texts[0]
    assert sent_chat == settings.owner_chat_id
    assert "Generated!" in sent_text
    assert str(photo) not in sent_text

    await conn.close()


async def test_scheduler_trigger_delivers_pdf_via_send_document(tmp_path: Path) -> None:
    """PDF suffix → classify routes to send_document, not send_photo."""
    data_dir = tmp_path / "data"
    outbox = outbox_dir(data_dir)
    outbox.mkdir(parents=True)
    pdf = outbox / "weekly.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    settings = _settings(tmp_path)
    conn = await connect(tmp_path / "sched-pdf.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
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

    sid = await store.insert_schedule(cron="0 10 * * *", prompt="p", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "p", datetime(2026, 4, 15, 10, 0, tzinfo=UTC)
    )
    assert trig is not None

    async def emit_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"Weekly report: {pdf}")

    handler.fn = emit_reply

    await _drive_one_trigger(disp, trig, sid, "p")

    assert adapter.documents == [(settings.owner_chat_id, pdf.resolve())]
    assert adapter.photos == []
    # Cleaned text still reaches Telegram.
    assert len(adapter.texts) == 1
    assert "Weekly report" in adapter.texts[0][1]
    assert str(pdf) not in adapter.texts[0][1]

    await conn.close()


async def test_scheduler_trigger_with_traversal_path_not_treated_as_artefact(
    tmp_path: Path,
) -> None:
    """Path OUTSIDE `outbox_dir(data_dir)` → NOT sent as photo.

    Regression for the Phase-7 path-guard invariant: only paths that
    resolve INSIDE the sanctioned outbox dir route through
    send_photo. A traversal-style path (e.g. model hallucinates
    `/etc/passwd.png`) must fall through to send_text so the user at
    least sees the malformed output rather than us silently opening
    arbitrary files.
    """
    data_dir = tmp_path / "data"
    outbox = outbox_dir(data_dir)
    outbox.mkdir(parents=True)

    # A path that LOOKS like an outbox artefact but resolves to a
    # sibling directory — must fail the is_relative_to guard.
    rogue = tmp_path / "elsewhere" / "evil.png"
    rogue.parent.mkdir()
    rogue.write_bytes(b"\x89PNG\r\n\x1a\n")

    settings = _settings(tmp_path)
    conn = await connect(tmp_path / "sched-rogue.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
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

    sid = await store.insert_schedule(cron="0 11 * * *", prompt="q", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "q", datetime(2026, 4, 15, 11, 0, tzinfo=UTC)
    )
    assert trig is not None

    async def emit_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        # The model says "done" and dumps a rogue path — dispatch_reply
        # must NOT open the file on our behalf.
        await emit(f"done: {rogue}")

    handler.fn = emit_reply

    await _drive_one_trigger(disp, trig, sid, "q")

    assert adapter.photos == []
    assert adapter.documents == []
    # Raw text (with the rogue path still embedded) reaches Telegram so
    # the user can see the model misbehaved.
    assert len(adapter.texts) == 1
    assert str(rogue) in adapter.texts[0][1]

    await conn.close()
