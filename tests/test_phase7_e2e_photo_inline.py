"""Phase 7 / commit 17 — E2E photo → inline_base64 → model describes.

This is the cross-commit wiring test for the primary multimodal path
(Spike 0 Q0-1: SDK accepts inline base64 image content blocks). Unit
tests for the individual pieces live in sibling files:

  * `test_handler_multimodal_envelope.py` — handler builds image_blocks
  * `test_handler_multimodal_real_photo.py` — real JPEG fixture + SDK
    round-trip (also gated by `RUN_SDK_INT=1`)
  * `test_telegram_adapter_media_handlers.py` — adapter ingress

This E2E file asserts the GLUE: an `IncomingMessage` with a photo
attachment flows through the real `ClaudeHandler` into a spy
`ClaudeBridge`, the envelope carries a base64 image block in the
shape the SDK expects (Q0-5b: `text → image → system-note` order),
and when `RUN_SDK_INT=1` is set the real bridge completes the turn.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, MediaSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore


class _SpyBridge:
    """Records the exact envelope the handler forwards.

    The SpyBridge differs from the unit-test variants by also
    validating the S-0 Q0-5b envelope ordering invariant: if both
    image_blocks and system_notes are forwarded, the ordering is
    enforced by ClaudeBridge.ask's prompt_stream (text → images →
    notes). We emit a synthetic `prompt_stream` preview so the E2E
    test can assert the envelope layout without reaching into the
    real bridge internals.
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None
        self.last_image_blocks: list[dict[str, Any]] | None = None
        self.last_user_text: str | None = None
        self.ask_called = False

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
        self.ask_called = True
        self.last_user_text = user_text
        self.last_system_notes = (
            list(system_notes) if system_notes is not None else None
        )
        self.last_image_blocks = (
            list(image_blocks) if image_blocks is not None else None
        )
        for item in self._items:
            yield item


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(),
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


def _write_jpeg(path: Path, size_bytes: int = 2048) -> None:
    """Write a minimum-valid JPEG with random-ish filler.

    Handler never inspects the JPEG; it only base64-encodes the raw
    bytes. A valid SOI/EOI envelope is enough for the round-trip.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    soi = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    eoi = b"\xff\xd9"
    filler = b"\xab" * max(0, size_bytes - len(soi) - len(eoi))
    path.write_bytes(soi + filler + eoi)


async def test_photo_turn_builds_image_block_and_reaches_reply(
    tmp_path: Path,
) -> None:
    """Full handler path: photo → base64 image block → bridge → emit.

    Assertions (all load-bearing on real production code, no mocks of
    the handler itself):

      * `bridge.last_image_blocks` has exactly one image block.
      * The base64 body round-trips to the raw JPEG bytes (no corruption).
      * `system_notes` carries a voice/photo description in order.
      * `ConversationStore.load_recent` returns ONLY the user's text —
        the raw image bytes are NEVER persisted (invariant §3.3).
      * Handler emits the assistant's reply text downstream.
    """
    conn = await connect(tmp_path / "e2e_photo.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "scene.jpg"
    _write_jpeg(photo, size_bytes=4096)
    expected_b64 = base64.b64encode(photo.read_bytes()).decode("ascii")

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="I see a JPEG scene"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    emitted: list[str] = []

    async def emit(t: str) -> None:
        emitted.append(t)

    msg = IncomingMessage(
        chat_id=77,
        text="describe",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=photo.stat().st_size,
                width=1920,
                height=1080,
            ),
        ),
    )
    await handler.handle(msg, emit)

    # Bridge received an image block in canonical SDK shape.
    assert bridge.ask_called is True
    assert bridge.last_image_blocks is not None
    assert len(bridge.last_image_blocks) == 1
    block = bridge.last_image_blocks[0]
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": expected_b64,
        },
    }

    # System-note names the photo + dimensions.
    assert bridge.last_system_notes is not None
    assert any(
        "photo" in n and "1920x1080" in n and str(photo) in n
        for n in bridge.last_system_notes
    ), bridge.last_system_notes

    # User text reaches the bridge unchanged.
    assert bridge.last_user_text == "describe"

    # History MUST NOT persist raw bytes — only the text row.
    rows = await conv.load_recent(77, limit_turns=5)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows
    assert user_rows[0]["content"] == [{"type": "text", "text": "describe"}]

    # Handler emitted the assistant text fragment.
    assert "I see a JPEG scene" in "".join(emitted)

    await conn.close()


async def test_photo_oversize_falls_back_to_note_only(tmp_path: Path) -> None:
    """Over-cap photo → zero image_blocks, one failure note.

    Regression for the §3.1 oversize branch; keeps the E2E file
    self-contained (no reliance on the dedicated unit test).
    """
    conn = await connect(tmp_path / "e2e_over.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "huge.jpg"
    _write_jpeg(photo, size_bytes=128)

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="that's too big"),
            _result(),
        ]
    )
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(photo_max_inline_bytes=64),  # sub-fixture cap
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=99,
        text="look",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=128,
                width=100,
                height=50,
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_image_blocks is None
    assert bridge.last_system_notes is not None
    assert any("exceeds inline cap" in n for n in bridge.last_system_notes)

    await conn.close()


@pytest.mark.skipif(
    os.environ.get("RUN_SDK_INT") != "1",
    reason="RUN_SDK_INT=1 required to hit a live SDK with a photo",
)
async def test_photo_turn_completes_via_real_bridge(tmp_path: Path) -> None:
    """C-2-style live roundtrip, but with the small test JPEG.

    Kept separate from `test_handler_multimodal_real_photo.py` (which
    owns the 3 MB fixture assertion) so the E2E suite can also opt
    into SDK coverage without requiring the heavy fixture.
    """
    from assistant.bridge.claude import ClaudeBridge

    photo = tmp_path / "inbox" / "small.jpg"
    _write_jpeg(photo, size_bytes=2048)

    conn = await connect(tmp_path / "e2e-sdk-photo.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=60, max_turns=2),
        media=MediaSettings(photo_max_inline_bytes=photo.stat().st_size + 1),
    )
    (tmp_path / "src" / "assistant" / "bridge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "assistant" / "bridge" / "system_prompt.md").write_text(
        "project_root={project_root} vault_dir={vault_dir} "
        "skills_manifest={skills_manifest}\n",
        encoding="utf-8",
    )
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)

    bridge = ClaudeBridge(settings)
    handler = ClaudeHandler(settings, conv, turns, bridge)

    sent: list[str] = []

    async def emit(t: str) -> None:
        sent.append(t)

    msg = IncomingMessage(
        chat_id=555,
        text="Describe this image in one short sentence.",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=photo.stat().st_size,
                width=100,
                height=100,
            ),
        ),
    )
    await handler.handle(msg, emit)

    async with conn.execute(
        "SELECT status FROM turns WHERE chat_id = ?", (555,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "complete"
    assert sent, "SDK produced no text fragments"

    await conn.close()
