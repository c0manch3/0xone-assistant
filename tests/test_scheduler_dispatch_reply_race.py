"""Phase 7 / commit 18f — scheduler + main-turn dispatch_reply race dedup.

The two-level mitigation for pitfall #9 (plan §0) requires that the
SchedulerDispatcher and the main-turn `dispatch_reply` call-site share
ONE `_DedupLedger` instance. When both flows happen to mention the
SAME outbox path within the 300 s TTL window — for example: a cron
trigger fires and emits `<outbox>/photo.png`, while moments later the
owner-initiated main turn also references the same path — the network
must see exactly ONE artefact send. The user still receives BOTH
text bodies (cleaned of the raw path), one per flow.

This is distinct from `test_phase7_e2e_double_delivery_dedup.py` (which
covers main-turn + subagent-stop) and from
`test_scheduler_dispatch_reply_integration.py` (which only verifies
the scheduler-side wiring once). Here we drive the REAL
`SchedulerDispatcher.run()` loop AND a direct `dispatch_reply` call
that mimics the main turn's call-site, with one shared ledger and the
production-default TTL semantics. The four scenarios captured:

  1. **Scheduler fires first → main turn second (same path / same chat,
     within 300 s).** Scheduler sends the photo + scheduler-text;
     main-turn skips the photo, still sends its own (different) text.
  2. **Main turn first → scheduler second (same path / same chat,
     within 300 s).** Main-turn wins the send; scheduler skips the
     photo and only emits its (different) text.
  3. **Same path / DIFFERENT chats.** The dedup key is
     `(resolved_path_str, chat_id)` — both legs send the artefact.
  4. **Same path / same chat / OUTSIDE the 300 s window.** TTL has
     expired; both legs send the artefact again.

We drive `time.monotonic` deterministically via a monkey-patched
clock for scenario (4) — cf. `test_dispatch_reply_dedup_ledger.py`
H-12 mock-clock-is-authoritative rule. For scenarios (1)-(3) the
real monotonic clock is fine: the calls happen within milliseconds
and far inside the 300 s window.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply
from assistant.config import ClaudeSettings, SchedulerSettings, Settings
from assistant.media.paths import outbox_dir
from assistant.scheduler.dispatcher import ScheduledTrigger, SchedulerDispatcher
from assistant.scheduler.store import SchedulerStore
from assistant.state.db import apply_schema, connect


# --------------------------------------------------------------------- helpers


class _RecordingAdapter:
    """Records every send for after-the-fact assertions.

    Duck-types `MessengerAdapter` because the dispatcher and
    `dispatch_reply` only call `send_text` / `send_photo` /
    `send_document` / `send_audio`.
    """

    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.photos: list[tuple[int, Path]] = []
        self.documents: list[tuple[int, Path]] = []
        self.audios: list[tuple[int, Path]] = []

    async def start(self) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
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
    """Parameterised handler stub that emits caller-provided text.

    Mirrors the shape used elsewhere in the suite (e.g.
    `test_phase7_e2e_scheduler_media.py`). `fn` is set per-trigger so
    one dispatcher instance can answer a sequence of triggers with
    different bodies.
    """

    def __init__(self) -> None:
        self.fn: Callable[[Any, Callable[[str], Awaitable[None]]], Awaitable[None]] | None = None

    async def handle(
        self, msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        assert self.fn is not None, "handler.fn must be set per trigger"
        await self.fn(msg, emit)


def _settings(tmp_path: Path, owner_chat_id: int = 42) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=owner_chat_id,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        scheduler=SchedulerSettings(),
    )


async def _drive_one_trigger(
    disp: SchedulerDispatcher,
    *,
    trigger_id: int,
    schedule_id: int,
    prompt: str,
) -> None:
    """Pump the dispatcher queue for exactly one trigger, then drain.

    Mirrors the polling shape used by
    `test_phase7_e2e_scheduler_media.py::_drive_one_trigger`. We do
    NOT reuse it directly because tests aren't supposed to import
    each other; the helper is small and specific to this scenario.
    """
    await disp._queue.put(
        ScheduledTrigger(
            trigger_id=trigger_id,
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
    # Give the consumer a tick to ack the dequeued trigger.
    await asyncio.sleep(0.1)
    disp.stop()
    await asyncio.wait_for(task, timeout=2.0)


def _make_photo(outbox: Path, name: str = "shared.png") -> Path:
    photo = outbox / name
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal-plausible PNG magic
    return photo


# --------------------------------------------------------------------- tests


async def test_scheduler_first_then_main_turn_same_path_dedupes_send(
    tmp_path: Path,
) -> None:
    """Scheduler fires; THEN main turn emits the same path within the
    300 s TTL window. Scheduler sends the photo; main turn skips the
    photo but its (different) text still reaches Telegram."""
    settings = _settings(tmp_path)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = _make_photo(outbox)

    conn = await connect(tmp_path / "race-sched-first.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
    handler = _FakeHandler()
    ledger = _DedupLedger()  # shared across both call-sites

    disp = SchedulerDispatcher(
        queue=asyncio.Queue(maxsize=4),
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=ledger,
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="cron-prompt", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "cron-prompt", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    )
    assert trig is not None

    async def emit_scheduler_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"scheduler ready: {photo}")

    handler.fn = emit_scheduler_reply

    # ----- LEG 1: scheduler dispatcher run-loop fires first.
    await _drive_one_trigger(
        disp, trigger_id=trig, schedule_id=sid, prompt="cron-prompt"
    )

    # Scheduler leg: photo went out, text was cleaned.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())], (
        f"scheduler leg should have sent the photo, got: {adapter.photos!r}"
    )
    assert len(adapter.texts) == 1
    sched_chat, sched_text = adapter.texts[0]
    assert sched_chat == settings.owner_chat_id
    assert "scheduler ready" in sched_text
    assert str(photo) not in sched_text

    # ----- LEG 2: main turn runs `dispatch_reply` directly with the
    # SAME ledger, SAME chat, SAME path, but a distinct text body. The
    # main-turn call-site (`TelegramAdapter._on_text` in production)
    # invokes dispatch_reply identically — we collapse the call here.
    await dispatch_reply(
        adapter,  # type: ignore[arg-type]
        chat_id=settings.owner_chat_id,
        text=f"main turn: also see {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "main_turn"},
    )

    # Photo NOT re-sent — ledger suppressed the duplicate.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())], (
        "main turn should have been deduped — only the scheduler "
        f"send should remain. Got: {adapter.photos!r}"
    )
    # But the main-turn text DID go out (cleaned of the raw path).
    assert len(adapter.texts) == 2
    main_chat, main_text = adapter.texts[1]
    assert main_chat == settings.owner_chat_id
    assert "main turn" in main_text
    assert str(photo) not in main_text
    # Sanity: the two text bodies are distinct (each flow had its own).
    assert sched_text != main_text

    await conn.close()


async def test_main_turn_first_then_scheduler_same_path_dedupes_send(
    tmp_path: Path,
) -> None:
    """Reverse ordering of scenario #1. Main turn fires its
    `dispatch_reply` first; the scheduler dispatcher's subsequent run
    finds the path in the ledger and skips the photo send. Both
    text bodies still reach Telegram."""
    settings = _settings(tmp_path)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = _make_photo(outbox, name="reverse.png")

    conn = await connect(tmp_path / "race-main-first.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
    handler = _FakeHandler()
    ledger = _DedupLedger()  # shared across both call-sites

    disp = SchedulerDispatcher(
        queue=asyncio.Queue(maxsize=4),
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=ledger,
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="cron-prompt", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "cron-prompt", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    )
    assert trig is not None

    # ----- LEG 1: main turn runs first.
    await dispatch_reply(
        adapter,  # type: ignore[arg-type]
        chat_id=settings.owner_chat_id,
        text=f"main first: {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "main_turn"},
    )
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())]
    assert len(adapter.texts) == 1
    main_chat, main_text = adapter.texts[0]
    assert main_chat == settings.owner_chat_id
    assert "main first" in main_text
    assert str(photo) not in main_text

    # ----- LEG 2: scheduler dispatcher run-loop wakes up next.
    async def emit_scheduler_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"scheduler followup: {photo}")

    handler.fn = emit_scheduler_reply
    await _drive_one_trigger(
        disp, trigger_id=trig, schedule_id=sid, prompt="cron-prompt"
    )

    # Photo NOT re-sent — only the original main-turn send remains.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())], (
        "scheduler should have been deduped — only the main-turn "
        f"send should remain. Got: {adapter.photos!r}"
    )
    # Scheduler text still went out (cleaned of the raw path).
    assert len(adapter.texts) == 2
    sched_chat, sched_text = adapter.texts[1]
    assert sched_chat == settings.owner_chat_id
    assert "scheduler followup" in sched_text
    assert str(photo) not in sched_text
    # Each flow contributed a distinct body.
    assert main_text != sched_text

    await conn.close()


async def test_same_path_different_chats_both_legs_send(tmp_path: Path) -> None:
    """Dedup key is `(resolved_path_str, chat_id)`. The scheduler runs
    against the owner chat; a parallel main turn fires for a DIFFERENT
    chat with the same artefact path. Both legs must send.

    Phase 7 today is single-owner so this is a forward-compat guard
    for phase-8 multi-chat — and a correctness guard against any future
    refactor that accidentally collapses the key to path-only."""
    owner_chat = 42
    other_chat = 99
    settings = _settings(tmp_path, owner_chat_id=owner_chat)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = _make_photo(outbox, name="cross-chat.png")

    conn = await connect(tmp_path / "race-cross-chat.db")
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
        owner_chat_id=owner_chat,
        settings=settings,
        dedup_ledger=ledger,
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="cron-prompt", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "cron-prompt", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    )
    assert trig is not None

    async def emit_scheduler_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"sched (owner): {photo}")

    handler.fn = emit_scheduler_reply
    await _drive_one_trigger(
        disp, trigger_id=trig, schedule_id=sid, prompt="cron-prompt"
    )

    # Scheduler delivered to the owner.
    assert adapter.photos == [(owner_chat, photo.resolve())]

    # Main turn for a DIFFERENT chat — distinct dedup key, must send.
    await dispatch_reply(
        adapter,  # type: ignore[arg-type]
        chat_id=other_chat,
        text=f"main (other): {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "main_turn"},
    )

    # Both legs delivered — one photo per chat.
    assert adapter.photos == [
        (owner_chat, photo.resolve()),
        (other_chat, photo.resolve()),
    ], (
        "different chat_ids must NOT dedup against each other. "
        f"Got: {adapter.photos!r}"
    )
    # And both cleaned-text bodies reached their respective chats.
    assert len(adapter.texts) == 2
    sched_text = adapter.texts[0]
    main_text = adapter.texts[1]
    assert sched_text[0] == owner_chat
    assert main_text[0] == other_chat
    assert "sched (owner)" in sched_text[1]
    assert "main (other)" in main_text[1]
    assert str(photo) not in sched_text[1]
    assert str(photo) not in main_text[1]

    await conn.close()


async def test_same_path_same_chat_past_ttl_window_both_legs_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the 300 s TTL the ledger forgets the prior key; both legs
    must send again. We drive `time.monotonic` deterministically via
    monkey-patch on the dispatch_reply module — the H-12 authoritative
    mock-clock variant. The real `time.monotonic()` is replaced with
    a sequence-driven stub so each call inside `dispatch_reply` reads
    a controlled timestamp.

    The first leg (scheduler) marks the key at t=0; we then push the
    clock past 300 s before the second leg (main turn) so the ledger
    treats the path as fresh again.
    """
    settings = _settings(tmp_path)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = _make_photo(outbox, name="ttl-expire.png")

    conn = await connect(tmp_path / "race-ttl-expire.db")
    await apply_schema(conn)
    store = SchedulerStore(conn, asyncio.Lock())
    adapter = _RecordingAdapter()
    handler = _FakeHandler()
    ledger = _DedupLedger()  # default 300 s TTL

    disp = SchedulerDispatcher(
        queue=asyncio.Queue(maxsize=4),
        store=store,
        handler=handler,  # type: ignore[arg-type]
        adapter=adapter,  # type: ignore[arg-type]
        owner_chat_id=settings.owner_chat_id,
        settings=settings,
        dedup_ledger=ledger,
    )

    sid = await store.insert_schedule(cron="0 9 * * *", prompt="cron-prompt", tz="UTC")
    trig = await store.try_materialize_trigger(
        sid, "cron-prompt", datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    )
    assert trig is not None

    # Mutable clock: dispatch_reply reads `time.monotonic()` once per
    # ARTEFACT_RE match. We pin it to 0.0 for the scheduler leg, then
    # bump it past TTL before the main-turn leg.
    #
    # CRITICAL: we must NOT patch `time.monotonic` globally — the
    # asyncio event loop on CPython uses `time.monotonic()` internally
    # for `wait_for` / `sleep` / queue `get(timeout=...)`. Freezing it
    # globally deadlocks the SchedulerDispatcher's
    # `wait_for(self._queue.get(), timeout=0.5)` loop.
    #
    # `dispatch_reply.py` does `import time` at module scope and calls
    # `time.monotonic()` — so the name it dereferences is
    # `dispatch_reply.time`. We swap the `time` binding on the module
    # for a lightweight shim carrying a mutable `monotonic` attribute.
    # asyncio continues to see the real `time.monotonic`.
    import types

    import assistant.adapters.dispatch_reply as dr_mod

    current_monotonic = [0.0]
    fake_time = types.SimpleNamespace(monotonic=lambda: current_monotonic[0])
    monkeypatch.setattr(dr_mod, "time", fake_time)

    # ----- LEG 1: scheduler at t=0.
    async def emit_scheduler_reply(
        _msg: Any, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        await emit(f"sched at t=0: {photo}")

    handler.fn = emit_scheduler_reply
    await _drive_one_trigger(
        disp, trigger_id=trig, schedule_id=sid, prompt="cron-prompt"
    )

    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())]
    assert len(adapter.texts) == 1

    # ----- Push the clock past the 300 s TTL window.
    # `_DedupLedger` treats `now - last >= ttl_s` as expired
    # (cf. test_dedup_ttl_mock_clock); 301 s gives one second of
    # headroom past the boundary.
    current_monotonic[0] = 301.0

    # ----- LEG 2: main turn at t=301 (past TTL).
    await dispatch_reply(
        adapter,  # type: ignore[arg-type]
        chat_id=settings.owner_chat_id,
        text=f"main at t=301: {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "main_turn"},
    )

    # Photo SENT AGAIN — ledger entry expired.
    assert adapter.photos == [
        (settings.owner_chat_id, photo.resolve()),
        (settings.owner_chat_id, photo.resolve()),
    ], (
        "TTL has expired; the second leg must re-send the artefact. "
        f"Got: {adapter.photos!r}"
    )
    # Two text bodies, one per leg, each cleaned of the raw path.
    assert len(adapter.texts) == 2
    sched_text = adapter.texts[0][1]
    main_text = adapter.texts[1][1]
    assert "sched at t=0" in sched_text
    assert "main at t=301" in main_text
    assert str(photo) not in sched_text
    assert str(photo) not in main_text

    await conn.close()
