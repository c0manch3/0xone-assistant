"""Phase 7 / Wave 11.C (commit 18i) — TelegramAdapter pre-flight
oversize rejection across all five media kinds.

Scope: the adapter must compare aiogram's `File.file_size` (nullable per
spike S-6) against the matching `MediaSettings.<kind>_max_bytes` cap
BEFORE calling `assistant.media.download.download_telegram_file(...)`.
Three boundary cases per kind:

  * `file_size > cap` → reject (`message.answer("Файл слишком большой.")`
    + NO call into `download_telegram_file`, therefore NO call into
    `Bot.download_file` either).
  * `file_size == cap` → accept (strict `>` comparison in the adapter;
    the equality case must flow through the download path).
  * `file_size is None` → the adapter cannot pre-flight (aiogram's
    S-6 nullable semantics). The download path IS exercised; the
    streaming `SizeCappedWriter` handles the cap at write-time
    (covered by `tests/test_media_download.py`, not here). For this
    file, we only assert the adapter does NOT pre-flight-reject a
    `None`-size payload — i.e. the download IS attempted.

Why mock both layers:
  The task spec asks for `Bot.download_file` mocking to observe the
  "was the network actually touched" question at its narrowest waist;
  monkey-patching `assistant.adapters.telegram.download_telegram_file`
  gives us the stronger "adapter never entered the download code
  path" assertion. Both are wired up per test so we can assert on
  whichever surface is relevant.

All five Telegram media kinds (voice / audio / photo / document /
video_note) have independent caps in `MediaSettings`; each kind is
covered so a future cap rewiring (or a regression that drops one
kind's pre-flight guard) fails loudly here rather than silently in
production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings

# ----------------------------------------------------------------------
# Shared minimum-viable aiogram surface (mirrors
# test_telegram_adapter_media_handlers.py so this test file stays
# self-contained — no cross-file fixture coupling).
# ----------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeVoice:
    def __init__(
        self,
        *,
        file_id: str = "vox-1",
        duration: int = 10,
        mime_type: str | None = "audio/ogg",
        file_size: int | None = 2048,
    ) -> None:
        self.file_id = file_id
        self.duration = duration
        self.mime_type = mime_type
        self.file_size = file_size


class _FakeAudio:
    def __init__(
        self,
        *,
        file_id: str = "aud-1",
        duration: int = 120,
        mime_type: str | None = "audio/mpeg",
        file_size: int | None = 100_000,
        file_name: str | None = "song.mp3",
    ) -> None:
        self.file_id = file_id
        self.duration = duration
        self.mime_type = mime_type
        self.file_size = file_size
        self.file_name = file_name


class _FakePhotoSize:
    def __init__(
        self,
        *,
        file_id: str = "pho-1",
        width: int = 1280,
        height: int = 720,
        file_size: int | None = 200_000,
    ) -> None:
        self.file_id = file_id
        self.width = width
        self.height = height
        self.file_size = file_size


class _FakeDocument:
    def __init__(
        self,
        *,
        file_id: str = "doc-1",
        file_name: str | None = "report.pdf",
        mime_type: str | None = "application/pdf",
        file_size: int | None = 50_000,
    ) -> None:
        self.file_id = file_id
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size


class _FakeVideoNote:
    def __init__(
        self,
        *,
        file_id: str = "vnote-1",
        duration: int = 15,
        file_size: int | None = 500_000,
    ) -> None:
        self.file_id = file_id
        self.duration = duration
        self.file_size = file_size


class _FakeMessage:
    def __init__(
        self,
        *,
        chat_id: int = 42,
        message_id: int = 1,
        caption: str | None = None,
        voice: _FakeVoice | None = None,
        audio: _FakeAudio | None = None,
        photo: list[_FakePhotoSize] | None = None,
        document: _FakeDocument | None = None,
        video_note: _FakeVideoNote | None = None,
    ) -> None:
        self.chat = _FakeChat(chat_id)
        self.message_id = message_id
        self.caption = caption
        self.voice = voice
        self.audio = audio
        self.photo = photo
        self.document = document
        self.video_note = video_note
        self.content_type = "unknown"
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class _RecordingHandler:
    """Captures dispatch. Tests on the reject path assert `received == []`;
    tests on the accept path assert a single dispatch landed with the
    expected attachment kind."""

    def __init__(self) -> None:
        self.received: list[Any] = []

    async def handle(self, msg: Any, emit: Any) -> None:
        self.received.append(msg)
        # Keep the reply empty so `_dispatch_to_handler` skips the
        # `send_text` tail — the tests in this file are about the
        # download-call observation, not about the reply round-trip.


def _patch_noop_typing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the aiogram ChatActionSender typing ctx manager.

    The adapter wraps `_dispatch_to_handler` in `ChatActionSender.typing(
    bot=..., chat_id=...)`; without patching, aiogram would attempt a
    real `sendChatAction` network call. We're testing pre-flight +
    download-call observation, so the typing indicator is irrelevant.
    """

    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def fake_typing(**_kwargs: Any) -> _NoopCtx:
        return _NoopCtx()

    monkeypatch.setattr("assistant.adapters.telegram.ChatActionSender.typing", fake_typing)


