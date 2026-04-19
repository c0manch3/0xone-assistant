"""Phase 7 / Wave 7A (commit 12) — `TelegramAdapter` media ingress +
out-of-band send path.

Covers the invariants spelled out in `plan/phase7/implementation.md`
§3.4 + §0 pitfalls #3, #13, #17 + §4.1 test matrix row #12:

  * `_on_photo` → download → `MediaAttachment` → `IncomingMessage`
    flows into the handler exactly once (happy path).
  * Attachment-ingress dedup (I-7.6, C-6): same `(chat_id, local_path)`
    fed twice within 60 s → handler invoked ONCE; second call is
    silently dropped with `log.debug("attachment_dedup")`.
  * `send_photo` honours `TelegramRetryAfter.retry_after` (L-21).
  * `send_document` re-raises `FileNotFoundError` after warning-log
    (L-20).
  * `send_audio` retries `TelegramNetworkError` twice then propagates
    (#13 exponential backoff budget).
  * Happy-path attachment construction for each of the five media
    kinds (voice / audio / photo / document / video_note).

All aiogram network calls are monkey-patched — the tests never touch
a real Bot or `getFile` endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings

# ----------------------------------------------------------------------
# Fixtures — minimum viable aiogram surface
# ----------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    """Build a Settings pointing at tmp_path for both project + data."""
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
    """Minimum aiogram Message surface exercised by media handlers.

    Implements `.chat`, `.caption`, `.message_id`, `.answer()`, and one
    of (`.voice` | `.audio` | `.photo` | `.document` | `.video_note`).
    Unused media fields default to None so the F.<kind> filter logic
    in the adapter sees only the one we populated.
    """

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
    """Captures every IncomingMessage the adapter dispatches.

    Emits a fixed reply so `_dispatch_to_handler`'s tail `send_text`
    exercises the standard retry-wrapper path. Test-setup decides
    whether to monkey-patch `Bot.send_message` or leave the default
    (the monkey-patch suite covers both).
    """

    def __init__(self, reply: str = "ok") -> None:
        self.received: list[IncomingMessage] = []
        self._reply = reply

    async def handle(self, msg: IncomingMessage, emit: Any) -> None:
        self.received.append(msg)
        if self._reply:
            await emit(self._reply)


def _patch_noop_io(
    adapter: TelegramAdapter, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[int, str]]:
    """Silence ChatActionSender + record send_message calls.

    Returns the list `send_message` was given so tests can assert the
    reply went out. Used by every test that feeds a handler a full
    turn; tests asserting NO handler dispatch can still call this and
    then assert the list stayed empty.
    """
    sends: list[tuple[int, str]] = []

    async def fake_send(**kwargs: Any) -> None:
        sends.append((kwargs["chat_id"], kwargs["text"]))

    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def fake_typing(**_kwargs: Any) -> _NoopCtx:
        return _NoopCtx()

    monkeypatch.setattr(adapter._bot, "send_message", fake_send)
    monkeypatch.setattr("assistant.adapters.telegram.ChatActionSender.typing", fake_typing)
    return sends


def _make_download_stub(dest_root: Path, *, fail: type[BaseException] | None = None) -> Any:
    """Return an async replacement for `download_telegram_file`.

    The stub writes a tiny payload under `dest_root` and returns the
    resolved path, mimicking the real helper's contract. `fail` lets
    a test force a failure (e.g. `SizeCapExceeded`) from the download
    layer.
    """

    dest_root.mkdir(parents=True, exist_ok=True)

    async def _stub(
        bot: Any,
        file_id: str,
        dest_dir: Path,
        suggested_filename: str,
        *,
        max_bytes: int,
        timeout_s: int = 30,
    ) -> Path:
        del bot, timeout_s
        if fail is not None:
            raise fail("forced by test")  # type: ignore[call-arg]
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Deterministic filename per file_id so dedup tests can
        # intentionally collide two ingresses on the same path.
        target = (dest_dir / f"{file_id}.bin").resolve()
        target.write_bytes(b"x" * 16)
        assert max_bytes > 0
        assert suggested_filename
        return target

    return _stub


# ----------------------------------------------------------------------
# Happy-path ingress tests — one per media kind
# ----------------------------------------------------------------------


async def test_on_voice_builds_media_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    voice = _FakeVoice(file_id="vox-happy", duration=7, mime_type="audio/ogg", file_size=3072)
    msg = _FakeMessage(voice=voice, caption="hey there")
    await adapter._on_voice(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.chat_id == 42
    assert incoming.text == "hey there"
    assert incoming.attachments is not None
    (att,) = incoming.attachments
    assert isinstance(att, MediaAttachment)
    assert att.kind == "voice"
    assert att.duration_s == 7
    assert att.file_size == 3072
    assert att.mime_type == "audio/ogg"
    assert att.telegram_file_id == "vox-happy"
    assert att.local_path.exists()


async def test_on_audio_builds_media_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    audio = _FakeAudio(file_id="aud-happy", duration=42, file_name="track.mp3")
    msg = _FakeMessage(audio=audio)
    await adapter._on_audio(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    att = handler.received[0].attachments
    assert att is not None
    assert att[0].kind == "audio"
    assert att[0].duration_s == 42
    assert att[0].filename_original == "track.mp3"
    assert att[0].telegram_file_id == "aud-happy"


async def test_on_photo_builds_media_attachment_from_largest_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram delivers PhotoSize[]; adapter must pick the largest (last)."""
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    thumb = _FakePhotoSize(file_id="pho-thumb", width=90, height=90, file_size=2_000)
    largest = _FakePhotoSize(file_id="pho-large", width=1920, height=1080, file_size=250_000)
    msg = _FakeMessage(photo=[thumb, largest])
    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    att = handler.received[0].attachments
    assert att is not None
    assert att[0].kind == "photo"
    assert att[0].width == 1920
    assert att[0].height == 1080
    assert att[0].telegram_file_id == "pho-large"
    assert att[0].mime_type == "image/jpeg"


