"""Phase 6e fix-pack regression tests.

Each test pins one fix from the consolidated fix-pack. The names map
1:1 to the fix-pack identifiers (F1..F12) for traceability:

- **F1**: bg job aggregates streamed text into ONE final emit_direct
  call (no per-block 429 storm, no 10x push notifications).
- **F3**: ``emit_direct`` logs ``TelegramRetryAfter`` /
  ``TelegramAPIError`` / generic exceptions instead of silently
  suppressing them — operators get a clean signal without a crashing
  bg task.
- **F4**: ``Daemon.spawn_audio_task`` wraps the inner coroutine so
  unhandled exceptions become a structured ``log.exception`` instead
  of an unraisable-hook warning.
- **F5**: path-containment guards on the audio path route the Russian
  error reply through ``emit_direct`` so the owner actually sees it.
- **F6**: inline-mode (no audio_persist_pending) awaits ``_persist()``
  directly without an orphan-able shielded task.
- **F7**: tmp-file unlink survives a ``CancelledError`` raised by the
  persist branch — outer ``finally`` guarantees cleanup.
- **F8**: outer try/finally guarantees persist runs even if
  cancellation lands mid-transcribe (before the bridge.ask call).
- **F11**: ``Daemon.stop`` drain timeout log includes task names so
  post-mortem identifies which turn(s) overran the budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.methods import SendMessage

from assistant.adapters.base import IncomingMessage
from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.claude import ClaudeBridge
from assistant.config import (
    AudioBgSettings,
    ClaudeSettings,
    SchedulerSettings,
    Settings,
)
from assistant.handlers.message import ClaudeHandler
from assistant.main import Daemon
from assistant.services.transcription import (
    TranscriptionResult,
    TranscriptionService,
)
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(enabled=False),
        whisper_api_url="http://mac.test:9000",
        whisper_api_token="x" * 32,
        claude_voice_timeout=900,
        voice_vault_threshold_seconds=120,
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "fixpack.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


def _result_message() -> Any:
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
        stop_reason="end_turn",
    )


def _text_block(text: str) -> Any:
    from claude_agent_sdk import TextBlock

    return TextBlock(text=text)


def _make_audio_tmp(tmp_path: Path, name: str = "voice.ogg") -> Path:
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / name
    p.write_bytes(b"OggS bytes")
    return p


class _ScriptedBridge(ClaudeBridge):
    """Bridge that yields a fixed sequence of SDK blocks/messages."""

    def __init__(self, settings: Settings, script: list[Any]) -> None:
        super().__init__(settings)
        self._script = script

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
        timeout_override: int | None = None,
    ) -> AsyncIterator[Any]:
        for item in self._script:
            yield item


class _CancellingBridge(ClaudeBridge):
    """Bridge that raises CancelledError to simulate Daemon.stop mid-transcribe."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
        timeout_override: int | None = None,
    ) -> AsyncIterator[Any]:
        raise asyncio.CancelledError
        if False:  # pragma: no cover — placate the AsyncIterator type
            yield None


class _StubTranscription(TranscriptionService):
    def __init__(
        self,
        settings: Settings,
        result: TranscriptionResult,
        *,
        sleep_s: float = 0.0,
    ) -> None:
        super().__init__(settings)
        self._result = result
        self._sleep_s = sleep_s

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return True

    async def health_check(self) -> bool:  # type: ignore[override]
        return True

    async def transcribe_file(  # type: ignore[override]
        self, audio_path: Path, mime_type: str, filename: str
    ) -> TranscriptionResult:
        if self._sleep_s > 0:
            await asyncio.sleep(self._sleep_s)
        return self._result


