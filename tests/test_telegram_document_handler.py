"""Phase 6a — TelegramAdapter._on_document unit tests.

Covers:
- size cap pre-download (devil C2);
- suffix whitelist accept / reject;
- Optional[int] file_size guard (None → ``or 0``);
- Optional[str] file_name guard (None → reject);
- TelegramBadRequest "too big" envelope on download (devil C2);
- TelegramBadRequest other envelope;
- caption fallback to Russian default for ALL formats including TXT/MD
  (devil L6);
- tmp filename sanitisation (devil M5: UUID + sanitised stem);
- path-injection defence (no dot/slash escapes the uploads dir).

The aiogram ``Message`` / ``Document`` objects are mocked because we
don't run a real bot; the assertions target the side-effects the
adapter triggers (replies, IncomingMessage construction, tmp file
existence).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.adapters.base import IncomingMessage
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings

# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


@dataclass
class _FakeChat:
    id: int = 42


@dataclass
class _FakeDocument:
    file_id: str = "doc_id"
    file_size: int | None = 1024
    file_name: str | None = "report.pdf"


@dataclass
class _FakeMessage:
    document: _FakeDocument | None
    caption: str | None = None
    text: str | None = None
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 1
    replies: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    content_type: str = "document"

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class _FakeHandler:
    """Captures the IncomingMessage the adapter passes through."""

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


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        # Telegram token format `<digits>:<token>` is enforced by
        # aiogram's Bot constructor; the existing handler tests use
        # the same shape.
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


def _build_adapter(tmp_path: Path) -> tuple[TelegramAdapter, _FakeHandler]:
    settings = _build_settings(tmp_path)
    adapter = TelegramAdapter(settings)
    handler = _FakeHandler()
    adapter.set_handler(handler)
    # Replace the real Bot with mocks. The download path should be
    # intercepted to emulate file writing without hitting Telegram.
    adapter._bot = MagicMock()  # type: ignore[assignment]

    async def fake_download(
        doc: Any, *, destination: Path, timeout: int = 30
    ) -> None:
        # ``timeout`` accepted (and ignored) so the adapter can pass
        # the explicit 90s bump (devil M-W2-6) without breaking the
        # mock signature.
        del timeout
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake content")

    adapter._bot.download = AsyncMock(side_effect=fake_download)
    adapter._bot.send_message = AsyncMock()
    # ChatActionSender uses bot.send_chat_action; mock it.
    adapter._bot.send_chat_action = AsyncMock()
    return adapter, handler


# ----------------------------------------------------------------------
# Size cap
# ----------------------------------------------------------------------


async def test_oversize_pre_download_rejects(tmp_path: Path) -> None:
    """``file_size > 20 MB`` rejects without invoking download."""
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_size=21 * 1024 * 1024, file_name="big.pdf")
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("20 МБ" in r for r in msg.replies)
    adapter._bot.download.assert_not_called()


async def test_file_size_none_is_treated_as_zero_and_proceeds(tmp_path: Path) -> None:
    """Devil C2: ``Document.file_size`` may be ``None`` for old-client
    forwards. ``(file_size or 0) > N`` keeps the comparison type-safe;
    download proceeds.
    """
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_size=None, file_name="ok.txt")
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    assert handler.received[0].attachment is not None


# ----------------------------------------------------------------------
# Suffix whitelist
# ----------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["pdf", "docx", "txt", "md", "xlsx"])
async def test_whitelist_accept_each_suffix(tmp_path: Path, suffix: str) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name=f"file.{suffix}", file_size=2048)
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment_kind == suffix
    assert incoming.attachment is not None
    assert incoming.attachment.suffix == f".{suffix}"


@pytest.mark.parametrize("suffix", ["zip", "exe", "py", "tar", "rtf"])
async def test_whitelist_reject_other_suffix(tmp_path: Path, suffix: str) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name=f"thing.{suffix}", file_size=1024)
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("формат не поддерживается" in r for r in msg.replies)
    adapter._bot.download.assert_not_called()


async def test_file_name_none_rejects(tmp_path: Path) -> None:
    """Devil M1: ``Document.file_name`` may be None — reject with the
    format-list message because there's no usable suffix to whitelist.
    """
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name=None, file_size=1024)
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("формат не поддерживается" in r for r in msg.replies)


async def test_file_name_no_suffix_rejects(tmp_path: Path) -> None:
    """``file_name="random_no_extension"`` → reject."""
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name="no_extension_here", file_size=1024)
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []


# ----------------------------------------------------------------------
# TelegramBadRequest envelope
# ----------------------------------------------------------------------


async def test_download_too_big_yields_russian_reply(tmp_path: Path) -> None:
    """``TelegramBadRequest("file is too big")`` from ``bot.download``
    surfaces the Russian "лимит" reply (devil C2).
    """
    from aiogram.exceptions import TelegramBadRequest

    adapter, handler = _build_adapter(tmp_path)
    method = MagicMock()
    method.__class__.__name__ = "DownloadFile"
    error = TelegramBadRequest(method=method, message="file is too big")
    adapter._bot.download = AsyncMock(side_effect=error)

    msg = _FakeMessage(document=_FakeDocument(file_name="x.pdf", file_size=1024))
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("лимит" in r for r in msg.replies)
    # Reply must NOT echo upstream exception text into Telegram (QA M3).
    assert not any("file is too big" in r for r in msg.replies)


async def test_download_other_bad_request_surfaces_fixed_reply(tmp_path: Path) -> None:
    """A non-"too big" ``TelegramBadRequest`` surfaces the FIXED
    Russian "не смог скачать — проверь логи" reply. The upstream
    exception text MUST NOT be echoed to Telegram (QA M3 / devil
    M-W2-4 partial).
    """
    from aiogram.exceptions import TelegramBadRequest

    adapter, handler = _build_adapter(tmp_path)
    method = MagicMock()
    method.__class__.__name__ = "DownloadFile"
    secret_token_in_error = "bad request: invalid file id 12345-secret-token"
    error = TelegramBadRequest(method=method, message=secret_token_in_error)
    adapter._bot.download = AsyncMock(side_effect=error)

    msg = _FakeMessage(document=_FakeDocument(file_name="x.pdf", file_size=1024))
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("не смог скачать" in r for r in msg.replies)
    # The upstream exception text MUST NOT leak into Telegram.
    assert not any(secret_token_in_error in r for r in msg.replies)
    assert not any("12345-secret-token" in r for r in msg.replies)


async def test_download_unexpected_exception_uses_fixed_reply(tmp_path: Path) -> None:
    """A non-Telegram exception (``OSError``, runtime bug, etc.) must
    NOT have its ``repr`` echoed to Telegram. The reply is a fixed
    Russian "internal error" string; the structured log captures the
    full error for owner debuggability (QA M3).
    """
    adapter, handler = _build_adapter(tmp_path)
    secret_path = "/internal/secret/path/with/token-deadbeef"
    error = OSError(f"disk error at {secret_path}")
    adapter._bot.download = AsyncMock(side_effect=error)

    msg = _FakeMessage(document=_FakeDocument(file_name="x.pdf", file_size=1024))
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    # Reply is a fixed Russian internal-error string.
    assert any("внутренняя ошибка" in r for r in msg.replies)
    # No exception ``repr`` / ``str`` content leaked.
    assert not any(secret_path in r for r in msg.replies)
    assert not any("OSError" in r for r in msg.replies)
    assert not any("deadbeef" in r for r in msg.replies)


async def test_download_timeout_uses_dedicated_reply(tmp_path: Path) -> None:
    """``TimeoutError`` from ``bot.download`` surfaces a dedicated
    Russian "timeout" reply (devil M-W2-6).
    """
    adapter, handler = _build_adapter(tmp_path)
    adapter._bot.download = AsyncMock(side_effect=TimeoutError())

    msg = _FakeMessage(document=_FakeDocument(file_name="x.pdf", file_size=1024))
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any(
        "Telegram не успел отдать файл за 90 секунд" in r for r in msg.replies
    )


async def test_download_passes_request_timeout_90s(tmp_path: Path) -> None:
    """``bot.download`` is called with ``timeout=90`` (devil M-W2-6).

    aiogram's default 30s is too tight for a 19 MB file on a slow
    uplink; bumping to 90s lets the call complete instead of opaquely
    timing out and surfacing a confusing reply.
    """
    adapter, _handler = _build_adapter(tmp_path)

    msg = _FakeMessage(document=_FakeDocument(file_name="x.pdf", file_size=1024))
    await adapter._on_document(msg)  # type: ignore[arg-type]

    # The fake download in _build_adapter accepts ``destination`` and
    # ``timeout`` (mirrors aiogram's real signature). AsyncMock records
    # the kwargs the adapter passed.
    adapter._bot.download.assert_awaited_once()
    _, kwargs = adapter._bot.download.call_args
    assert kwargs.get("timeout") == 90


# ----------------------------------------------------------------------
# Caption fallback (devil L6 — applies to ALL whitelisted formats)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["pdf", "docx", "txt", "md", "xlsx"])
async def test_empty_caption_uses_default_russian(tmp_path: Path, suffix: str) -> None:
    """``caption=""`` AND ``caption=None`` AND whitespace-only caption →
    text="опиши содержимое файла" for ALL whitelisted formats.
    """
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name=f"x.{suffix}", file_size=1024),
        caption=None,
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    assert handler.received[0].text == "опиши содержимое файла"


async def test_whitespace_caption_uses_default(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name="x.pdf", file_size=1024),
        caption="   \t  ",
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received[0].text == "опиши содержимое файла"


async def test_real_caption_passes_through(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name="x.pdf", file_size=1024),
        caption="please summarize",
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert handler.received[0].text == "please summarize"


# ----------------------------------------------------------------------
# Path sanitisation (devil M5)
# ----------------------------------------------------------------------


async def test_tmp_path_sanitises_traversal(tmp_path: Path) -> None:
    """``file_name="../../etc/passwd.txt"`` → tmp file lives inside
    ``settings.uploads_dir``; sanitisation replaces ``/`` with ``_``.
    """
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(
            file_name="../../etc/passwd.txt", file_size=64
        )
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment is not None
    # Resolves inside uploads_dir.
    settings_uploads = adapter._settings.uploads_dir.resolve()
    assert incoming.attachment.resolve().is_relative_to(settings_uploads)
    # The leading "../../etc/" never reaches the on-disk path.
    assert "/etc/" not in str(incoming.attachment)
    assert ".." not in incoming.attachment.name


async def test_tmp_path_uuid_uniqueness(tmp_path: Path) -> None:
    """Two uploads of the same filename produce distinct tmp paths."""
    adapter, handler = _build_adapter(tmp_path)
    msg1 = _FakeMessage(
        document=_FakeDocument(file_name="same.txt", file_size=64),
        message_id=1,
    )
    msg2 = _FakeMessage(
        document=_FakeDocument(file_name="same.txt", file_size=64),
        message_id=2,
    )
    await adapter._on_document(msg1)  # type: ignore[arg-type]
    await adapter._on_document(msg2)  # type: ignore[arg-type]

    p1 = handler.received[0].attachment
    p2 = handler.received[1].attachment
    assert p1 is not None and p2 is not None
    assert p1 != p2


# ----------------------------------------------------------------------
# Routing order (RQ3)
# ----------------------------------------------------------------------


def test_handler_registration_order_text_doc_catchall(tmp_path: Path) -> None:
    """Registration order: text → document → catch-all (RQ3)."""
    adapter, _ = _build_adapter(tmp_path)
    # aiogram stores handlers as a list of HandlerObject in
    # ``_dp.message.handlers``; their callable is in ``.callback``.
    callbacks = [h.callback for h in adapter._dp.message.handlers]
    names = [getattr(c, "__name__", repr(c)) for c in callbacks]
    assert names.index("_on_text") < names.index("_on_document")
    assert names.index("_on_document") < names.index("_on_non_text")
