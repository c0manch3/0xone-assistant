"""Phase 7 commit 13 — handler builds the multimodal envelope correctly.

Scenarios (per implementation.md §3.1 / §4.1 test matrix):

* Photo attachment under the inline cap → `image_blocks` carries one
  base64-encoded image block, `system_notes` carries one descriptive
  note. The MIME type + width/height echo the `MediaAttachment`.
* Oversize photo → no `image_blocks`, ONE note describing the skip.
* Mixed-kind bundle (photo + voice + document) → image_blocks has 1
  entry, notes has 3 entries in attachment order, none are dropped.

The handler must NEVER locally dedup `local_path` — invariant I-7.6
(adapter-level dedup in Wave 7A commit 12). We therefore do NOT seed a
duplicate-path scenario here; it belongs in the adapter test suite.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, MediaSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


class _SpyBridge:
    """Capture the exact `ask(...)` kwargs the handler passes."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None
        self.last_image_blocks: list[dict[str, Any]] | None = None
        self.last_user_text: str | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, history
        self.last_user_text = user_text
        self.last_system_notes = (
            list(system_notes) if system_notes is not None else None
        )
        self.last_image_blocks = (
            list(image_blocks) if image_blocks is not None else None
        )
        for item in self._items:
            yield item


def _settings(tmp_path: Path, photo_mode: str = "inline_base64") -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(photo_mode=photo_mode),  # type: ignore[arg-type]
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
        result="ok",
        uuid="u",
    )


def _write_jpeg_stub(path: Path, *, size_bytes: int = 1024) -> None:
    """Writes a valid JPEG SOI marker followed by filler bytes.

    Handler only base64-encodes `read_bytes()`; no JPEG parsing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # SOI (FFD8) + APP0/JFIF header + filler + EOI (FFD9). Size-padded
    # to the requested length.
    soi = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    eoi = b"\xff\xd9"
    filler_len = max(0, size_bytes - len(soi) - len(eoi))
    path.write_bytes(soi + (b"\xab" * filler_len) + eoi)


async def test_photo_inline_base64_produces_image_block_and_note(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "env1.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "pic.jpg"
    _write_jpeg_stub(photo, size_bytes=2048)
    expected_b64 = base64.b64encode(photo.read_bytes()).decode("ascii")

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="got it"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=7,
        text="look at this",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=photo.stat().st_size,
                width=640,
                height=480,
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_image_blocks is not None
    assert len(bridge.last_image_blocks) == 1
    block = bridge.last_image_blocks[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/jpeg"
    assert block["source"]["data"] == expected_b64

    assert bridge.last_system_notes is not None
    assert any("640x480" in n and str(photo) in n for n in bridge.last_system_notes)
    # History MUST persist ONLY the original user text — NOT the raw
    # image bytes (plan §3.3 + phase-2 invariant).
    rows = await conv.load_recent(7, limit_turns=10)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows and user_rows[0]["content"] == [
        {"type": "text", "text": "look at this"}
    ]

    await conn.close()


async def test_photo_over_inline_cap_produces_only_note(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "env2.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "huge.jpg"
    _write_jpeg_stub(photo, size_bytes=64)

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="too big"),
            _result(),
        ]
    )
    # Settings with a tiny cap so the stub (64 B declared size) trips it.
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(photo_max_inline_bytes=32),  # type: ignore[arg-type]
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=8,
        text="see?",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=64,  # > 32 cap
                width=1,
                height=1,
            ),
        ),
    )
    await handler.handle(msg, emit)

    # Oversize → no image_block; note explains the skip.
    assert bridge.last_image_blocks is None
    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 1
    assert "exceeds inline cap" in bridge.last_system_notes[0]

    await conn.close()


async def test_mixed_kinds_preserve_order_and_collect_notes(tmp_path: Path) -> None:
    """Photo + voice + document in one envelope: order preserved, each kind
    contributes exactly one note; only the photo contributes an image block."""
    conn = await connect(tmp_path / "env3.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "mix.jpg"
    voice = tmp_path / "inbox" / "speak.ogg"
    doc = tmp_path / "inbox" / "report.pdf"
    for p in (photo, voice, doc):
        p.parent.mkdir(parents=True, exist_ok=True)
    _write_jpeg_stub(photo, size_bytes=1024)
    voice.write_bytes(b"OggS" + b"\x00" * 512)
    doc.write_bytes(b"%PDF-1.4\n" + b"\x00" * 512)

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="ack"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=9,
        text="bundle",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=photo.stat().st_size,
                width=100,
                height=200,
            ),
            MediaAttachment(
                kind="voice",
                local_path=voice,
                mime_type="audio/ogg",
                file_size=voice.stat().st_size,
                duration_s=12,
            ),
            MediaAttachment(
                kind="document",
                local_path=doc,
                mime_type="application/pdf",
                file_size=doc.stat().st_size,
                filename_original="report.pdf",
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_image_blocks is not None
    assert len(bridge.last_image_blocks) == 1

    notes = bridge.last_system_notes
    assert notes is not None
    assert len(notes) == 3
    # Order MUST match attachment order.
    assert "photo" in notes[0] and "100x200" in notes[0]
    assert "voice" in notes[1] and "duration=12" in notes[1]
    assert "document" in notes[2] and "report.pdf" in notes[2]
    assert "tools/extract_doc" in notes[2]

    await conn.close()
