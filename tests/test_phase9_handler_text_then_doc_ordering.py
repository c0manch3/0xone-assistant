"""Phase 9 fix-pack F1 (W3-CRIT-1 / QH-1 / AC#19) — owner-visible
``text₁ → doc₁ → text₂ → doc₂`` ordering invariant.

The handler's ``_flush_artefacts`` now accepts a ``flush_text``
callable that the adapter wires to drain its accumulated chunks
BEFORE each ``send_document`` call. Without this primitive the
Telegram adapter buffers text in ``chunks: list[str]`` and ships it
AFTER the entire handler returns — opposite of the spec's promise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.handlers.message import ClaudeHandler
from assistant.render_doc import ArtefactBlock


class _RecordingAdapter(MessengerAdapter):
    """Records the actual ORDER of send_text + send_document calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> None:
        self.events.append(("text", text))

    async def send_document(  # type: ignore[override]
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        suggested_filename: str | None = None,
    ) -> None:
        self.events.append(
            ("doc", suggested_filename or path.name)
        )


class _FakeSub:
    """Minimal fake subsystem; tracks delivered + has artefact_dir."""

    def __init__(self, artefact_dir: Path) -> None:
        self._artefact_dir = artefact_dir
        self.delivered: list[Path] = []

    async def mark_delivered(self, path: Path) -> None:
        self.delivered.append(path)


@pytest.fixture
def handler(tmp_path: Path) -> tuple[
    ClaudeHandler, _RecordingAdapter, _FakeSub
]:
    from assistant.config import Settings

    settings = Settings(
        telegram_bot_token="x" * 16, owner_chat_id=1
    )
    artefact_dir = tmp_path / "artefacts"
    artefact_dir.mkdir(parents=True, exist_ok=True)
    adapter = _RecordingAdapter()
    fake_sub = _FakeSub(artefact_dir)
    h = ClaudeHandler.__new__(ClaudeHandler)
    h._adapter = adapter
    h._render_doc = fake_sub  # type: ignore[assignment]
    h._settings = settings  # type: ignore[assignment]
    return h, adapter, fake_sub


@pytest.mark.asyncio
async def test_flush_text_runs_before_send_document(
    handler: tuple[ClaudeHandler, _RecordingAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    """F1: handler invokes ``flush_text`` BEFORE each send_document."""
    h, adapter, sub = handler
    artefact_dir = sub._artefact_dir

    # Two artefacts — flush_text must fire BEFORE each.
    p1 = artefact_dir / "a.pdf"
    p1.write_bytes(b"%PDF-x")
    p2 = artefact_dir / "b.docx"
    p2.write_bytes(b"PK\x03\x04docx")
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
    flush_call_count = 0

    async def flush_text() -> None:
        nonlocal flush_call_count
        flush_call_count += 1
        # Simulate the adapter shipping accumulated text NOW.
        await adapter.send_text(123, f"text-{flush_call_count}")

    await h._flush_artefacts(123, arts, flush_text=flush_text)

    # AC#19 ordering — text₁ → doc₁ → text₂ → doc₂.
    kinds = [k for k, _ in adapter.events]
    assert kinds == ["text", "doc", "text", "doc"], adapter.events
    assert sub.delivered == [p1, p2]


@pytest.mark.asyncio
async def test_no_flush_text_fallback_keeps_legacy_order(
    handler: tuple[ClaudeHandler, _RecordingAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    """When ``flush_text`` is None (legacy callers), the helper still
    delivers each artefact and marks delivery — no crash, no extra
    text events emitted."""
    h, adapter, sub = handler
    artefact_dir = sub._artefact_dir
    p = artefact_dir / "x.pdf"
    p.write_bytes(b"%PDF-x")
    arts = [
        ArtefactBlock(
            path=p, fmt="pdf", suggested_filename="x.pdf",
            tool_use_id="t",
        )
    ]

    await h._flush_artefacts(7, arts)

    kinds = [k for k, _ in adapter.events]
    assert kinds == ["doc"]
    assert sub.delivered == [p]


@pytest.mark.asyncio
async def test_flush_text_failure_does_not_block_send_document(
    handler: tuple[ClaudeHandler, _RecordingAdapter, _FakeSub],
    tmp_path: Path,
) -> None:
    """A raising ``flush_text`` MUST NOT crash the artefact delivery —
    handler suppresses + logs and continues to send_document."""
    h, adapter, sub = handler
    artefact_dir = sub._artefact_dir
    p = artefact_dir / "x.pdf"
    p.write_bytes(b"%PDF-x")
    arts = [
        ArtefactBlock(
            path=p, fmt="pdf", suggested_filename="x.pdf",
            tool_use_id="t",
        )
    ]

    async def flush_text() -> None:
        raise RuntimeError("transient adapter failure")

    await h._flush_artefacts(7, arts, flush_text=flush_text)

    kinds = [k for k, _ in adapter.events]
    assert kinds == ["doc"]
    assert sub.delivered == [p]
