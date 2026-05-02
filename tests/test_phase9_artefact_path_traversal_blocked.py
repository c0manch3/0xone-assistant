"""Phase 9 fix-pack F3 (CR-1 + CR-4) — path-traversal defense in
depth at the handler AND bridge layers.

A misbehaving / compromised render_doc @tool body emitting
``path=/etc/passwd`` would otherwise be passed verbatim to
``adapter.send_document`` (Telegram ``FSInputFile``), exfiltrating
any file the daemon process can read. Both layers must reject paths
outside the configured artefact dir + .staging/ subdir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.bridge.claude import _parse_render_doc_artefact_block
from assistant.handlers.message import ClaudeHandler
from assistant.render_doc import ArtefactBlock


class _CaptureAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.docs: list[tuple[int, Path, str | None]] = []

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
        self.docs.append((chat_id, path, suggested_filename))


class _FakeSub:
    def __init__(self, artefact_dir: Path) -> None:
        self._artefact_dir = artefact_dir
        self.delivered: list[Path] = []

    async def mark_delivered(self, path: Path) -> None:
        self.delivered.append(path)


def _make_handler(tmp_path: Path) -> tuple[
    ClaudeHandler, _CaptureAdapter, _FakeSub
]:
    from assistant.config import Settings

    settings = Settings(
        telegram_bot_token="x" * 16, owner_chat_id=1
    )
    artefact_dir = tmp_path / "artefacts"
    artefact_dir.mkdir(parents=True, exist_ok=True)
    (artefact_dir / ".staging").mkdir(parents=True, exist_ok=True)
    adapter = _CaptureAdapter()
    sub = _FakeSub(artefact_dir)
    h = ClaudeHandler.__new__(ClaudeHandler)
    h._adapter = adapter
    h._render_doc = sub  # type: ignore[assignment]
    h._settings = settings  # type: ignore[assignment]
    return h, adapter, sub


@pytest.mark.asyncio
async def test_handler_blocks_path_outside_artefact_dir(
    tmp_path: Path,
) -> None:
    """``art.path=/etc/passwd`` MUST be dropped + Russian text fallback."""
    h, adapter, sub = _make_handler(tmp_path)
    rogue = ArtefactBlock(
        path=Path("/etc/passwd"),
        fmt="pdf",
        suggested_filename="leak.pdf",
        tool_use_id="t-rogue",
    )
    await h._flush_artefacts(123, [rogue])
    # No document call.
    assert adapter.docs == []
    # Russian fallback text was emitted.
    assert any("artefact dir" in text for _, text in adapter.texts)
    # Ledger flag still flipped (mark_delivered called) so the orphan
    # doesn't persist.
    assert sub.delivered == [Path("/etc/passwd")]


@pytest.mark.asyncio
async def test_handler_blocks_staging_subdir_path(
    tmp_path: Path,
) -> None:
    """A path inside ``.staging/`` is treated as escape — staging is
    NOT a valid delivery target."""
    h, adapter, sub = _make_handler(tmp_path)
    artefact_dir = sub._artefact_dir
    staging_path = artefact_dir / ".staging" / "leak.pdf"
    staging_path.write_bytes(b"%PDF-x")
    rogue = ArtefactBlock(
        path=staging_path,
        fmt="pdf",
        suggested_filename="leak.pdf",
        tool_use_id="t-staging",
    )
    await h._flush_artefacts(123, [rogue])
    assert adapter.docs == []
    assert any("artefact dir" in text for _, text in adapter.texts)


@pytest.mark.asyncio
async def test_handler_passes_legitimate_artefact(
    tmp_path: Path,
) -> None:
    """A path INSIDE the artefact dir (not staging) MUST be delivered."""
    h, adapter, sub = _make_handler(tmp_path)
    artefact_dir = sub._artefact_dir
    legit_path = artefact_dir / "abc.pdf"
    legit_path.write_bytes(b"%PDF-x")
    art = ArtefactBlock(
        path=legit_path,
        fmt="pdf",
        suggested_filename="abc.pdf",
        tool_use_id="t-ok",
    )
    await h._flush_artefacts(123, [art])
    assert len(adapter.docs) == 1
    assert adapter.docs[0][1] == legit_path


def _make_tool_result_block(envelope: dict[str, Any]) -> Any:
    """Build a minimal ToolResultBlock-like object."""
    block = MagicMock()
    block.tool_use_id = "tu_test"
    block.content = [{"type": "text", "text": json.dumps(envelope)}]
    return block


def test_bridge_parser_blocks_path_traversal(tmp_path: Path) -> None:
    """``_parse_render_doc_artefact_block`` returns None for any
    envelope whose ``path`` resolves OUTSIDE ``artefact_root``."""
    artefact_root = (tmp_path / "artefacts").resolve()
    artefact_root.mkdir(parents=True, exist_ok=True)
    envelope = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "path": "/etc/passwd",
        "suggested_filename": "leak.pdf",
    }
    result = _parse_render_doc_artefact_block(
        _make_tool_result_block(envelope),
        artefact_root=artefact_root,
    )
    assert result is None


def test_bridge_parser_accepts_legitimate_path(tmp_path: Path) -> None:
    artefact_root = (tmp_path / "artefacts").resolve()
    artefact_root.mkdir(parents=True, exist_ok=True)
    legit = artefact_root / "abc.pdf"
    legit.write_bytes(b"%PDF-x")
    envelope = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "path": str(legit),
        "suggested_filename": "abc.pdf",
    }
    result = _parse_render_doc_artefact_block(
        _make_tool_result_block(envelope),
        artefact_root=artefact_root,
    )
    assert result is not None
    assert result.path == legit


def test_bridge_parser_blocks_staging_path(tmp_path: Path) -> None:
    artefact_root = (tmp_path / "artefacts").resolve()
    staging_root = artefact_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging_path = staging_root / "leak.pdf"
    staging_path.write_bytes(b"%PDF-x")
    envelope = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "path": str(staging_path),
        "suggested_filename": "leak.pdf",
    }
    result = _parse_render_doc_artefact_block(
        _make_tool_result_block(envelope),
        artefact_root=artefact_root,
    )
    assert result is None


def test_bridge_parser_no_artefact_root_skips_check(
    tmp_path: Path,
) -> None:
    """When ``artefact_root=None`` (legacy / test bridges), the parser
    SHOULD skip the path check — the handler-side defense still
    applies. This keeps backward compat for callers that don't yet
    pass the kwarg."""
    envelope = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "path": "/tmp/x.pdf",
        "suggested_filename": "x.pdf",
    }
    result = _parse_render_doc_artefact_block(
        _make_tool_result_block(envelope),
        artefact_root=None,
    )
    assert result is not None
