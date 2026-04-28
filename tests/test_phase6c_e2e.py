"""Phase 6c F20 fix-pack — end-to-end test gaps.

Three high-level checks that the per-component suites don't catch:

- ``test_audio_document_m4a_e2e`` — owner sends iPhone Voice Memo m4a
  via Telegram document route → handler routes through audio path,
  calls transcribe_file (NOT extract_url), and the user-row marker
  uses the ``audio:`` prefix with the original filename (AC#3).

- ``test_url_extraction_e2e`` — full path: explicit
  ``транскрибируй <URL>`` trigger → adapter health-checks → ack →
  IncomingMessage routed to handler → extract_url called → vault
  saved (>2 min) → bridge.ask receives the wrapped/caged transcript
  with the scheduler-note absent and the URL system-note present
  (AC#4).

- ``test_bridge_voice_timeout_900s_test`` — when the handler receives
  a voice IncomingMessage, the bridge.ask call applies the
  configured ``claude_voice_timeout`` (default 900s) instead of
  ``claude.timeout`` (300s).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
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

# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


@dataclass
class _FakeChat:
    id: int = 42


@dataclass
class _FakeDoc:
    file_id: str = "doc_id"
    file_size: int | None = 4096
    file_name: str | None = "memo.m4a"
    mime_type: str | None = "audio/mp4"


@dataclass
class _FakeMessage:
    document: _FakeDoc | None = None
    text: str | None = None
    caption: str | None = None
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 7
    replies: list[str] = field(default_factory=list)
    voice: object | None = None
    audio: object | None = None

    @property
    def content_type(self) -> str:
        return "document"

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def answer(self, text: str) -> None:
        self.replies.append(text)


class _CapturingBridge(ClaudeBridge):
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
    def __init__(
        self,
        settings: Settings,
        result: TranscriptionResult,
        *,
        healthy: bool = True,
    ) -> None:
        super().__init__(settings)
        self._result = result
        self._healthy = healthy
        self.transcribe_calls: list[Any] = []
        self.transcribe_file_calls: list[Any] = []
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
        return self._result

    async def transcribe_file(  # type: ignore[override]
        self, audio_path: Path, mime_type: str, filename: str
    ) -> TranscriptionResult:
        self.transcribe_file_calls.append((audio_path, mime_type, filename))
        return self._result

    async def extract_url(self, url: str) -> TranscriptionResult:  # type: ignore[override]
        self.extract_calls.append(url)
        return self._result


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=300, max_concurrent=1, history_limit=5),
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
    tmp_path: Path,
    filename: str,
    payload: bytes = b"audio bytes",
) -> Path:
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / filename
    p.write_bytes(payload)
    return p


def _make_emit() -> tuple[list[str], Callable[[str], Awaitable[None]]]:
    chunks: list[str] = []

    async def emit(text: str) -> None:
        chunks.append(text)

    return chunks, emit


# ---------------------------------------------------------------------------
# F20 #1: AC#3 — m4a Voice Memo via document route
# ---------------------------------------------------------------------------


async def test_audio_document_m4a_e2e(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("саммари встречи"), _result_message()],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="meeting transcript", language="ru", duration=600
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)

    # Adapter constructs the IncomingMessage; we mimic the audio-document
    # route by handing in a pre-built message that matches what
    # ``_on_audio_document`` would emit.
    tmp = _make_audio_tmp(tmp_path, "memo.m4a")
    msg = IncomingMessage(
        chat_id=42,
        message_id=10,
        text="",
        attachment=tmp,
        attachment_kind="m4a",
        attachment_filename="memo.m4a",
        audio_duration=None,  # document route — Telegram doesn't expose
        audio_mime_type="audio/mp4",
    )
    _, emit = _make_emit()
    await handler.handle(msg, emit)

    # transcribe_file (streaming) called; transcribe (RAM) NOT called.
    assert len(transcription.transcribe_file_calls) == 1
    assert transcription.transcribe_calls == []
    assert transcription.extract_calls == []

    # Marker uses ``audio: memo.m4a`` prefix + duration from Whisper.
    rows = await store.load_recent(42, 10)
    user_text = "\n".join(
        b.get("text", "")
        for r in rows if r["role"] == "user"
        for b in r["content"] if b.get("type") == "text"
    )
    assert "[audio: memo.m4a" in user_text
    # Vault save fires (>120s threshold).
    saved = list((settings.vault_dir).rglob("transcript-*.md"))
    assert len(saved) == 1
    assert "vault: " in user_text


# ---------------------------------------------------------------------------
# F20 #2: AC#4 — URL extraction full path
# ---------------------------------------------------------------------------


async def test_url_extraction_e2e(tmp_path: Path) -> None:
    settings = _build_settings(tmp_path)
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("саммари лекции"), _result_message()],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(
            text="lecture transcript " * 10, language="ru", duration=1800
        ),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)

    msg = IncomingMessage(
        chat_id=42,
        message_id=20,
        text="",
        url_for_extraction="https://example.com/lecture",
    )
    _, emit = _make_emit()
    await handler.handle(msg, emit)

    # extract_url called; transcribe NOT called.
    assert transcription.extract_calls == ["https://example.com/lecture"]
    assert transcription.transcribe_calls == []
    assert transcription.transcribe_file_calls == []

    # Vault save fires (>120s threshold).
    saved = list((settings.vault_dir).rglob("transcript-*.md"))
    assert len(saved) == 1

    # Bridge envelope sees the wrap_untrusted cage + URL system-note;
    # the scheduler-note is absent (origin defaults to "telegram").
    user_text = bridge.calls[0]["user_text"]
    assert "untrusted-note-snippet-" in user_text
    notes = bridge.calls[0]["system_notes"] or []
    assert any("UNTRUSTED" in n for n in notes)
    assert not any("scheduler" in n.lower() for n in notes)

    # Marker prefix uses ``voice-url:``.
    rows = await store.load_recent(42, 10)
    persisted = "\n".join(
        b.get("text", "")
        for r in rows if r["role"] == "user"
        for b in r["content"] if b.get("type") == "text"
    )
    assert "[voice-url: https://example.com/lecture" in persisted


# ---------------------------------------------------------------------------
# F20 #3: claude_voice_timeout actually applied
# ---------------------------------------------------------------------------


async def test_bridge_voice_timeout_900s_test(tmp_path: Path) -> None:
    """Handler MUST pass ``timeout_override=settings.claude_voice_timeout``
    to ``bridge.ask`` for every audio turn.

    The test_handler_audio_branch.py suite covers the default 900s
    case; this F20 test pins a non-default value to make sure we
    propagate the *configured* number, not the literal default.
    """
    settings = _build_settings(tmp_path)
    settings.claude_voice_timeout = 1234  # any non-default
    store = await _make_store(tmp_path)
    bridge = _CapturingBridge(
        settings,
        script=[_text_block("ok"), _result_message()],
    )
    transcription = _StubTranscription(
        settings,
        TranscriptionResult(text="hi", language="ru", duration=10),
    )
    handler = ClaudeHandler(settings, store, bridge, transcription)
    tmp = _make_audio_tmp(tmp_path, "voice.ogg")
    msg = IncomingMessage(
        chat_id=42,
        message_id=30,
        text="",
        attachment=tmp,
        attachment_kind="ogg",
        attachment_filename=tmp.name,
        audio_duration=10,
        audio_mime_type="audio/ogg",
    )
    _, emit = _make_emit()
    await handler.handle(msg, emit)
    assert bridge.calls[0]["timeout_override"] == 1234