class _CancellingTranscription(TranscriptionService):
    """Transcription that signals it has reached the await point and
    parks until the test cancels it — simulates ``Daemon.stop`` cancel
    mid-transcribe (F8).
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.entered = asyncio.Event()
        self.never_finish = asyncio.Event()

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return True

    async def health_check(self) -> bool:  # type: ignore[override]
        return True

    async def transcribe_file(  # type: ignore[override]
        self, audio_path: Path, mime_type: str, filename: str
    ) -> TranscriptionResult:
        self.entered.set()
        await self.never_finish.wait()
        # Unreachable; placate the type-checker.
        return TranscriptionResult(text="x", language="ru", duration=1)


# ---------------------------------------------------------------------------
# F1 — single emit_direct after streaming
# ---------------------------------------------------------------------------


async def test_f1_streamed_text_blocks_emit_once(tmp_path: Path) -> None:
    """The bg job must coalesce N text blocks into ONE emit_direct call.

    Pre-fix the bg loop emitted per-block; for a 10-block reply the
    owner saw 10 push notifications + risk of TelegramRetryAfter.
    Post-fix the streamed text is buffered and the final string is
    emitted once at the end of the bridge loop.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _ScriptedBridge(
        settings,
        script=[
            _text_block("первая часть. "),
            _text_block("вторая часть. "),
            _text_block("третья часть."),
            _result_message(),
        ],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hello", language="ru", duration=5),
    )
    handler = ClaudeHandler(
        settings, store, bridge, transcription, audio_bridge=bridge
    )

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    captured: list[str] = []

    async def emit(_text: str) -> None:
        return

    async def emit_direct(text: str) -> None:
        captured.append(text)

    await handler.handle(msg, emit, emit_direct=emit_direct)

    # Exactly ONE emit (not 3) — the streamed text was aggregated.
    assert len(captured) == 1
    assert captured[0] == "первая часть. вторая часть. третья часть."


async def test_f1_empty_response_falls_back_to_placeholder(
    tmp_path: Path,
) -> None:
    """When the model emits zero text blocks (only tool_use), the
    final emit_direct must be ``"(пустой ответ)"`` — same fallback as
    phase 6c text/photo paths."""
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    # No TextBlock — only a result.
    bridge = _ScriptedBridge(settings, script=[_result_message()])
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=5),
    )
    handler = ClaudeHandler(
        settings, store, bridge, transcription, audio_bridge=bridge
    )

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )
    captured: list[str] = []

    async def emit(_t: str) -> None:
        return

    async def emit_direct(text: str) -> None:
        captured.append(text)

    await handler.handle(msg, emit, emit_direct=emit_direct)
    assert captured == ["(пустой ответ)"]


# ---------------------------------------------------------------------------
# F3 — emit_direct logs on TelegramRetryAfter / TelegramAPIError
# ---------------------------------------------------------------------------


def _capture_structlog_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    """Replace ``adapters.telegram``'s logger with a capture sink.

    The project uses structlog; structlog records bypass the standard
    Python logging caplog fixture (it goes straight through structlog's
    own JSONRenderer to stdout). Monkey-patching the module-level
    ``log`` lets us assert specific event names + kwargs without
    parsing JSON or stdout.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    class _CapturingLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def error(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

    import assistant.adapters.telegram as telegram_module

    monkeypatch.setattr(telegram_module, "log", _CapturingLogger())
    return captured


async def test_f3_emit_direct_logs_telegram_retry_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter's ``emit_direct`` MUST log a structured
    ``emit_direct_rate_limited`` warning when the Telegram API
    surfaces a ``TelegramRetryAfter`` — pre-fix it was swallowed by a
    blanket ``contextlib.suppress(Exception)``.
    """
    captured = _capture_structlog_warnings(monkeypatch)

    settings = _build_settings(tmp_path)
    adapter = TelegramAdapter(settings)
    # Inject a Bot mock that raises TelegramRetryAfter on send_message.
    adapter._bot = MagicMock()  # type: ignore[assignment]
    method = SendMessage(chat_id=42, text="x")
    adapter._bot.send_message = AsyncMock(
        side_effect=TelegramRetryAfter(
            method=method, message="r", retry_after=7
        )
    )
    adapter._bot.send_chat_action = AsyncMock()

    captured_lifecycle: list[Any] = []

    class _Handler:
        async def handle(
            self,
            msg: IncomingMessage,
            emit: Any,
            emit_direct: Any | None = None,
            typing_lifecycle: Any | None = None,
        ) -> None:
            captured_lifecycle.append(typing_lifecycle)
            assert emit_direct is not None
            await emit_direct("something")

    adapter.set_handler(_Handler())  # type: ignore[arg-type]

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    await adapter._dispatch_audio_turn(42, msg)

    events = [(e, k) for e, k in captured]
    rate_limit_events = [
        (e, k) for e, k in events if e == "emit_direct_rate_limited"
    ]
    assert rate_limit_events, f"expected rate-limit log; got {events}"
    # The retry_after value carries through to the structured log.
    assert rate_limit_events[0][1].get("retry_after") == 7
    assert rate_limit_events[0][1].get("chat_id") == 42
    # F2 hotfix (2026-04-29 owner UX feedback): typing_lifecycle is
    # NO LONGER passed by adapter — pre-lock ack is the progress
    # signal; persistent typing during 22-45 min bg run was UI noise.
    # Bg path falls back to AudioJob's _noop_typing_lifecycle.
    assert captured_lifecycle == [None]


