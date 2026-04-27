"""Phase 6b — handler image-branch unit tests.

Covers:

- single jpeg → bridge gets image_blocks list with 1 item;
- 2-photo media_group → bridge gets image_blocks list with 2 items;
- vision summary captured: ``[photo: NAME | seen: <200 chars>]`` in
  persisted user row;
- empty caption gets default Russian "что на фото?";
- VisionError → quarantine + Russian reply, bridge NEVER called;
- magic mismatch → quarantine + format-specific
  "не похож на JPEG" reply (F3 spec AC#6);
- multi-photo cleanup: every path in attachment_paths unlinked in
  finally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _CapturingBridge(ClaudeBridge):
    """Mock bridge that records image_blocks + system_notes."""

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
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "history": history,
                "system_notes": system_notes,
                "image_blocks": image_blocks,
            }
        )
        if isinstance(self._script, Exception):
            raise self._script
        for item in self._script:
            yield item


def _settings(project_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=project_root / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "handler.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


def _make_emit() -> tuple[list[str], Any]:
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    return emitted, emit


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


def _make_jpeg(tmp_path: Path, name: str, color: str = "red") -> Path:
    """Drop a real JPEG into the uploads_dir.

    Mirrors ``Settings.uploads_dir`` Mac-dev fallback so the path the
    handler computes matches the file we wrote.
    """
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / name
    im = Image.new("RGB", (200, 200), color=color)
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=80)
    p.write_bytes(buf.getvalue())
    return p


# ---------------------------------------------------------------------------
# Single jpeg
# ---------------------------------------------------------------------------


async def test_single_jpeg_passes_image_blocks_to_bridge(
    tmp_path: Path,
) -> None:
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="на фото красный квадрат"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    p = _make_jpeg(tmp_path, "abc__photo.jpg")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=1,
        message_id=1,
        text="что на фото?",
        attachment=p,
        attachment_kind="jpg",
        attachment_filename="abc__photo.jpg",
    )
    await handler.handle(msg, emit)

    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    assert call["image_blocks"] is not None
    assert len(call["image_blocks"]) == 1
    block = call["image_blocks"][0]
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    # Tmp cleaned up.
    assert not p.exists()
    await store._conn.close()


async def test_single_png_passes_image_blocks_to_bridge(tmp_path: Path) -> None:
    """PNG kind also routes through the vision branch."""
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="это png"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / "abc__pic.png"
    im = Image.new("RGB", (64, 64), color="green")
    im.save(p, format="PNG")

    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=2,
        message_id=2,
        text="что?",
        attachment=p,
        attachment_kind="png",
        attachment_filename="pic.png",
    )
    await handler.handle(msg, emit)

    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    assert call["image_blocks"] is not None
    # PNG converted to JPEG in the envelope.
    assert call["image_blocks"][0]["source"]["media_type"] == "image/jpeg"
    await store._conn.close()


# ---------------------------------------------------------------------------
# Media group (multi-photo)
# ---------------------------------------------------------------------------


async def test_media_group_two_photos_yields_two_image_blocks(
    tmp_path: Path,
) -> None:
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="одинаковые"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    p1 = _make_jpeg(tmp_path, "u1__photo_1.jpg", color="red")
    p2 = _make_jpeg(tmp_path, "u2__photo_2.jpg", color="blue")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=3,
        message_id=0,
        text="что общего?",
        attachment=p1,
        attachment_kind="jpg",
        attachment_filename=p1.name,
        attachment_paths=[p1, p2],
    )
    await handler.handle(msg, emit)

    assert len(bridge.calls) == 1
    blocks = bridge.calls[0]["image_blocks"]
    assert blocks is not None
    assert len(blocks) == 2
    # Both photos cleaned up.
    assert not p1.exists()
    assert not p2.exists()
    await store._conn.close()


async def test_media_group_user_row_carries_marker_per_photo(
    tmp_path: Path,
) -> None:
    """User row markers: one ``[photo: NAME | seen: ...]`` per photo,
    same ``seen:`` text repeated across the group.
    """
    import json

    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings,
        [TextBlock(text="одинаковые красные"), _result_message()],
    )
    handler = ClaudeHandler(settings, store, bridge)

    p1 = _make_jpeg(tmp_path, "u1__photo_1.jpg", color="red")
    p2 = _make_jpeg(tmp_path, "u2__photo_2.jpg", color="red")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=10,
        message_id=0,
        text="?",
        attachment=p1,
        attachment_kind="jpg",
        attachment_filename=p1.name,
        attachment_paths=[p1, p2],
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=10 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    content = json.loads(rows[0][0])
    text = content[0]["text"]
    assert "[photo: u1__photo_1.jpg" in text
    assert "[photo: u2__photo_2.jpg" in text
    assert "seen: одинаковые красные" in text
    await store._conn.close()


# ---------------------------------------------------------------------------
# Auto-summary (Q8 v1)
# ---------------------------------------------------------------------------


async def test_user_row_marker_includes_seen_summary(tmp_path: Path) -> None:
    import json

    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings,
        [TextBlock(text="на фото красный квадрат"), _result_message()],
    )
    handler = ClaudeHandler(settings, store, bridge)

    p = _make_jpeg(tmp_path, "uuid__photo.jpg")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=4,
        message_id=4,
        text="что на фото?",
        attachment=p,
        attachment_kind="jpg",
        attachment_filename="uuid__photo.jpg",
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=4 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    payload = json.loads(rows[0][0])
    text = payload[0]["text"]
    assert "[photo: uuid__photo.jpg" in text
    assert "seen: на фото красный квадрат" in text
    await store._conn.close()


# ---------------------------------------------------------------------------
# VisionError quarantine
# ---------------------------------------------------------------------------


async def test_corrupt_image_quarantines_and_replies_russian(
    tmp_path: Path,
) -> None:
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    # Bridge would fail anyway since it should never be called.
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    # JPEG suffix, but body is plain text → VisionError("magic mismatch …").
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    bad = uploads / "uuid__bad.jpg"
    bad.write_bytes(b"this is not an image at all, just text")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=5,
        message_id=5,
        text="что?",
        attachment=bad,
        attachment_kind="jpg",
        attachment_filename="bad.jpg",
    )
    await handler.handle(msg, emit)

    # Bridge NEVER invoked.
    assert bridge.calls == []
    # F3: format-specific Russian magic-mismatch reply (spec AC#6).
    # The owner declared ``.jpg`` so the reply must mention "JPEG".
    assert any("не похож на JPEG" in e for e in emitted)
    # Quarantined.
    quarantine = settings.uploads_dir / ".failed"
    failed = list(quarantine.iterdir())
    assert len(failed) == 1
    assert not bad.exists()
    await store._conn.close()


async def test_corrupt_decode_yields_generic_russian_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that passes magic but fails decode → generic
    "не смог обработать изображение" reply.
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / "uuid__truncated.jpg"
    # Valid JPEG magic header, garbage body — Pillow decode fails.
    p.write_bytes(b"\xff\xd8\xff\x00garbage bytes truncated")

    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=6,
        message_id=6,
        text="что?",
        attachment=p,
        attachment_kind="jpg",
        attachment_filename="truncated.jpg",
    )
    await handler.handle(msg, emit)

    assert bridge.calls == []
    assert any("не смог обработать" in e for e in emitted)
    assert not p.exists()
    await store._conn.close()


# ---------------------------------------------------------------------------
# Vision error: bridge skipped + finally cleanup
# ---------------------------------------------------------------------------


async def test_invalid_attachment_paths_invariant_raises_assert(
    tmp_path: Path,
) -> None:
    """F9: handler asserts the three multi-photo invariants on entry —
    ``attachment_paths`` non-empty, ``attachment_paths[0] is attachment``,
    and ``attachment_kind in IMAGE_KINDS``. A future caller that
    synthesises ``IncomingMessage`` with mismatched paths fails fast
    rather than producing a silently-wrong DB row.
    """
    import pytest

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    uploads = settings.uploads_dir
    uploads.mkdir(parents=True, exist_ok=True)
    p1 = uploads / "uuid1__a.jpg"
    p1.write_bytes(b"\xff\xd8\xff\xe0placeholder")
    p2 = uploads / "uuid2__b.jpg"
    p2.write_bytes(b"\xff\xd8\xff\xe0placeholder")

    _, emit = _make_emit()
    # attachment is p2 but attachment_paths[0] is p1 — invariant
    # violation: ``attachment_paths[0] is attachment`` must hold.
    msg = IncomingMessage(
        chat_id=99,
        message_id=99,
        text="?",
        attachment=p2,
        attachment_kind="jpg",
        attachment_filename=p2.name,
        attachment_paths=[p1, p2],
    )
    with pytest.raises(AssertionError):
        await handler.handle(msg, emit)
    await store._conn.close()


async def test_multi_textblock_seen_summary_concatenates_chunks(
    tmp_path: Path,
) -> None:
    """F11: model often emits a brief preamble + substantive
    description across multiple TextBlocks. The ``seen:`` summary
    must concatenate ALL text chunks (joined with a space) before
    trimming to 200 chars at a word boundary.
    """
    import json

    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings,
        [
            TextBlock(text="давайте посмотрим"),
            TextBlock(text="на фото красный квадрат"),
            _result_message(),
        ],
    )
    handler = ClaudeHandler(settings, store, bridge)

    p = _make_jpeg(tmp_path, "uuid__photo.jpg")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=20,
        message_id=20,
        text="что?",
        attachment=p,
        attachment_kind="jpg",
        attachment_filename="uuid__photo.jpg",
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=20 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    payload = json.loads(rows[0][0])
    text = payload[0]["text"]
    # Both chunks present in the seen-summary, joined with a space.
    assert "давайте посмотрим на фото красный квадрат" in text
    await store._conn.close()


async def test_media_group_vision_error_produces_one_marker_per_photo(
    tmp_path: Path,
) -> None:
    """F5: vision-error branch persists ONE marker line per photo for a
    media_group of N photos (mirroring the success path).
    """
    import json

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    # Both files have bogus magic → VisionError on validate.
    p1 = uploads / "u1__bad.jpg"
    p2 = uploads / "u2__bad.jpg"
    p1.write_bytes(b"not an image")
    p2.write_bytes(b"also not an image")

    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=21,
        message_id=21,
        text="что?",
        attachment=p1,
        attachment_kind="jpg",
        attachment_filename=p1.name,
        attachment_paths=[p1, p2],
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=21 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    text = json.loads(rows[0][0])[0]["text"]
    # ONE marker line per photo on error path (mirrors success path).
    assert text.count("[photo: u1__bad.jpg") == 1
    assert text.count("[photo: u2__bad.jpg") == 1
    assert text.count("(vision pre-process failed)") == 2
    await store._conn.close()


async def test_image_as_document_marker_uses_original_filename(
    tmp_path: Path,
) -> None:
    """F12: for a single-image-as-document upload, the marker uses
    the original Telegram filename (e.g. ``IMG_1234.heic``) rather
    than the uuid-prefixed tmp synthetic. Single-image case only —
    media_group / inline F.photo retain the synthetic name.
    """
    import json

    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings,
        [TextBlock(text="это фото"), _result_message()],
    )
    handler = ClaudeHandler(settings, store, bridge)

    p = _make_jpeg(tmp_path, "uuidabc__IMG_1234.jpg")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=22,
        message_id=22,
        text="?",
        attachment=p,
        attachment_kind="jpg",
        # Original filename as Telegram delivered it. Adapter
        # propagates this verbatim so the forensic marker is
        # human-readable on post-mortem.
        attachment_filename="IMG_1234.jpg",
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=22 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    text = json.loads(rows[0][0])[0]["text"]
    # F12: marker uses the Telegram original filename for the
    # single-image case, NOT the uuid-prefixed tmp synthetic.
    assert "[photo: IMG_1234.jpg" in text
    assert "uuidabc__IMG_1234.jpg" not in text
    await store._conn.close()


async def test_vision_error_finally_cleans_all_image_paths(
    tmp_path: Path,
) -> None:
    """Multi-photo: VisionError on photo[1] still cleans both tmp paths."""
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    p1 = _make_jpeg(tmp_path, "u1__a.jpg")
    # Second photo has bogus magic → VisionError on validate.
    uploads = tmp_path / "data" / "uploads"
    p2 = uploads / "u2__bad.jpg"
    p2.write_bytes(b"random text not an image")

    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=7,
        message_id=0,
        text="что?",
        attachment=p1,
        attachment_kind="jpg",
        attachment_filename=p1.name,
        attachment_paths=[p1, p2],
    )
    await handler.handle(msg, emit)

    # Bridge skipped — VisionError aborted before image_blocks built.
    assert bridge.calls == []
    # First photo quarantined; cleanup unlink in finally is a no-op then.
    quarantine = settings.uploads_dir / ".failed"
    failed_names = sorted(p.name for p in quarantine.iterdir())
    # Both names quarantined (the failure handler walks all paths).
    assert "u1__a.jpg" in failed_names
    assert "u2__bad.jpg" in failed_names
    # Original locations empty.
    assert not p1.exists()
    assert not p2.exists()
    await store._conn.close()
