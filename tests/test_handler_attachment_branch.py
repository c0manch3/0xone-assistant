"""Phase 6a — handler attachment branch unit tests.

Covers:
- PDF (Option C) path: system-note appended; tmp file unlinked in
  ``finally`` even on bridge error;
- DOCX (Option B) path: extractor invoked; user_text_for_sdk includes
  the extracted text + ``[attached: NAME]`` marker;
- ExtractionError → quarantine to ``.failed/`` + Russian reply;
- Encrypted-error message → "файл зашифрован" reply variant;
- Bridge exception path also unlinks tmp file;
- Defensive total-cap truncation: > POST_EXTRACT_CHAR_CAP gets
  truncated marker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import ClaudeSettings, Settings
from assistant.files.extract import POST_EXTRACT_CHAR_CAP, ExtractionError
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

# ----------------------------------------------------------------------
# Test infrastructure (mirrors tests/test_claude_handler.py)
# ----------------------------------------------------------------------


class _CapturingBridge(ClaudeBridge):
    """Captures the user_text + system_notes the handler sends in."""

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
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
        stop_reason="end_turn",
    )


def _make_attachment(tmp_path: Path, name: str, content: bytes = b"x") -> Path:
    """Drop a real file into the uploads_dir so unlink/exists checks work.

    Mirrors ``Settings.uploads_dir`` Mac-dev fallback (``<data_dir>/uploads``)
    so the path reported by the handler matches the file we wrote.
    """
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / name
    p.write_bytes(content)
    return p


# ----------------------------------------------------------------------
# PDF Option C path
# ----------------------------------------------------------------------


async def test_pdf_option_c_appends_system_note_and_unlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF kind under Option C (forced via monkeypatch): extractor NOT
    invoked; system-note tells the model to use the Read tool. Tmp file
    is unlinked in ``finally`` after the bridge call completes.

    NOTE: phase-6a live probe (2026-04-27) showed claude-opus-4-7
    ignores the system-note and goes to Bash; ``_is_pdf_native_read``
    flipped to ``return False`` globally. This test still exercises
    Option C semantics by monkeypatching back to True — that path is
    retained as a future re-enable knob.
    """
    from claude_agent_sdk import TextBlock

    monkeypatch.setattr(
        "assistant.handlers.message._is_pdf_native_read",
        lambda kind: kind == "pdf",
    )

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="ok"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    tmp_file = _make_attachment(tmp_path, "abc__sample.pdf")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=1,
        message_id=1,
        text="describe",
        attachment=tmp_file,
        attachment_kind="pdf",
        attachment_filename="sample.pdf",
    )
    await handler.handle(msg, emit)

    assert emitted == ["ok"]
    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    # No pre-extract for PDFs (under Option C).
    assert call["user_text"] == "describe"
    notes = call["system_notes"] or []
    pdf_notes = [n for n in notes if "Read(file_path=" in n]
    assert len(pdf_notes) == 1
    assert "sample.pdf" in pdf_notes[0]

    # Tmp file unlinked.
    assert not tmp_file.exists()
    await store._conn.close()


async def test_pdf_option_c_unlinks_on_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bridge raises → tmp file STILL unlinked (finally clause).

    Forces Option C via monkeypatch (production default is now Option B
    after the 2026-04-27 live-probe finding).
    """
    monkeypatch.setattr(
        "assistant.handlers.message._is_pdf_native_read",
        lambda kind: kind == "pdf",
    )

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, ClaudeBridgeError("timeout"))
    handler = ClaudeHandler(settings, store, bridge)

    tmp_file = _make_attachment(tmp_path, "uuid__file.pdf")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=2,
        message_id=2,
        text="ping",
        attachment=tmp_file,
        attachment_kind="pdf",
        attachment_filename="file.pdf",
    )
    await handler.handle(msg, emit)

    assert any("ошибка" in e for e in emitted)
    assert not tmp_file.exists()
    await store._conn.close()


async def test_pdf_option_b_default_pre_extracts_via_pypdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase-6a regression: default `_is_pdf_native_read` returns False
    so PDFs go through `EXTRACTORS["pdf"]` (pypdf, Option B uniform).
    """
    from claude_agent_sdk import TextBlock

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "pdf",
        lambda p: ("EXTRACTED-PDF-TEXT", 18),
    )

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="ok"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    tmp_file = _make_attachment(tmp_path, "abc__sample.pdf")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=11,
        message_id=11,
        text="describe",
        attachment=tmp_file,
        attachment_kind="pdf",
        attachment_filename="sample.pdf",
    )
    await handler.handle(msg, emit)

    assert emitted == ["ok"]
    assert len(bridge.calls) == 1
    call = bridge.calls[0]
    # Option B: extracted text injected into user_text envelope.
    assert "EXTRACTED-PDF-TEXT" in call["user_text"]
    assert "[attached: sample.pdf]" in call["user_text"]
    notes = call["system_notes"] or []
    # NO Read system-note under Option B.
    assert all("Read(file_path=" not in n for n in notes)

    # Tmp file unlinked.
    assert not tmp_file.exists()
    await store._conn.close()


# ----------------------------------------------------------------------
# DOCX Option B path
# ----------------------------------------------------------------------