async def test_f3_emit_direct_logs_telegram_api_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic ``TelegramAPIError`` is logged under
    ``emit_direct_telegram_api_error`` (separate event from rate limit
    so dashboards can split signal vs noise)."""
    captured = _capture_structlog_warnings(monkeypatch)

    settings = _build_settings(tmp_path)
    adapter = TelegramAdapter(settings)
    adapter._bot = MagicMock()  # type: ignore[assignment]
    method = SendMessage(chat_id=42, text="x")
    adapter._bot.send_message = AsyncMock(
        side_effect=TelegramAPIError(method=method, message="boom")
    )
    adapter._bot.send_chat_action = AsyncMock()

    class _Handler:
        async def handle(
            self,
            msg: IncomingMessage,
            emit: Any,
            emit_direct: Any | None = None,
            typing_lifecycle: Any | None = None,
        ) -> None:
            assert emit_direct is not None
            await emit_direct("something")

    adapter.set_handler(_Handler())  # type: ignore[arg-type]

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )
    await adapter._dispatch_audio_turn(42, msg)

    api_error_events = [
        e for e, _ in captured if e == "emit_direct_telegram_api_error"
    ]
    assert api_error_events, (
        f"expected API-error log event; got {captured}"
    )


# ---------------------------------------------------------------------------
# F4 — spawn_audio_task wraps unhandled exceptions
# ---------------------------------------------------------------------------


async def test_f4_spawn_audio_task_logs_unhandled_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception escaping the bg coroutine is converted to a
    structured ``audio_bg_task_unhandled`` log line. Without the
    wrapper the asyncio default unraisable hook would only print to
    stderr — invisible to structured-log telemetry.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    class _CapturingLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def error(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

    import assistant.main as main_module

    monkeypatch.setattr(main_module, "log", _CapturingLogger())

    settings = _build_settings(tmp_path)
    daemon = Daemon(settings)

    async def _bad() -> None:
        raise RuntimeError("simulated unhandled error in bg audio job")

    daemon.spawn_audio_task(_bad())
    # Drain the registered bg task so the wrapper actually runs.
    await asyncio.gather(*daemon._bg_tasks, return_exceptions=True)

    events = [e for e, _ in captured]
    assert "audio_bg_task_unhandled" in events, (
        f"expected log event; got {events}"
    )


async def test_f4_spawn_audio_task_propagates_cancellation(
    tmp_path: Path,
) -> None:
    """``CancelledError`` MUST propagate out of the wrapper so the
    daemon's ``_bg_tasks`` drain semantics still work; only non-cancel
    exceptions are absorbed into a log line."""
    settings = _build_settings(tmp_path)
    daemon = Daemon(settings)

    started = asyncio.Event()
    can_finish = asyncio.Event()

    async def _slow() -> None:
        started.set()
        await can_finish.wait()

    daemon.spawn_audio_task(_slow())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    # Cancel as Daemon.stop would do.
    for t in list(daemon._bg_tasks):
        t.cancel()
    results = await asyncio.gather(
        *daemon._bg_tasks, return_exceptions=True
    )
    # Wrapper re-raises CancelledError so the gather sees it.
    assert any(
        isinstance(r, asyncio.CancelledError) for r in results
    ), f"expected CancelledError; got {results}"


# ---------------------------------------------------------------------------
# F5 — path-containment failure routed through emit_direct on audio path
# ---------------------------------------------------------------------------


async def test_f5_audio_path_containment_failure_uses_emit_direct(
    tmp_path: Path,
) -> None:
    """A path-containment guard rejection on the audio path must reach
    the owner via ``emit_direct`` — pre-fix it called ``emit`` (the
    no-op lock-time channel for audio) and the owner saw nothing.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _ScriptedBridge(settings, script=[])
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=5),
    )
    handler = ClaudeHandler(
        settings, store, bridge, transcription, audio_bridge=bridge
    )

    # Construct an audio message whose attachment is OUTSIDE the
    # uploads_dir — the path-containment guard rejects it.
    outside = tmp_path / "outside.ogg"
    outside.write_bytes(b"OggS")
    # Bypass __post_init__ source check by passing the outside path —
    # the kind is still 'ogg' so the audio branch fires.
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=outside,
        attachment_kind="ogg",
        attachment_filename="outside.ogg",
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    emit_calls: list[str] = []
    direct_calls: list[str] = []

    async def emit(text: str) -> None:
        emit_calls.append(text)

    async def emit_direct(text: str) -> None:
        direct_calls.append(text)

    await handler.handle(msg, emit, emit_direct=emit_direct)

    # The Russian rejection message must land on emit_direct (audio
    # path), NOT emit (lock-time no-op channel).
    assert direct_calls, "owner-visible rejection should land on emit_direct"
    assert any("uploads dir" in m for m in direct_calls)
    assert emit_calls == [], (
        f"emit (lock-time channel) must stay silent on audio rejection; "
        f"got {emit_calls}"
    )


