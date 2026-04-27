"""Phase 6a fix-pack — quarantine OSError chain (devil M-W2-5).

If ``Path.rename`` fails (cross-device move, permissions glitch,
non-POSIX filesystem collision), the previous flow logged a warning
and fell through; the outer ``finally`` in ``_handle_locked`` then
``unlink``ed the source file → forensic evidence destroyed.

The fix:
  1. Log the full traceback BEFORE attempting recovery, so the owner
     sees what happened even if the fallback also fails.
  2. Try ``shutil.copy2`` into ``.failed/`` as a fallback.
  3. Do NOT propagate the OSError — propagation would short-circuit
     the Russian reply + ``complete_turn``, leaving the turn stuck
     ``pending``.

This test pins the new behaviour: monkeypatch ``Path.rename`` to
raise; assert ``.failed/`` ends up with a copy of the file, the user
gets the Russian reply, and the turn is marked complete.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.files.extract import ExtractionError
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _NoopBridge(ClaudeBridge):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[dict[str, Any]] = []

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"chat_id": chat_id})
        if False:  # pragma: no cover — generator type contract
            yield None


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


def _drop_attachment(uploads_dir: Path, name: str, content: bytes = b"data") -> Path:
    uploads_dir.mkdir(parents=True, exist_ok=True)
    p = uploads_dir / name
    p.write_bytes(content)
    return p


async def test_quarantine_rename_oserror_falls_back_to_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.rename`` raises ``OSError`` → ``shutil.copy2`` is invoked
    and the file ends up in ``.failed/``. The user STILL gets the
    Russian reply and the turn is marked complete.
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _NoopBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "docx",
        lambda p: (_ for _ in ()).throw(ExtractionError("corrupt DOCX: bad XML")),
    )

    # Force ``Path.rename`` to fail. This MUST come AFTER the extractor
    # patching above (otherwise the extractor would never run).
    real_rename = Path.rename

    def fake_rename(self: Path, target: Any) -> Path:
        # Simulate a cross-device move failure.
        raise OSError("EXDEV: cross-device link failed")

    monkeypatch.setattr(Path, "rename", fake_rename)

    tmp_file = _drop_attachment(
        settings.uploads_dir, "uuid__broken.docx", b"\x00\x01\x02"
    )
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=1,
        message_id=1,
        text="read",
        attachment=tmp_file,
        attachment_kind="docx",
        attachment_filename="broken.docx",
    )
    await handler.handle(msg, emit)

    # Restore so subsequent assertions / fixtures don't see fake_rename.
    monkeypatch.setattr(Path, "rename", real_rename)

    # User STILL got the Russian reply (rename OSError must NOT
    # short-circuit the reply + complete_turn).
    assert any("не смог прочитать файл" in e for e in emitted)

    # Bridge NEVER called.
    assert bridge.calls == []

    # Copy fallback succeeded — ``.failed/`` has the quarantined file.
    quarantine = settings.uploads_dir / ".failed"
    failed_files = list(quarantine.iterdir())
    assert len(failed_files) == 1
    assert failed_files[0].name.startswith("uuid__")
    # Original bytes preserved by copy2.
    assert failed_files[0].read_bytes() == b"\x00\x01\x02"

    # Turn marked complete (status='complete', NOT pending).
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=1"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == "complete"
    await store._conn.close()


async def test_quarantine_rename_and_copy_both_fail_does_not_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ``rename`` AND ``copy2`` raise — handler logs and continues.

    The OSError must NOT propagate out of ``_handle_extraction_failure``;
    propagation would skip the Russian reply and the
    ``complete_turn`` call, leaving the turn stuck ``pending``.
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _NoopBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "docx",
        lambda p: (_ for _ in ()).throw(ExtractionError("corrupt DOCX")),
    )

    real_rename = Path.rename

    def fake_rename(self: Path, target: Any) -> Path:
        raise OSError("rename failed")

    def fake_copy2(src: Any, dst: Any) -> Any:
        raise OSError("copy2 failed")

    monkeypatch.setattr(Path, "rename", fake_rename)
    monkeypatch.setattr(
        "assistant.handlers.message.shutil.copy2", fake_copy2
    )

    tmp_file = _drop_attachment(settings.uploads_dir, "uuid__broken2.docx")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=2,
        message_id=2,
        text="read",
        attachment=tmp_file,
        attachment_kind="docx",
        attachment_filename="broken2.docx",
    )
    # MUST NOT raise.
    await handler.handle(msg, emit)

    monkeypatch.setattr(Path, "rename", real_rename)

    # Russian reply still fired.
    assert any("не смог прочитать файл" in e for e in emitted)
    # Turn marked complete.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=2"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == "complete"
    await store._conn.close()
