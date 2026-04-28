"""Phase 6c — ClaudeHandler audio-branch unit tests.

Covers:

- short voice (≤120s, empty caption) → intent prefix injected;
- long voice (>120s, empty caption) → auto-summary prompt;
- non-empty caption → caption + transcript combined;
- save trigger ("сохрани") in caption → no auto-vault-save;
- duration > 3h → reject with Russian reply, NO bridge call;
- audio file (m4a) → marker source = "audio" + filename in marker;
- URL extraction → marker prefix "voice-url:", extract_url called;
- vault_path persisted in marker on success;
- timeout_override=settings.claude_voice_timeout passed to bridge;
- Mac sidecar offline → quarantine + Russian reply, NO bridge call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.services.transcription import (
    TranscriptionError,
    TranscriptionResult,
    TranscriptionService,
)
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


class _CapturingBridge(ClaudeBridge):
    """Records each ask() invocation; replays a scripted response."""

    def __init__(
        self,
        settings: Settings,
        script: list[Any] | Exception,
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
        if isinstance(self._script, Exception):
            raise self._script
        for item in self._script:
            yield item


class _StubTranscription(TranscriptionService):
    """TranscriptionService that returns a scripted result.

    Phase 6c F9 (fix-pack): the handler now prefers ``transcribe_file``
    (streaming from disk) over ``transcribe`` (bytes-in-RAM). The stub
    overrides BOTH methods so tests written before the fix-pack still
    pass without churn.
    """

    def __init__(
        self,
        settings: Settings,
        result: TranscriptionResult | TranscriptionError,
        *,
        healthy: bool = True,
    ) -> None:
        super().__init__(settings)
        self._result = result
        self._healthy = healthy
        self.transcribe_calls: list[tuple[bytes, str, str]] = []
        self.transcribe_file_calls: list[tuple[Path, str, str]] = []
        self.extract_calls: list[str] = []

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return True

    async def health_check(self) -> bool:  # type: ignore[override]
        return self._healthy

    async def transcribe(  # type: ignore[override]
        self, audio_bytes: bytes, mime_type: str, filename: str
    ) -> TranscriptionResult:
        self.transcribe_calls.append((audio_bytes, mime_type, filename))
        if isinstance(self._result, TranscriptionError):
            raise self._result
        return self._result

    async def transcribe_file(  # type: ignore[override]
        self, audio_path: Path, mime_type: str, filename: str
    ) -> TranscriptionResult:
        self.transcribe_file_calls.append((audio_path, mime_type, filename))
        if isinstance(self._result, TranscriptionError):
            raise self._result
        return self._result

    async def extract_url(self, url: str) -> TranscriptionResult:  # type: ignore[override]
        self.extract_calls.append(url)
        if isinstance(self._result, TranscriptionError):
            raise self._result
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


def _make_audio_tmp(
    tmp_path: Path, filename: str = "voice.ogg", payload: bytes = b"OggS audio bytes"
) -> Path:
    """Drop a fake audio file under uploads_dir."""
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / filename
    p.write_bytes(payload)
    return p


def _make_emit() -> tuple[list[str], Any]:
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    return chunks, emit


# ----------------------------------------------------------------------
# Short voice + empty caption → intent prefix
# ----------------------------------------------------------------------


async def test_short_voice_empty_caption_uses_intent_prefix(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("ок"), _result_message()],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="когда у тебя следующий созвон?",
            language="ru",
            duration=15.0,
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=15,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)

    assert len(bridge.calls) == 1
    user_text = bridge.calls[0]["user_text"]
    assert "[голосовое от owner" in user_text
    assert "когда у тебя следующий созвон" in user_text


# ----------------------------------------------------------------------
# Long voice + empty caption → auto-summary
# ----------------------------------------------------------------------


async def test_long_voice_empty_caption_uses_summary_prompt(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("саммари: важные тезисы..."), _result_message()],
    )
    transcript_text = "длинный транскрипт " * 50
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text=transcript_text,
            language="ru",
            duration=600.0,  # 10 min
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)

    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=2,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=600,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)

    user_text = bridge.calls[0]["user_text"]
    assert "Сделай краткое саммари" in user_text
    assert transcript_text.strip() in user_text


# ----------------------------------------------------------------------
# Non-empty caption → combined
# ----------------------------------------------------------------------


async def test_caption_plus_transcript_combined(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="my voice content", language="ru", duration=10),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=3,
        text="переведи на английский",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=10,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    user_text = bridge.calls[0]["user_text"]
    assert user_text.startswith("переведи на английский")
    assert "my voice content" in user_text


# ----------------------------------------------------------------------
# Save trigger in caption → no auto-vault-save
# ----------------------------------------------------------------------


async def test_save_trigger_disables_auto_vault(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        # 5-min duration would normally trigger auto-vault-save
        TranscriptionResult(text="long body" * 30, language="ru", duration=300),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=4,
        text="сохрани в проекты",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=300,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)

    # No vault file written.
    assert not any((settings.vault_dir).glob("**/*.md"))
    # Marker indicates the deferred save.
    rows = await store.load_recent(42, 10)
    user_text_blocks = [r for r in rows if r["role"] == "user"]
    user_text = " ".join(
        b.get("text", "")
        for r in user_text_blocks
        for b in r["content"]
        if b.get("type") == "text"
    )
    assert "saved by user-request" in user_text


# ----------------------------------------------------------------------
# 3-hour cap reject
# ----------------------------------------------------------------------


async def test_three_hour_cap_rejects(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("never"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="too long",
            language="ru",
            duration=4 * 3600,  # 4 hours
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=5,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=4 * 3600,
        audio_mime_type="audio/ogg",
    )
    chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    assert bridge.calls == []  # bridge NEVER called
    assert any("слишком длинная" in c for c in chunks)


# ----------------------------------------------------------------------
# claude_voice_timeout passed to bridge
# ----------------------------------------------------------------------


async def test_voice_timeout_override_propagated(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=5),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=6,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=5,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    assert bridge.calls[0]["timeout_override"] == 900


# ----------------------------------------------------------------------
# audio file (m4a) source kind
# ----------------------------------------------------------------------


async def test_audio_file_marker_uses_audio_prefix(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("ok"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="meeting transcript", language="ru", duration=2400
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path, filename="memo.m4a")
    msg = IncomingMessage(
        chat_id=42,
        message_id=7,
        text="",
        attachment=tmp,
        attachment_kind="m4a",
        attachment_filename="memo.m4a",
        audio_duration=2400,
        audio_mime_type="audio/mp4",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    rows = await store.load_recent(42, 10)
    user_blocks = [
        b
        for r in rows if r["role"] == "user"
        for b in r["content"] if b.get("type") == "text"
    ]
    user_text = "\n".join(b.get("text", "") for b in user_blocks)
    assert "[audio: memo.m4a" in user_text


# ----------------------------------------------------------------------
# URL extraction → voice-url marker
# ----------------------------------------------------------------------


async def test_url_extraction_marker_prefix(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("саммари"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="podcast text", language="ru", duration=1800),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    msg = IncomingMessage(
        chat_id=42,
        message_id=8,
        text="",
        url_for_extraction="https://example.com/podcast/episode",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    assert transcription.extract_calls == ["https://example.com/podcast/episode"]
    rows = await store.load_recent(42, 10)
    user_blocks = [
        b
        for r in rows if r["role"] == "user"
        for b in r["content"] if b.get("type") == "text"
    ]
    user_text = "\n".join(b.get("text", "") for b in user_blocks)
    assert "[voice-url:" in user_text


# ----------------------------------------------------------------------
# Mac offline → quarantine + Russian reply
# ----------------------------------------------------------------------


async def test_transcription_error_quarantines_audio(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("never"), _result_message()]
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionError("транскрипция временно недоступна"),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=9,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=15,
        audio_mime_type="audio/ogg",
    )
    chunks, emit = _make_emit()
    await handler.handle(msg, emit)

    assert bridge.calls == []
    assert any("недоступна" in c for c in chunks)
    quarantine = settings.uploads_dir / ".failed"
    # File quarantined (rename moves it; original tmp path is gone).
    assert not tmp.exists()
    assert quarantine.is_dir()
    assert any(quarantine.iterdir())


# ----------------------------------------------------------------------
# Auto-vault save persists vault marker
# ----------------------------------------------------------------------


async def test_auto_vault_save_marker_includes_vault_path(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("ключевые тезисы"), _result_message()],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="длинная запись на большом промежутке времени" * 5,
            language="ru",
            duration=600,
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=10,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=600,
        audio_mime_type="audio/ogg",
    )
    _chunks, emit = _make_emit()
    await handler.handle(msg, emit)

    # Vault file present.
    saved = list((settings.vault_dir / "inbox").glob("transcript-*.md"))
    assert saved, f"vault note expected, found {list(settings.vault_dir.rglob('*'))}"
    rows = await store.load_recent(42, 10)
    user_blocks = [
        b
        for r in rows if r["role"] == "user"
        for b in r["content"] if b.get("type") == "text"
    ]
    user_text = "\n".join(b.get("text", "") for b in user_blocks)
    assert "vault: " in user_text


# ----------------------------------------------------------------------
# Health-fail at handler level → quarantine reply
# ----------------------------------------------------------------------


async def test_disabled_transcription_yields_offline_reply(
    tmp_path: Path,
) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings, script=[_text_block("never"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge, transcription=None)
    tmp = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=11,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=10,
        audio_mime_type="audio/ogg",
    )
    chunks, emit = _make_emit()
    await handler.handle(msg, emit)
    assert bridge.calls == []
    assert any("недоступна" in c for c in chunks)