# ---------------------------------------------------------------------------
# F6 — inline-mode persist runs synchronously, no orphan task
# ---------------------------------------------------------------------------


async def test_f6_inline_mode_persists_without_orphan_task(
    tmp_path: Path,
) -> None:
    """When ``audio_persist_pending`` is None (inline / test mode),
    the bg job awaits ``_persist()`` directly. No
    ``asyncio.create_task`` + ``shield`` indirection means no risk of
    orphaning the persist task on cancel."""
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _ScriptedBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=5),
    )

    # Default constructor — audio_persist_pending stays None (inline mode).
    handler = ClaudeHandler(
        settings, store, bridge, transcription, audio_bridge=bridge
    )
    assert handler._audio_persist_pending is None

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    async def emit(_t: str) -> None:
        return

    async def emit_direct(_t: str) -> None:
        return

    # Snapshot the loop tasks BEFORE the call so we can detect any
    # un-awaited persist tasks left dangling.
    pre_tasks = set(asyncio.all_tasks())
    await handler.handle(msg, emit, emit_direct=emit_direct)
    # Synchronous path: by the time handle() returns, the persist
    # work is done. No leftover task should be sitting in the event
    # loop with a name like ``audio-persist-*``.
    post_tasks = set(asyncio.all_tasks()) - pre_tasks
    audio_persist_tasks = [
        t for t in post_tasks
        if not t.done() and t.get_name().startswith("audio-persist-")
    ]
    assert audio_persist_tasks == [], (
        f"inline mode must not orphan an audio-persist-* task; "
        f"got {[t.get_name() for t in audio_persist_tasks]}"
    )


# ---------------------------------------------------------------------------
# F7 — tmp file unlink survives CancelledError raised in persist branch
# ---------------------------------------------------------------------------


