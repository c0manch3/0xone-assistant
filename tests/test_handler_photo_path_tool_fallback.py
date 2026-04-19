"""Phase 7 commit 13 — C-4 silent-drop fix.

Regression guard for the v2 fix-pack: when `MEDIA_PHOTO_MODE=path_tool`
the handler's attachment loop used to fall through silently (only the
`inline_base64` branch existed), dropping photos without even a note.
The explicit `elif att.kind == "photo" and photo_mode == "path_tool":`
branch now emits a note-only envelope so the model still knows a photo
was sent even though inline base64 is disabled.

This test locks that behaviour: `image_blocks` is None, `system_notes`
contains exactly one entry mentioning the `path_tool fallback` and the
photo path.
"""

from __future__ import annotations

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
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None
        self.last_image_blocks: list[dict[str, Any]] | None = None
        self.ask_called = 0

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history
        self.ask_called += 1
        self.last_system_notes = (
            list(system_notes) if system_notes is not None else None
        )
        self.last_image_blocks = (
            list(image_blocks) if image_blocks is not None else None
        )
        for item in self._items:
            yield item


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


async def test_photo_path_tool_mode_emits_note_only_no_silent_drop(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "pt.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    photo = tmp_path / "inbox" / "p.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    # Content irrelevant — the path_tool branch must NOT read the file.
    photo.write_bytes(b"\xff\xd8\xff\xd9")

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(photo_mode="path_tool"),  # type: ignore[arg-type]
    )
    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=55,
        text="here",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=photo,
                mime_type="image/jpeg",
                file_size=photo.stat().st_size,
                width=320,
                height=240,
            ),
        ),
    )
    await handler.handle(msg, emit)

    # The envelope was built and passed to the bridge — no silent drop.
    assert bridge.ask_called == 1
    # Image bytes MUST NOT be inlined under path_tool.
    assert bridge.last_image_blocks is None
    # Note MUST exist and reference the fallback + the path + dimensions.
    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 1
    note = bridge.last_system_notes[0]
    assert "path_tool fallback" in note
    assert str(photo) in note
    assert "320x240" in note

    await conn.close()
