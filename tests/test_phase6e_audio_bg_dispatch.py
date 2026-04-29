"""Phase 6e — bg dispatch invariants.

Three checks:

- Handler dispatch returns within ~50 ms; the heavy work runs
  asynchronously in the daemon-tracked bg coroutine.
- The bg coroutine completes the turn (transcribe + bridge.ask +
  marker persist) and ``emit_direct`` delivers the model output.
- Cancellation (``Daemon.stop`` mid-bg-task) leaves the turn marked
  ``interrupted`` so the boot reaper does not pick it up again.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.services.transcription import (
    TranscriptionResult,
    TranscriptionService,
)
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _CapturingBridge(ClaudeBridge):
    def __init__(
        self,
        settings: Settings,
        script: list[Any],
    ) -> None:
        super().__init__(settings)
        self._script = script
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "history": history,
                "system_notes": system_notes,
                "image_blocks": image_blocks,
                "timeout_override": timeout_override,
            }
        )
        for item in self._script:
            yield item


class _SlowBridge(ClaudeBridge):
    """Bridge that suspends in the middle of streaming so the test
    can race a cancel against an in-flight bg task."""

    def __init__(
        self, settings: Settings, gate: asyncio.Event, never_finish: asyncio.Event
    ) -> None:
        super().__init__(settings)
        self._gate = gate
        self._never_finish = never_finish

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
        # Park here so the outer test can cancel us mid-flight.
        self._gate.set()
        await self._never_finish.wait()
        # unreachable in the cancel test; placate the type checker.
        if False:  # pragma: no cover
            yield None


class _StubTranscription(TranscriptionService):
    def __init__(
        self,
        settings: Settings,
        result: TranscriptionResult,
    ) -> None:
        super().__init__(settings)
        self._result = result

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return True

    async def health_check(self) -> bool:  # type: ignore[override]
        return True

    async def transcribe_file(  # type: ignore[override]
        self, audio_path: Path, mime_type: str, filename: str
    ) -> TranscriptionResult:
        return self._result


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
        whisper_api_url="http://mac.test:9000",
        whisper_api_token="x" * 32,
        claude_voice_timeout=900,
        voice_vault_threshold_seconds=120,
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "handler.db"
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


def _spawner(
    bg_tasks: set[asyncio.Task[Any]],
) -> Callable[[Coroutine[Any, Any, None]], None]:
    """Return an audio_dispatch callable that mirrors Daemon._spawn_bg.

    Tracks the spawned tasks so the test can ``await`` them after the
    handler returns.
    """

    def _fn(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        bg_tasks.add(task)
        task.add_done_callback(bg_tasks.discard)

    return _fn


async def test_dispatch_returns_quickly_and_bg_completes(
    tmp_path: Path,
) -> None:
    """``handler.handle`` returns within ~50 ms; the bg task does the
    transcribe + bridge.ask + persist work; ``emit_direct`` carries
    the final reply text to the owner.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    audio_bridge = _CapturingBridge(
        settings, script=[_text_block("ответ"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="how do you spell my name?", language="ru", duration=10),
    )

    bg_tasks: set[asyncio.Task[Any]] = set()
    persist_pending: set[asyncio.Task[Any]] = set()
    audio_bg_sem = asyncio.Semaphore(1)
    handler = ClaudeHandler(
        settings,
        store,
        audio_bridge,  # use audio_bridge as the only bridge for this test
        transcription,
        audio_bridge=audio_bridge,
        audio_bg_sem=audio_bg_sem,
        audio_dispatch=_spawner(bg_tasks),
        audio_persist_pending=persist_pending,
    )

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=10,
        audio_mime_type="audio/ogg",
    )

    captured: list[str] = []

    async def emit(_text: str) -> None:
        # Lock-time channel — must NOT receive the bg-time output.
        return

    async def emit_direct(text: str) -> None:
        captured.append(text)

    t0 = time.perf_counter()
    await handler.handle(msg, emit, emit_direct=emit_direct)
    dispatch_ms = (time.perf_counter() - t0) * 1000

    # The dispatch path should be fast: lock-only work is start_turn
    # (one INSERT), invariant asserts, and an asyncio.create_task.
    # Production target is <50 ms; 500 ms is CI-runner slack to absorb
    # cold sqlite opens, GC pauses, and noisy-neighbour effects on a
    # shared CI executor.
    assert dispatch_ms < 500, f"dispatch took {dispatch_ms:.1f}ms (>500ms)"

    # Drain the bg task so the test can observe final state.
    await asyncio.gather(*bg_tasks, return_exceptions=True)
    # And the persist task.
    await asyncio.gather(*persist_pending, return_exceptions=True)

    assert captured == ["ответ"], (
        f"expected emit_direct to deliver 'ответ', got {captured!r}"
    )
    assert len(audio_bridge.calls) == 1
    assert audio_bridge.calls[0]["timeout_override"] == 900

    # Turn marked complete in the conversations store.
    rows = await store.load_recent(42, 5)
    assert any(r["role"] == "user" for r in rows)