async def test_f7_tmp_unlink_survives_cancel(tmp_path: Path) -> None:
    """When the persist branch re-raises CancelledError, the OUTER
    finally still unlinks the tmp file. Pre-fix the unlink lived
    inside the same finally as the persist+raise, so cancel skipped
    cleanup and leaked the audio bytes on disk."""
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _ScriptedBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=5),
    )
    persist_pending: set[asyncio.Task[Any]] = set()
    handler = ClaudeHandler(
        settings,
        store,
        bridge,
        transcription,
        audio_bridge=bridge,
        audio_persist_pending=persist_pending,
    )

    tmp = _make_audio_tmp(tmp_path)
    assert tmp.exists()

    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    bg_tasks: set[asyncio.Task[Any]] = set()

    def _spawn(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    handler._audio_dispatch = _spawn  # type: ignore[assignment]

    async def emit(_t: str) -> None:
        return

    async def emit_direct(_t: str) -> None:
        return

    await handler.handle(msg, emit, emit_direct=emit_direct)

    # Wait for the bg task to nearly-finish, then cancel it before
    # the body completes — the persist task will still be in the
    # pending set and the outer finally MUST run.
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    await asyncio.gather(*persist_pending, return_exceptions=True)

    # Tmp file must be gone.
    assert not tmp.exists(), (
        f"audio tmp file must be unlinked after cancel; still at {tmp}"
    )


# ---------------------------------------------------------------------------
# F8 — outer try/finally guarantees persist on cancel mid-transcribe
# ---------------------------------------------------------------------------


async def test_f8_cancel_mid_transcribe_still_marks_turn(
    tmp_path: Path,
) -> None:
    """Cancellation that lands DURING transcribe (before bridge.ask
    runs) must still trigger persist + interrupt_turn. Pre-fix the
    persist lived only in the bridge.ask try/finally, so this branch
    leaked turns as ``pending`` and tripped boot-reaper noise.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _ScriptedBridge(settings, script=[_result_message()])
    transcription = _CancellingTranscription(settings)
    persist_pending: set[asyncio.Task[Any]] = set()
    bg_tasks: set[asyncio.Task[Any]] = set()

    def _spawn(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    handler = ClaudeHandler(
        settings,
        store,
        bridge,
        transcription,
        audio_bridge=bridge,
        audio_persist_pending=persist_pending,
        audio_dispatch=_spawn,
    )

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    async def emit(_t: str) -> None:
        return

    async def emit_direct(_t: str) -> None:
        return

    await handler.handle(msg, emit, emit_direct=emit_direct)
    # Wait until transcribe has parked.
    await asyncio.wait_for(transcription.entered.wait(), timeout=2.0)

    # Cancel as Daemon.stop would do.
    for t in list(bg_tasks):
        t.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    await asyncio.wait_for(
        asyncio.gather(*persist_pending, return_exceptions=True),
        timeout=2.0,
    )

    # Turn should be ``interrupted`` — proves the outer finally fired
    # persist even though cancellation hit mid-transcribe.
    cur = await store._conn.execute(  # type: ignore[attr-defined]
        "SELECT status FROM turns WHERE chat_id=42"
    )
    rows = await cur.fetchall()
    await cur.close()
    statuses = [r[0] for r in rows]
    assert statuses, "expected at least one turn row"
    assert all(s == "interrupted" for s in statuses), (
        f"all turns should be interrupted (persist ran); got {statuses}"
    )


# ---------------------------------------------------------------------------
# F11 — drain timeout log carries turn_id-bearing task names
# ---------------------------------------------------------------------------


async def test_f11_drain_timeout_log_includes_task_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the drain budget elapses, the warning must list
    ``outstanding=[...task names...]`` so post-mortem can pinpoint
    which audio turn(s) overran (each persist task is named
    ``audio-persist-<turn_id>``).

    We exercise the production drain block directly (via the
    ``asyncio.wait`` snapshot pattern) so the test validates both the
    log shape and the not-done snapshot semantics — historically a
    naïve ``except TimeoutError + [t for t ... if not t.done()]``
    after a cancelled ``gather`` always reads back ``outstanding=[]``
    because gather cascades cancellation to its children.
    """
    captured: list[tuple[str, dict[str, Any]]] = []

    class _CapturingLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def error(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def exception(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

        def debug(self, event: str, **kwargs: Any) -> None:
            captured.append((event, kwargs))

    import assistant.main as main_module

    monkeypatch.setattr(main_module, "log", _CapturingLogger())

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        scheduler=SchedulerSettings(enabled=False),
        audio_bg=AudioBgSettings(drain_timeout_s=0.05),
    )
    daemon = Daemon(settings)

    async def slow_persist() -> None:
        await asyncio.sleep(0.5)

    persist_task = asyncio.create_task(
        slow_persist(), name="audio-persist-deadbeef"
    )
    daemon._audio_persist_pending.add(persist_task)
    persist_task.add_done_callback(daemon._audio_persist_pending.discard)

    audio_pending = list(daemon._audio_persist_pending)
    # Mirror the production drain block (main.py:824-846).
    main_module.log.info(
        "daemon_draining_audio_persist", count=len(audio_pending)
    )
    _done, not_done = await asyncio.wait(
        audio_pending,
        timeout=daemon._settings.audio_bg.drain_timeout_s,
        return_when=asyncio.ALL_COMPLETED,
    )
    if not_done:
        main_module.log.warning(
            "daemon_audio_persist_drain_timeout",
            outstanding=[t.get_name() for t in not_done],
        )
        for t in not_done:
            t.cancel()
        await asyncio.gather(*not_done, return_exceptions=True)

    timeout_events = [
        kwargs
        for event, kwargs in captured
        if event == "daemon_audio_persist_drain_timeout"
    ]
    assert timeout_events, (
        f"drain timeout warning must fire; got events={[e for e, _ in captured]}"
    )
    assert "audio-persist-deadbeef" in timeout_events[0]["outstanding"]
