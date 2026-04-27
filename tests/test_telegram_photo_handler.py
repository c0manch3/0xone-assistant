"""Phase 6b — TelegramAdapter._on_photo + media-group + animation.

Covers:

- ``F.animation`` no longer matches ``_on_document`` (registration
  filter excludes it).
- Single-photo path: largest PhotoSize selected by area, default
  caption fallback, IncomingMessage attachment fields populated.
- Pre-download size cap rejects > 20 MB.
- Filename synthesis: ``<uuid>__photo_<msg_id>.jpg``.
- ``_on_text`` flushes pending media-group buckets.
- Image-as-document: ``_on_document`` accepts ``.png`` etc. as
  AttachmentKind ``"png"``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from assistant.adapters.base import IncomingMessage
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings


@dataclass
class _FakeChat:
    id: int = 42


@dataclass
class _FakePhotoSize:
    file_id: str = "p"
    file_unique_id: str = "u"
    width: int = 100
    height: int = 100
    file_size: int | None = 1024


@dataclass
class _FakeAnimation:
    file_id: str = "anim"


@dataclass
class _FakeDocument:
    file_id: str = "doc"
    file_size: int | None = 1024
    file_name: str | None = "x.pdf"


@dataclass
class _FakeMessage:
    photo: list[_FakePhotoSize] | None = None
    document: _FakeDocument | None = None
    animation: _FakeAnimation | None = None
    caption: str | None = None
    text: str | None = None
    media_group_id: str | None = None
    chat: _FakeChat = field(default_factory=_FakeChat)
    message_id: int = 1
    replies: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    content_type: str = "photo"

    async def reply(self, text: str) -> None:
        self.replies.append(text)

    async def answer(self, text: str) -> None:
        self.answers.append(text)


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


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
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
    adapter._bot = MagicMock()  # type: ignore[assignment]

    async def fake_download(
        target: Any, *, destination: Path, timeout: int = 30
    ) -> None:
        del target, timeout
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Write a real 100x100 JPEG so downstream vision pipeline
        # (when invoked in integration tests) gets valid magic.
        im = Image.new("RGB", (100, 100), color="red")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=80)
        destination.write_bytes(buf.getvalue())

    adapter._bot.download = AsyncMock(side_effect=fake_download)
    adapter._bot.send_message = AsyncMock()
    adapter._bot.send_chat_action = AsyncMock()
    return adapter, handler


# ---------------------------------------------------------------------------
# F.animation excluded from _on_document
# ---------------------------------------------------------------------------


async def test_document_filter_excludes_animation(tmp_path: Path) -> None:
    """A document with ``animation`` set is rejected by the registered
    filter ``F.document & ~F.animation`` (M-W2-1 closure).

    aiogram's MagicFilter is opaque to introspection; we instead drive
    the dispatcher's ``check`` path with a fake animation message and
    assert the filter returns False.
    """
    adapter, _ = _build_adapter(tmp_path)
    doc_handlers = [
        h for h in adapter._dp.message.handlers
        if getattr(h.callback, "__name__", "") == "_on_document"
    ]
    assert len(doc_handlers) == 1
    handler = doc_handlers[0]
    # Build a fake animation-document and a fake plain-document. The
    # filter callable on aiogram's HandlerObject expects the event
    # object as a positional arg.
    plain_msg = _FakeMessage(
        document=_FakeDocument(file_name="x.pdf", file_size=1024),
        animation=None,
        content_type="document",
    )
    anim_msg = _FakeMessage(
        document=_FakeDocument(file_name="x.gif", file_size=1024),
        animation=_FakeAnimation(),
        content_type="document",
    )
    # MagicFilter.resolve returns a tuple (matched, dependencies) —
    # truthiness of the first element is what aiogram acts on.
    filter_callable = handler.filters[0].callback
    plain_match = filter_callable(plain_msg)
    anim_match = filter_callable(anim_msg)
    assert plain_match
    assert not anim_match


# ---------------------------------------------------------------------------
# Single photo
# ---------------------------------------------------------------------------


async def test_single_photo_largest_size_selected(tmp_path: Path) -> None:
    """``message.photo`` carries a list; adapter picks the LARGEST by
    area regardless of list order.
    """
    adapter, handler = _build_adapter(tmp_path)
    sizes = [
        _FakePhotoSize(file_id="s", width=90, height=90, file_size=300),
        _FakePhotoSize(file_id="m", width=320, height=320, file_size=1500),
        _FakePhotoSize(file_id="l", width=1280, height=720, file_size=80000),
    ]
    msg = _FakeMessage(photo=sizes, message_id=7)
    await adapter._on_photo(msg)  # type: ignore[arg-type]

    # Largest variant (l) is what bot.download was called with.
    args, _kwargs = adapter._bot.download.call_args
    target = args[0]
    assert target.file_id == "l"

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment is not None
    # Filename pattern: <uuid>__photo_<msg_id>.jpg.
    assert incoming.attachment.name.endswith(f"__photo_{msg.message_id}.jpg")


async def test_single_photo_default_russian_caption(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)],
        caption=None,
    )
    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    assert handler.received[0].text == "что на фото?"


async def test_single_photo_real_caption_passes_through(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)],
        caption="опиши маме",
    )
    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert handler.received[0].text == "опиши маме"


async def test_single_photo_oversize_pre_download_rejects(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    sizes = [
        _FakePhotoSize(width=4000, height=4000, file_size=21 * 1024 * 1024),
    ]
    msg = _FakeMessage(photo=sizes)
    await adapter._on_photo(msg)  # type: ignore[arg-type]

    assert handler.received == []
    assert any("20 МБ" in r for r in msg.replies)
    adapter._bot.download.assert_not_called()


async def test_single_photo_kind_is_jpg(tmp_path: Path) -> None:
    """Inline photos are always JPEG-compressed by Telegram."""
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)]
    )
    await adapter._on_photo(msg)  # type: ignore[arg-type]
    assert handler.received[0].attachment_kind == "jpg"


# ---------------------------------------------------------------------------
# Media group
# ---------------------------------------------------------------------------


async def test_media_group_two_photos_aggregates_to_one_handler_call(
    tmp_path: Path,
) -> None:
    adapter, handler = _build_adapter(tmp_path)
    # Lower the debounce so the test runs quickly.
    adapter._media_group._debounce_sec = 0.05

    msg1 = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)],
        media_group_id="grp1",
        message_id=1,
        caption="album",
    )
    msg2 = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)],
        media_group_id="grp1",
        message_id=2,
    )
    await adapter._on_photo(msg1)  # type: ignore[arg-type]
    await adapter._on_photo(msg2)  # type: ignore[arg-type]
    await asyncio.sleep(0.2)

    assert len(handler.received) == 1
    incoming = handler.received[0]
    assert incoming.attachment_paths is not None
    assert len(incoming.attachment_paths) == 2
    assert incoming.text == "album"


# ---------------------------------------------------------------------------
# Image-as-document (jpg/png/webp/heic via _on_document)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["jpg", "jpeg", "png", "webp", "heic"])
async def test_document_image_kind_accepted(tmp_path: Path, suffix: str) -> None:
    """``_on_document`` accepts image suffixes as image AttachmentKind."""
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        photo=None,
        document=_FakeDocument(file_name=f"x.{suffix}", file_size=1024),
        content_type="document",
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]

    assert len(handler.received) == 1
    assert handler.received[0].attachment_kind == suffix
    # Default caption: "что на фото?" for image kinds.
    assert handler.received[0].text == "что на фото?"


async def test_document_image_real_caption_preserved(tmp_path: Path) -> None:
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name="x.png", file_size=1024),
        caption="расскажи",
        content_type="document",
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]
    assert handler.received[0].text == "расскажи"


async def test_document_pdf_kind_keeps_doc_default_caption(tmp_path: Path) -> None:
    """Non-image document kinds keep the 6a default caption."""
    adapter, handler = _build_adapter(tmp_path)
    msg = _FakeMessage(
        document=_FakeDocument(file_name="x.pdf", file_size=1024),
        content_type="document",
    )
    await adapter._on_document(msg)  # type: ignore[arg-type]
    assert handler.received[0].text == "опиши содержимое файла"


# ---------------------------------------------------------------------------
# Text arrival flushes pending media group
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F2 — F.animation handler emits Russian "анимации не поддерживаю"
# ---------------------------------------------------------------------------


async def test_animation_handler_emits_russian_reply(tmp_path: Path) -> None:
    """F2 / AC#5: animations route to a dedicated handler that emits
    "анимации не поддерживаю — пришли картинку" (not the stale
    catch-all "phase 6" copy).
    """
    adapter, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(
        photo=None,
        animation=_FakeAnimation(),
        content_type="animation",
    )
    await adapter._on_animation(msg)  # type: ignore[arg-type]
    assert any("анимации не поддерживаю" in r for r in msg.replies)


async def test_non_text_catch_all_updated_string(tmp_path: Path) -> None:
    """F2: stale "это будет в phase 6" copy replaced with a
    capability-list message (phase 6 has shipped).
    """
    adapter, _ = _build_adapter(tmp_path)
    msg = _FakeMessage(content_type="voice")
    await adapter._on_non_text(msg)  # type: ignore[arg-type]
    assert msg.answers, "_on_non_text must call message.answer"
    text = msg.answers[0]
    assert "phase 6" not in text.lower()
    assert "пришли" in text


async def test_text_arrival_flushes_pending_media_group(tmp_path: Path) -> None:
    """A text message for the same chat preempts pending photo
    debounce so the vision turn doesn't block the text turn.
    """
    adapter, handler = _build_adapter(tmp_path)
    # Long debounce: photo would not flush on its own within the test.
    adapter._media_group._debounce_sec = 10.0

    photo_msg = _FakeMessage(
        photo=[_FakePhotoSize(width=100, height=100, file_size=1024)],
        media_group_id="grp_text",
        message_id=1,
    )
    await adapter._on_photo(photo_msg)  # type: ignore[arg-type]

    # No flush yet (long debounce).
    assert handler.received == []

    text_msg = _FakeMessage(
        photo=None,
        text="hello",
        message_id=2,
        content_type="text",
    )
    await adapter._on_text(text_msg)  # type: ignore[arg-type]

    # Flush triggered before the text turn ran → handler saw photo
    # turn first, then text turn.
    received_kinds = [m.attachment_kind for m in handler.received]
    assert received_kinds == ["jpg", None]
