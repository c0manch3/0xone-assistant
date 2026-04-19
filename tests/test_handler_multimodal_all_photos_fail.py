"""Phase 7 commit 13 — H-14 all-photos-fail scenario.

All three photos in an `IncomingMessage.attachments` tuple become
unreadable mid-turn (simulate the media sweeper racing the handler and
unlinking the inbox files between adapter download and handler run).

Invariants:
* Envelope still built and fed to the bridge — NO CRASH.
* Zero image_blocks, three failure-notes (one per path).
* `log.warning("media_photo_read_failed", ...)` recorded three times.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock
from structlog.testing import capture_logs

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, Settings
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


async def test_all_three_photos_unreadable_still_builds_envelope(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "af.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    # Three absolute paths that DO NOT exist — mimicking sweeper eviction.
    p1 = tmp_path / "gone" / "a.jpg"
    p2 = tmp_path / "gone" / "b.jpg"
    p3 = tmp_path / "gone" / "c.jpg"
    # Do NOT create them — `read_bytes` must raise FileNotFoundError.
    assert not p1.exists() and not p2.exists() and not p3.exists()

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )
    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="ack"),
            _result(),
        ]
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=100,
        text="three photos",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=p1,
                mime_type="image/jpeg",
                file_size=None,
                width=0,
                height=0,
            ),
            MediaAttachment(
                kind="photo",
                local_path=p2,
                mime_type="image/jpeg",
                file_size=None,
                width=0,
                height=0,
            ),
            MediaAttachment(
                kind="photo",
                local_path=p3,
                mime_type="image/jpeg",
                file_size=None,
                width=0,
                height=0,
            ),
        ),
    )

    with capture_logs() as cap:
        await handler.handle(msg, emit)

    # Envelope built and fed to bridge.
    assert bridge.ask_called == 1
    # Zero image blocks — every read failed.
    assert bridge.last_image_blocks is None
    # Three failure-notes, one per path.
    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 3
    for note, path in zip(bridge.last_system_notes, (p1, p2, p3), strict=True):
        assert "read failed" in note
        assert "FileNotFoundError" in note
        assert str(path) in note

    # Three log.warning("media_photo_read_failed", ...) entries.
    warns = [
        e
        for e in cap
        if e.get("log_level") == "warning"
        and e.get("event") == "media_photo_read_failed"
    ]
    assert len(warns) == 3
    seen_paths = {w["path"] for w in warns}
    assert seen_paths == {str(p1), str(p2), str(p3)}

    await conn.close()