async def test_docx_option_b_extracts_and_injects_into_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOCX kind: extractor invoked; ``user_text_for_sdk`` includes the
    extracted text + ``[attached: NAME]`` marker.
    """
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="reply"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    # Patch the EXTRACTORS map's docx entry.
    fake_text = "Extracted document body text."
    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "docx",
        lambda p: (fake_text, len(fake_text)),
    )

    tmp_file = _make_attachment(tmp_path, "uuid__doc.docx")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=3,
        message_id=3,
        text="explain",
        attachment=tmp_file,
        attachment_kind="docx",
        attachment_filename="doc.docx",
    )
    await handler.handle(msg, emit)

    call = bridge.calls[0]
    assert "[attached: doc.docx]" in call["user_text"]
    assert fake_text in call["user_text"]
    assert call["user_text"].startswith("explain")
    assert not tmp_file.exists()
    await store._conn.close()


async def test_docx_option_b_total_cap_truncates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive POST_EXTRACT_CHAR_CAP: > 200K char extract gets the
    ``[…truncated at 200000 chars]`` marker.
    """
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="ok"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    huge = "x" * (POST_EXTRACT_CHAR_CAP + 1000)
    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "txt",
        lambda p: (huge, len(huge)),
    )

    tmp_file = _make_attachment(tmp_path, "uuid__big.txt")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=4,
        message_id=4,
        text="summarize",
        attachment=tmp_file,
        attachment_kind="txt",
        attachment_filename="big.txt",
    )
    await handler.handle(msg, emit)

    call = bridge.calls[0]
    assert "truncated at 200000 chars" in call["user_text"]
    await store._conn.close()


# ----------------------------------------------------------------------
# ExtractionError → quarantine + Russian reply
# ----------------------------------------------------------------------


async def test_extraction_error_quarantines_and_replies_russian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "docx",
        lambda p: (_ for _ in ()).throw(ExtractionError("corrupt DOCX: bad XML")),
    )

    tmp_file = _make_attachment(tmp_path, "uuid__broken.docx")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=5,
        message_id=5,
        text="read",
        attachment=tmp_file,
        attachment_kind="docx",
        attachment_filename="broken.docx",
    )
    await handler.handle(msg, emit)

    # Reply surfaces the not-encrypted variant.
    assert any("не смог прочитать файл" in e for e in emitted)

    # Bridge NEVER called — turn short-circuited at extraction.
    assert bridge.calls == []

    # File quarantined inside .failed/.
    quarantine = settings.uploads_dir / ".failed"
    failed_files = list(quarantine.iterdir())
    assert len(failed_files) == 1
    assert failed_files[0].name.startswith("uuid__")

    # Tmp file no longer in the top-level uploads dir.
    assert not tmp_file.exists()

    # Turn marked complete with synthetic meta.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=5"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == "complete"
    await store._conn.close()


async def test_extraction_error_encrypted_uses_specific_russian_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reason-string containing "encrypted" → "файл зашифрован" reply."""
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(settings, [_result_message()])
    handler = ClaudeHandler(settings, store, bridge)

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "pdf",
        lambda p: (_ for _ in ()).throw(ExtractionError("encrypted PDF")),
    )
    # Force fallback through extractor: flip discriminator.
    monkeypatch.setattr(
        "assistant.handlers.message._is_pdf_native_read",
        lambda kind: False,
    )

    tmp_file = _make_attachment(tmp_path, "uuid__locked.pdf")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=6,
        message_id=6,
        text="open",
        attachment=tmp_file,
        attachment_kind="pdf",
        attachment_filename="locked.pdf",
    )
    await handler.handle(msg, emit)

    assert any("зашифрован" in e for e in emitted)
    assert not tmp_file.exists()
    await store._conn.close()


# ----------------------------------------------------------------------
# Forensics marker in user row
# ----------------------------------------------------------------------


async def test_user_row_persists_marker_not_extracted_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted user row contains the marker
    ``[file: NAME]`` — NOT the extracted bytes (keeps conversations
    table small).
    """
    from claude_agent_sdk import TextBlock

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _CapturingBridge(
        settings, [TextBlock(text="reply"), _result_message()]
    )
    handler = ClaudeHandler(settings, store, bridge)

    monkeypatch.setitem(
        __import__("assistant.handlers.message", fromlist=["EXTRACTORS"]).EXTRACTORS,
        "txt",
        lambda p: ("LARGE EXTRACTED PAYLOAD" * 100, 2300),
    )

    tmp_file = _make_attachment(tmp_path, "uuid__file.txt")
    _, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=7,
        message_id=7,
        text="explain",
        attachment=tmp_file,
        attachment_kind="txt",
        attachment_filename="file.txt",
    )
    await handler.handle(msg, emit)

    async with store._conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id=7 AND role='user'"
    ) as cur:
        rows = await cur.fetchall()
    import json

    payload = json.loads(rows[0][0])
    text_block = payload[0]["text"]
    assert "[file: file.txt]" in text_block
    assert "explain" in text_block
    # Extracted bytes NOT persisted.
    assert "LARGE EXTRACTED PAYLOAD" not in text_block
    await store._conn.close()