async def test_on_document_builds_media_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    document = _FakeDocument(
        file_id="doc-happy",
        file_name="memo.txt",
        mime_type="text/plain",
        file_size=4096,
    )
    msg = _FakeMessage(document=document)
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    att = handler.received[0].attachments
    assert att is not None
    assert att[0].kind == "document"
    assert att[0].filename_original == "memo.txt"
    assert att[0].mime_type == "text/plain"
    assert att[0].file_size == 4096


async def test_on_video_note_builds_media_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    vnote = _FakeVideoNote(file_id="vn-happy", duration=9, file_size=300_000)
    msg = _FakeMessage(video_note=vnote)
    await adapter._on_video_note(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    att = handler.received[0].attachments
    assert att is not None
    assert att[0].kind == "video_note"
    assert att[0].duration_s == 9
    assert att[0].mime_type == "video/mp4"


# ----------------------------------------------------------------------
# Attachment-ingress dedup (C-6, I-7.6) — same (chat_id, local_path)
# fed twice within 60 s → handler invoked ONCE.
# ----------------------------------------------------------------------


async def test_attachment_ingress_dedup_within_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    # The stub filenames are deterministic on file_id, so feeding the
    # same file_id twice produces the same `local_path` — the dedup
    # key we care about.
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    photo = _FakePhotoSize(file_id="pho-dedup", width=800, height=600, file_size=50_000)
    msg1 = _FakeMessage(photo=[photo])
    msg2 = _FakeMessage(photo=[photo], message_id=2)

    await adapter._on_photo(msg1)  # type: ignore[arg-type]
    await adapter._on_photo(msg2)  # type: ignore[arg-type]

    # Second ingress MUST be deduped before reaching the handler.
    assert len(handler.received) == 1, (
        f"expected exactly one handler dispatch, got {len(handler.received)}: "
        f"I-7.6 (C-6) dedup on (chat_id, local_path) failed"
    )


async def test_attachment_dedup_different_chats_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedup key is `(chat_id, path)`; two chats sharing a path are
    independent dispatches (no collision).

    The adapter's `F.chat.id == owner_chat_id` filter usually blocks
    foreign chats at the router, but the dedup key must still be
    chat-qualified so a multi-owner future (or scheduler-origin
    `chat_id != owner`) does not accidentally cross-dedup.
    """
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    photo = _FakePhotoSize(file_id="pho-multi", width=800, height=600, file_size=50_000)
    await adapter._on_photo(_FakeMessage(chat_id=42, photo=[photo]))  # type: ignore[arg-type]
    await adapter._on_photo(_FakeMessage(chat_id=99, photo=[photo]))  # type: ignore[arg-type]

    assert len(handler.received) == 2
    assert {m.chat_id for m in handler.received} == {42, 99}


async def test_attachment_dedup_ttl_expiry_allows_re_emit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advance `time.monotonic` past the 60 s TTL → previous dedup key
    expires → same path fed again flows through to the handler."""
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    monkeypatch.setattr(
        "assistant.adapters.telegram.download_telegram_file",
        _make_download_stub(tmp_path / "data" / "media" / "inbox"),
    )

    fake_clock = {"t": 1000.0}

    def fake_monotonic() -> float:
        return fake_clock["t"]

    monkeypatch.setattr("assistant.adapters.telegram.time.monotonic", fake_monotonic)

    photo = _FakePhotoSize(file_id="pho-ttl", width=800, height=600, file_size=50_000)

    await adapter._on_photo(_FakeMessage(photo=[photo]))  # type: ignore[arg-type]
    # Still within 60 s — second ingress dropped.
    fake_clock["t"] += 30
    await adapter._on_photo(_FakeMessage(photo=[photo]))  # type: ignore[arg-type]
    # Past the TTL — third ingress re-emits.
    fake_clock["t"] += 61
    await adapter._on_photo(_FakeMessage(photo=[photo]))  # type: ignore[arg-type]

    assert len(handler.received) == 2


# ----------------------------------------------------------------------
# Oversize rejection — pre-flight (adapter-level) caps
# ----------------------------------------------------------------------


async def test_oversize_document_rejected_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _RecordingHandler()
    adapter.set_handler(handler)  # type: ignore[arg-type]

    _patch_noop_io(adapter, monkeypatch)
    # If the download is attempted, this stub raises; the test
    # passes iff the adapter never gets here (pre-flight caught it).
    called = {"n": 0}

    async def should_not_run(*_args: Any, **_kwargs: Any) -> Path:
        called["n"] += 1
        raise AssertionError("download called despite pre-flight oversize")

    monkeypatch.setattr("assistant.adapters.telegram.download_telegram_file", should_not_run)

    cap = adapter._settings.media.document_max_bytes
    huge = _FakeDocument(file_id="doc-big", file_name="big.pdf", file_size=cap + 1)
    msg = _FakeMessage(document=huge)
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert called["n"] == 0
    assert msg.answers == ["Файл слишком большой."]
    assert handler.received == []


# ----------------------------------------------------------------------
# send_photo — TelegramRetryAfter honoured (L-21).
# ----------------------------------------------------------------------


async def test_send_photo_retry_after_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))

    artefact = tmp_path / "photo.jpg"
    artefact.write_bytes(b"\xff\xd8\xff\xd9")  # minimal JPEG SOI/EOI

    calls: list[str] = []
    attempt_counter = {"n": 0}

    async def flaky_send(**kwargs: Any) -> None:
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            raise TelegramRetryAfter(method=None, message="slow", retry_after=3)  # type: ignore[arg-type]
        calls.append(kwargs["caption"] or "")

    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(adapter._bot, "send_photo", flaky_send)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    await adapter.send_photo(42, artefact, caption="hello")

    assert calls == ["hello"]
    # We slept `retry_after + 1` before the retry — L-21 parity with
    # the send_text wave-2 retry wrapper.
    assert sleeps == [4]
    assert attempt_counter["n"] == 2


