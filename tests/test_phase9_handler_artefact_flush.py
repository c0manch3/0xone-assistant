"""Phase 9 §2.5 — handler ``_flush_artefacts`` partial-failure tests.

The handler-side flush helper is exercised in isolation via a fake
adapter + fake subsystem. Real bridge integration is covered by
``test_phase9_bridge_artefact_block.py``.

Covers:
  - happy path: every artefact dispatched + ``mark_delivered`` called.
  - HIGH-3 partial failure: a raising send_document → text fallback +
    ``mark_delivered`` still called + loop continues.
  - HIGH-6: ``NotImplementedError`` from adapter → handler logs +
    text fallback (no crash).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.handlers.message import ClaudeHandler
from assistant.render_doc import ArtefactBlock


class _CaptureAdapter(MessengerAdapter):
    """Records send_text + send_document calls for assertion."""

    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.docs: list[tuple[int, Path, str | None]] = []
        self.fail_first_doc: bool = False
        self._first_called: bool = False
        self.raise_not_implemented: bool = False

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_document(  # type: ignore[override]
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        suggested_filename: str | None = None,
    ) -> None:
        if self.raise_not_implemented:
            raise NotImplementedError("test")
        if self.fail_first_doc and not self._first_called:
            self._first_called = True
            # Mimic a transient "network" failure on the first send.
            class _Network(Exception):
                pass

            raise _Network("network")
        self._first_called = True
        self.docs.append((chat_id, path, suggested_filename))


class _FakeSub:
    """Fake subsystem; captures mark_delivered."""

    def __init__(self) -> None:
        self.delivered: list[Path] = []

    async def mark_delivered(self, path: Path) -> None:
        self.delivered.append(path)


@pytest.fixture
def handler(tmp_path: Path) -> tuple[ClaudeHandler, _CaptureAdapter, _FakeSub]:
    from assistant.config import Settings

    # Minimal settings; we don't exercise the rest.
    settings = Settings(
        telegram_bot_token="x" * 16, owner_chat_id=1
    )
    adapter = _CaptureAdapter()
    fake_sub = _FakeSub()
    h = ClaudeHandler.__new__(ClaudeHandler)
    h._adapter = adapter
    h._render_doc = fake_sub  # type: ignore[assignment]
    h._settings = settings  # type: ignore[assignment]
    return h, adapter, fake_sub


@pytest.mark.asyncio
async def test_happy_flush_dispatches_all(
    handler: tuple[ClaudeHandler, _CaptureAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    h, adapter, sub = handler
    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.docx"
    arts = [
        ArtefactBlock(
            path=p1, fmt="pdf", suggested_filename="a.pdf",
            tool_use_id="t1",
        ),
        ArtefactBlock(
            path=p2, fmt="docx", suggested_filename="b.docx",
            tool_use_id="t2",
        ),
    ]
    await h._flush_artefacts(123, arts)
    assert len(adapter.docs) == 2
    assert adapter.docs[0][1] == p1
    assert adapter.docs[1][1] == p2
    assert sub.delivered == [p1, p2]


@pytest.mark.asyncio
async def test_partial_failure_emits_text_fallback_and_continues(
    handler: tuple[ClaudeHandler, _CaptureAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    h, adapter, sub = handler
    adapter.fail_first_doc = True
    p1 = tmp_path / "a.pdf"
    p2 = tmp_path / "b.docx"
    arts = [
        ArtefactBlock(
            path=p1, fmt="pdf", suggested_filename="a.pdf",
            tool_use_id="t1",
        ),
        ArtefactBlock(
            path=p2, fmt="docx", suggested_filename="b.docx",
            tool_use_id="t2",
        ),
    ]
    await h._flush_artefacts(123, arts)
    # First doc raised; second still delivered.
    assert len(adapter.docs) == 1
    assert adapter.docs[0][1] == p2
    # Text fallback for first.
    assert any("a.pdf" in t for _, t in adapter.texts)
    # Both records mark_delivered (HIGH-3 finally).
    assert sub.delivered == [p1, p2]


@pytest.mark.asyncio
async def test_not_implemented_handled_gracefully(
    handler: tuple[ClaudeHandler, _CaptureAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    """HIGH-6: adapter without document-out raises
    NotImplementedError; handler logs + text fallback + continues."""
    h, adapter, sub = handler
    adapter.raise_not_implemented = True
    p = tmp_path / "x.pdf"
    arts = [
        ArtefactBlock(
            path=p, fmt="pdf", suggested_filename="x.pdf",
            tool_use_id="t",
        )
    ]
    await h._flush_artefacts(7, arts)
    assert any("x.pdf" in t for _, t in adapter.texts)
    assert sub.delivered == [p]
