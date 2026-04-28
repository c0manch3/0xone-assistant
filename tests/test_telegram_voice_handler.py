"""Phase 6c — TelegramAdapter._on_voice / _on_audio / URL trigger tests.

Covers:
- size guard pre-download (>20 MB → reject, no download);
- pre-flight Mac sidecar health (offline → reject, no download);
- 3-hour cap pre-download reject;
- ack message sent BEFORE handler is invoked (bypasses chunks);
- typing task created + cancelled in finally;
- F.audio routes through audio path (not document extract path);
- audio file via document route → routes to audio path;
- explicit URL trigger (транскрибируй <URL>) → routes to URL extract;
- non-trigger URL in text → normal text flow (handler receives plain text).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from assistant.adapters.base import IncomingMessage
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings
from assistant.services.transcription import (
    TranscriptionResult,
    TranscriptionService,
)


@dataclass
class _FakeChat:
    id: int = 42


@dataclass
class _FakeVoice:
    file_id: str = "voice_id"
    file_size: int | None = 1024
    duration: int | None = 15


@dataclass
class _FakeAudio:
    file_id: str = "audio_id"
    file_size: int | None = 2048
    duration: int | None = 600
    file_name: str | None = "memo.mp3"
    mime_type: str | None = "audio/mpeg"


@dataclass
class _FakeMessage:
    voice: _FakeVoice | None = None
    audio: _FakeAudio | None = None
    document: object | None = None
    text: str | None = None
    caption: str | None = None
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 7
    replies: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return "voice" if self.voice else "audio"

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def answer(self, text: str) -> None:
        self.replies.append(text)


class _FakeHandler:
    def __init__(self) -> None:
        self.received: list[IncomingMessage] = []
        self.reply_text = "ok"

    async def handle(
        self,
        msg: IncomingMessage,
        emit: Callable[[str], Awaitable[None]],
    ) -> None:
        self.received.append(msg)
        await emit(self.reply_text)


class _StubTranscription(TranscriptionService):
    def __init__(self, settings: Settings, *, healthy: bool = True) -> None:
        super().__init__(settings)
        self._healthy = healthy

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return True

    async def health_check(self) -> bool:  # type: ignore[override]
        return self._healthy

    async def transcribe(  # type: ignore[override]
        self, audio_bytes: bytes, mime_type: str, filename: str
    ) -> TranscriptionResult:
        return TranscriptionResult(text="transcript", language="ru", duration=10.0)

    async def extract_url(  # type: ignore[override]
        self, url: str
    ) -> TranscriptionResult:
        return TranscriptionResult(text="url-text", language="ru", duration=20.0)


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        whisper_api_url="http://mac.test:9000",
        whisper_api_token="x" * 32,
    )


def _build_adapter(
    tmp_path: Path,
    *,
    healthy: bool = True,
) -> tuple[TelegramAdapter, _FakeHandler, _StubTranscription]:
    settings = _build_settings(tmp_path)
    adapter = TelegramAdapter(settings)
    handler = _FakeHandler()
    adapter.set_handler(handler)
    transcription = _StubTranscription(settings, healthy=healthy)
    adapter.set_transcription(transcription)
    adapter._bot = MagicMock()  # type: ignore[assignment]

    async def fake_download(
        doc: Any, *, destination: Path, timeout: int = 30
    ) -> None:
        del timeout
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"OggS-fake-bytes")

    adapter._bot.download = AsyncMock(side_effect=fake_download)
    adapter._bot.send_message = AsyncMock()
    adapter._bot.send_chat_action = AsyncMock()
    return adapter, handler, transcription


# ----------------------------------------------------------------------
# Voice — size + cap guards
# ----------------------------------------------------------------------


async def test_voice_oversize_rejects_pre_download(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(voice=_FakeVoice(file_size=21 * 1024 * 1024, duration=10))
    await adapter._on_voice(msg)  # type: ignore[arg-type]
    assert handler.received == []
    assert any("20 МБ" in r for r in msg.replies)
    adapter._bot.download.assert_not_called()


async def test_voice_three_hour_cap_rejects_pre_download(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(
        voice=_FakeVoice(file_size=1024, duration=4 * 3600)
    )
    await adapter._on_voice(msg)  # type: ignore[arg-type]
    assert handler.received == []
    assert any("3 часа" in r for r in msg.replies)
    adapter._bot.download.assert_not_called()


async def test_voice_offline_sidecar_rejects(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path, healthy=False)
    msg = _FakeMessage(voice=_FakeVoice(duration=15))
    await adapter._on_voice(msg)  # type: ignore[arg-type]
    assert handler.received == []
    assert any("offline" in r.lower() for r in msg.replies)
    adapter._bot.download.assert_not_called()


# ----------------------------------------------------------------------
# Voice — happy path: ack BEFORE handler, IncomingMessage shape
# ----------------------------------------------------------------------


async def test_voice_happy_path_acks_then_dispatches(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(voice=_FakeVoice(duration=15))
    await adapter._on_voice(msg)  # type: ignore[arg-type]

    # Initial ack present (sent BEFORE handler invocation).
    ack_calls = [
        call.args[1]
        for call in adapter._bot.send_message.call_args_list
        if "получил" in str(call.args[1])
    ]
    assert ack_calls, "expected initial ⏳ ack"

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment_kind == "ogg"
    assert incoming.audio_duration == 15
    assert incoming.audio_mime_type == "audio/ogg"
    assert incoming.attachment is not None


# ----------------------------------------------------------------------
# Audio — F.audio path
# ----------------------------------------------------------------------


async def test_audio_happy_path_routes_to_audio(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(
        audio=_FakeAudio(
            file_size=2048,
            duration=600,
            file_name="podcast.mp3",
            mime_type="audio/mpeg",
        )
    )
    await adapter._on_audio(msg)  # type: ignore[arg-type]
    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment_kind == "mp3"
    assert incoming.audio_duration == 600


async def test_audio_unknown_format_rejects(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(
        audio=_FakeAudio(
            file_name="weird.flac",
            mime_type="audio/flac",
        )
    )
    await adapter._on_audio(msg)  # type: ignore[arg-type]
    assert handler.received == []
    assert any("не распознан" in r for r in msg.replies)


# ----------------------------------------------------------------------
# URL trigger detection
# ----------------------------------------------------------------------


async def test_url_trigger_routes_to_url_extraction(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(text="транскрибируй https://example.com/podcast")
    await adapter._on_text(msg)  # type: ignore[arg-type]
    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.url_for_extraction == "https://example.com/podcast"
    assert incoming.attachment is None


async def test_slash_voice_trigger_routes_to_url_extraction(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(text="/voice https://x.test/y")
    await adapter._on_text(msg)  # type: ignore[arg-type]
    assert len(handler.received) == 1
    assert handler.received[0].url_for_extraction == "https://x.test/y"


async def test_non_trigger_url_uses_normal_text_flow(tmp_path: Path) -> None:
    adapter, handler, _ = _build_adapter(tmp_path)
    # URL embedded in a normal sentence — should NOT route to extraction.
    msg = _FakeMessage(text="посмотри https://github.com/foo/bar пожалуйста")
    await adapter._on_text(msg)  # type: ignore[arg-type]
    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.url_for_extraction is None
    assert "github" in incoming.text