async def test_cancellation_marks_turn_interrupted(
    tmp_path: Path,
) -> None:
    """When ``Daemon.stop`` cancels the bg task, the turn must be
    transitioned out of ``pending`` so ``cleanup_orphan_pending_turns``
    on the next boot is not over-broad. The cancel propagates through
    the bg task's ``finally`` which schedules a persist task; that
    persist task lands an ``interrupt_turn`` even when shielded
    against the cancel.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    gate = asyncio.Event()
    never_finish = asyncio.Event()
    audio_bridge = _SlowBridge(settings, gate, never_finish)
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hello", language="ru", duration=5),
    )

    bg_tasks: set[asyncio.Task[Any]] = set()
    persist_pending: set[asyncio.Task[Any]] = set()
    handler = ClaudeHandler(
        settings,
        store,
        audio_bridge,
        transcription,
        audio_bridge=audio_bridge,
        audio_bg_sem=asyncio.Semaphore(1),
        audio_dispatch=_spawner(bg_tasks),
        audio_persist_pending=persist_pending,
    )

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=2,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )

    async def emit(_text: str) -> None:
        return

    async def emit_direct(_text: str) -> None:
        return

    await handler.handle(msg, emit, emit_direct=emit_direct)
    # Wait until the bg task is parked inside _SlowBridge.ask.
    await asyncio.wait_for(gate.wait(), timeout=2.0)

    # Cancel the bg task as Daemon.stop would do.
    for t in list(bg_tasks):
        t.cancel()
    await asyncio.gather(*bg_tasks, return_exceptions=True)

    # The persist_task is shielded inside _run_audio_job; drain it.
    await asyncio.wait_for(
        asyncio.gather(*persist_pending, return_exceptions=True),
        timeout=2.0,
    )

    # Turn should be interrupted (not pending).
    cur = await store._conn.execute(  # type: ignore[attr-defined]
        "SELECT status FROM turns WHERE chat_id=42"
    )
    rows = await cur.fetchall()
    await cur.close()
    statuses = [r[0] for r in rows]
    assert statuses, "expected at least one turn row"
    assert all(s == "interrupted" for s in statuses), (
        f"all turns should be interrupted after cancel; got {statuses}"
    )


async def test_inline_fallback_runs_synchronously(tmp_path: Path) -> None:
    """When ``audio_dispatch`` is None (no daemon attached — the test
    fallback), ``handler.handle`` runs the bg job inline so existing
    phase-6c suites keep their synchronous expectations.

    This is a guard-rail: if a refactor accidentally changes the inline
    fallback to deferred dispatch, every 6c test using the older
    ``await handler.handle(msg, emit)`` shape would silently regress.
    """
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=8),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)

    tmp = _make_audio_tmp(tmp_path, name="voice.ogg")
    msg = IncomingMessage(
        chat_id=42,
        message_id=3,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=8,
        audio_mime_type="audio/ogg",
    )
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    # ``emit_direct`` left None — the inline fallback should reuse
    # the lock-time emit so the test still observes "ok".
    await handler.handle(msg, emit)

    # Bridge was actually called (no deferred work pending).
    assert len(bridge.calls) == 1
    assert chunks == ["ok"]


# Re-export the type alias to silence "unused" complaints in static
# analysis when only the imports are scanned.
_ = Awaitable