# ----------------------------------------------------------------------
# send_document — FileNotFoundError re-raised after warning-log (L-20).
# ----------------------------------------------------------------------


async def test_send_document_file_not_found_logs_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))

    async def raise_fnf(**_kwargs: Any) -> None:
        raise FileNotFoundError("artefact went missing")

    monkeypatch.setattr(adapter._bot, "send_document", raise_fnf)

    with pytest.raises(FileNotFoundError):
        await adapter.send_document(42, tmp_path / "gone.pdf", caption="cap")


async def test_send_document_permission_error_re_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling to FNF: PermissionError also hits the L-20 non-retry branch."""
    adapter = TelegramAdapter(_settings(tmp_path))

    async def raise_perm(**_kwargs: Any) -> None:
        raise PermissionError("mode 000")

    monkeypatch.setattr(adapter._bot, "send_document", raise_perm)

    with pytest.raises(PermissionError):
        await adapter.send_document(42, tmp_path / "locked.pdf")


# ----------------------------------------------------------------------
# send_audio — TelegramNetworkError retries twice then raises.
# ----------------------------------------------------------------------


async def test_send_audio_network_error_retries_twice_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))

    artefact = tmp_path / "track.mp3"
    artefact.write_bytes(b"ID3\x04")

    attempt_counter = {"n": 0}

    async def always_network_fail(**_kwargs: Any) -> None:
        attempt_counter["n"] += 1
        raise TelegramNetworkError(method=None, message="conn reset")  # type: ignore[arg-type]

    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(adapter._bot, "send_audio", always_network_fail)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    with pytest.raises(TelegramNetworkError):
        await adapter.send_audio(42, artefact)

    # One initial attempt + two retries = three total send calls.
    assert attempt_counter["n"] == 3
    # Exponential backoff: 1 s and 2 s between the three attempts.
    assert sleeps == [1, 2]


async def test_send_audio_network_error_recovers_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: if the second retry succeeds, send_audio returns cleanly."""
    adapter = TelegramAdapter(_settings(tmp_path))

    artefact = tmp_path / "recover.mp3"
    artefact.write_bytes(b"ID3\x04")

    attempts = {"n": 0}

    async def flaky(**_kwargs: Any) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TelegramNetworkError(method=None, message="transient")  # type: ignore[arg-type]

    async def fake_sleep(duration: float) -> None:
        del duration

    monkeypatch.setattr(adapter._bot, "send_audio", flaky)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    await adapter.send_audio(42, artefact)
    assert attempts["n"] == 3