def _install_download_spies(
    monkeypatch: pytest.MonkeyPatch,
    adapter: TelegramAdapter,
    dest_root: Path,
) -> dict[str, Any]:
    """Install two independent observation surfaces:

      1. `download_telegram_file` (module-level in `adapters.telegram`):
         the adapter's direct dependency. The strongest "did we enter
         the download code path at all?" assertion lives on this spy.
      2. `Bot.download_file` (instance attribute, aiogram's primitive
         inside `download_telegram_file`): the task spec mandates this
         surface. In the accept path we assert both fired; in the
         reject path we assert neither fired.

    Returns a dict with:
      * `top_calls`: list of `(file_id, max_bytes)` for each
        `download_telegram_file` invocation (only populated on accept
        paths — it short-circuits via this stub without invoking aiogram).
      * `bot_calls`: list of `file_path` seen by `Bot.download_file`.
      * `result_paths`: list of paths the top-level stub returned.
    """
    top_calls: list[tuple[str, int]] = []
    bot_calls: list[str] = []
    result_paths: list[Path] = []

    dest_root.mkdir(parents=True, exist_ok=True)

    async def fake_download_telegram_file(
        bot: Any,
        file_id: str,
        dest_dir: Path,
        suggested_filename: str,
        *,
        max_bytes: int,
        timeout_s: int = 30,
    ) -> Path:
        del bot, timeout_s, suggested_filename
        top_calls.append((file_id, max_bytes))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = (dest_dir / f"{file_id}.bin").resolve()
        target.write_bytes(b"x" * 16)
        result_paths.append(target)
        return target

    async def fake_bot_download_file(file_path: str, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        bot_calls.append(file_path)

    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        fake_download_telegram_file,
    )
    monkeypatch.setattr(adapter._bot, "download_file", fake_bot_download_file)

    return {
        "top_calls": top_calls,
        "bot_calls": bot_calls,
        "result_paths": result_paths,
    }


# ----------------------------------------------------------------------
# Pre-flight reject tests — one per media kind.
# ----------------------------------------------------------------------


async def test_voice_oversize_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.voice_max_bytes
    voice = _FakeVoice(file_id="vox-big", file_size=cap + 1)
    msg = _FakeMessage(voice=voice)

    await adapter._on_voice(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == []
    assert spies["bot_calls"] == []
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


async def test_audio_oversize_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.audio_max_bytes
    audio = _FakeAudio(file_id="aud-big", file_size=cap + 1, file_name="mega.mp3")
    msg = _FakeMessage(audio=audio)

    await adapter._on_audio(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == []
    assert spies["bot_calls"] == []
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


async def test_photo_oversize_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.photo_download_max_bytes
    # Adapter picks `photo[-1]`; only the last variant's size matters.
    largest = _FakePhotoSize(file_id="pho-big", width=4096, height=4096, file_size=cap + 1)
    msg = _FakeMessage(photo=[largest])

    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == []
    assert spies["bot_calls"] == []
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


async def test_document_oversize_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.document_max_bytes
    document = _FakeDocument(file_id="doc-big", file_name="big.pdf", file_size=cap + 1)
    msg = _FakeMessage(document=document)

    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == []
    assert spies["bot_calls"] == []
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


async def test_video_note_oversize_rejected_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`video_note` has no dedicated cap — the adapter falls back to
    `document_max_bytes` (see `_on_video_note` comment). Over-cap
    payloads must still be rejected pre-flight."""
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.document_max_bytes
    vnote = _FakeVideoNote(file_id="vn-big", file_size=cap + 1)
    msg = _FakeMessage(video_note=vnote)

    await adapter._on_video_note(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == []
    assert spies["bot_calls"] == []
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


# ----------------------------------------------------------------------
# Boundary accept tests — `file_size == cap` must flow through.
# ----------------------------------------------------------------------


async def test_voice_at_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.voice_max_bytes
    voice = _FakeVoice(file_id="vox-edge", file_size=cap)
    msg = _FakeMessage(voice=voice)

    await adapter._on_voice(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == [("vox-edge", cap)]
    assert msg.answers == []
    assert len(handler.received) == 1
    (att,) = handler.received[0].attachments
    assert att.kind == "voice"
    assert att.telegram_file_id == "vox-edge"


async def test_audio_at_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.audio_max_bytes
    audio = _FakeAudio(file_id="aud-edge", file_size=cap, file_name="edge.mp3")
    msg = _FakeMessage(audio=audio)

    await adapter._on_audio(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == [("aud-edge", cap)]
    assert msg.answers == []
    assert len(handler.received) == 1
    (att,) = handler.received[0].attachments
    assert att.kind == "audio"


async def test_photo_at_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.photo_download_max_bytes
    largest = _FakePhotoSize(file_id="pho-edge", width=1920, height=1080, file_size=cap)
    msg = _FakeMessage(photo=[largest])

    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == [("pho-edge", cap)]
    assert msg.answers == []
    assert len(handler.received) == 1
    (att,) = handler.received[0].attachments
    assert att.kind == "photo"


async def test_document_at_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    cap = adapter._settings.media.document_max_bytes
    document = _FakeDocument(file_id="doc-edge", file_name="edge.pdf", file_size=cap)
    msg = _FakeMessage(document=document)

    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == [("doc-edge", cap)]
    assert msg.answers == []
    assert len(handler.received) == 1
    (att,) = handler.received[0].attachments
    assert att.kind == "document"


async def test_video_note_at_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    # video_note shares the document cap in the adapter.
    cap = adapter._settings.media.document_max_bytes
    vnote = _FakeVideoNote(file_id="vn-edge", file_size=cap)
    msg = _FakeMessage(video_note=vnote)

    await adapter._on_video_note(msg)  # type: ignore[arg-type]

    assert spies["top_calls"] == [("vn-edge", cap)]
    assert msg.answers == []
    assert len(handler.received) == 1
    (att,) = handler.received[0].attachments
    assert att.kind == "video_note"


# ----------------------------------------------------------------------
# None-size streaming-cap path — adapter cannot pre-flight, download IS
# attempted. SizeCappedWriter enforcement at write-time is covered by
# `tests/test_media_download.py` and is out of scope here.
# ----------------------------------------------------------------------


async def test_voice_none_size_falls_through_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    voice = _FakeVoice(file_id="vox-none", file_size=None)
    msg = _FakeMessage(voice=voice)

    await adapter._on_voice(msg)  # type: ignore[arg-type]

    # No pre-flight rejection UX.
    assert msg.answers == []
    # Download helper WAS called with the voice cap — the streaming
    # `SizeCappedWriter` enforces the cap during write (S-6 fallback).
    assert spies["top_calls"] == [("vox-none", adapter._settings.media.voice_max_bytes)]
    assert len(handler.received) == 1


async def test_audio_none_size_falls_through_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    audio = _FakeAudio(file_id="aud-none", file_size=None, file_name="unknown.mp3")
    msg = _FakeMessage(audio=audio)

    await adapter._on_audio(msg)  # type: ignore[arg-type]

    assert msg.answers == []
    assert spies["top_calls"] == [("aud-none", adapter._settings.media.audio_max_bytes)]
    assert len(handler.received) == 1


async def test_photo_none_size_falls_through_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    largest = _FakePhotoSize(file_id="pho-none", width=800, height=600, file_size=None)
    msg = _FakeMessage(photo=[largest])

    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert msg.answers == []
    assert spies["top_calls"] == [
        ("pho-none", adapter._settings.media.photo_download_max_bytes)
    ]
    assert len(handler.received) == 1


async def test_document_none_size_falls_through_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    document = _FakeDocument(file_id="doc-none", file_name="unknown.bin", file_size=None)
    msg = _FakeMessage(document=document)

    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert msg.answers == []
    assert spies["top_calls"] == [("doc-none", adapter._settings.media.document_max_bytes)]
    assert len(handler.received) == 1


async def test_video_note_none_size_falls_through_to_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]
    _patch_noop_typing(monkeypatch)
    spies = _install_download_spies(monkeypatch, adapter, tmp_path / "data" / "media" / "inbox")

    vnote = _FakeVideoNote(file_id="vn-none", file_size=None)
    msg = _FakeMessage(video_note=vnote)

    await adapter._on_video_note(msg)  # type: ignore[arg-type]

    assert msg.answers == []
    # Fallback cap is document_max_bytes for video_note.
    assert spies["top_calls"] == [("vn-none", adapter._settings.media.document_max_bytes)]
    assert len(handler.received) == 1
