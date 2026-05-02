"""Phase 9 §2.5 + MED-5 + MED-6 — bridge :class:`ArtefactBlock` parse
tests.

The bridge parses a ToolResultBlock from
``mcp__render_doc__render_doc`` and yields an ArtefactBlock when the
envelope shape matches. Schema-version mismatch logs warning + skips
yield (graceful degradation, MED-6).
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_sdk import ToolResultBlock

from assistant.bridge.claude import _parse_render_doc_artefact_block
from assistant.render_doc import ArtefactBlock


def _envelope_block(envelope: dict) -> ToolResultBlock:
    """Build a ToolResultBlock with one text content carrying the
    JSON-stringified envelope (mirrors what the @tool body returns)."""
    return ToolResultBlock(
        tool_use_id="toolu_test",
        content=[{"type": "text", "text": json.dumps(envelope)}],
        is_error=False,
    )


def test_happy_artefact_envelope_yields_block() -> None:
    env = {
        "ok": True,
        "result": "rendered",
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "path": "/tmp/abc.pdf",
        "suggested_filename": "report.pdf",
        "bytes": 1024,
        "expires_at": "2026-05-02T13:00:00+00:00",
        "tool_use_id": "toolu_test",
    }
    block = _envelope_block(env)
    art = _parse_render_doc_artefact_block(block)
    assert isinstance(art, ArtefactBlock)
    assert art.path == Path("/tmp/abc.pdf")
    assert art.fmt == "pdf"
    assert art.suggested_filename == "report.pdf"
    assert art.tool_use_id == "toolu_test"


def test_failed_envelope_yields_none() -> None:
    """``ok=False`` → no ArtefactBlock; model sees the text envelope
    and explains failure to owner."""
    env = {
        "ok": False,
        "kind": "error",
        "schema_version": 1,
        "reason": "render_failed_input_syntax",
        "error": "weasyprint-url-fetch-blocked",
    }
    assert _parse_render_doc_artefact_block(_envelope_block(env)) is None


def test_unknown_schema_version_yields_none() -> None:
    """MED-6 / AC#28: future schema_version → log warning + skip yield."""
    env = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 2,
        "format": "pdf",
        "path": "/tmp/x.pdf",
        "suggested_filename": "x.pdf",
        "bytes": 100,
    }
    assert _parse_render_doc_artefact_block(_envelope_block(env)) is None


def test_missing_path_yields_none() -> None:
    env = {
        "ok": True,
        "kind": "artefact",
        "schema_version": 1,
        "format": "pdf",
        "suggested_filename": "x.pdf",
        "bytes": 100,
    }
    assert _parse_render_doc_artefact_block(_envelope_block(env)) is None


def test_non_json_content_yields_none() -> None:
    block = ToolResultBlock(
        tool_use_id="toolu_test",
        content=[{"type": "text", "text": "not json at all"}],
        is_error=False,
    )
    assert _parse_render_doc_artefact_block(block) is None


def test_string_content_yields_none() -> None:
    """Some MCP results may carry str content (vs list[dict]); treat
    it the same — non-envelope text → no block."""
    block = ToolResultBlock(
        tool_use_id="toolu_test",
        content="bare string content",
        is_error=False,
    )
    assert _parse_render_doc_artefact_block(block) is None


def test_empty_content_yields_none() -> None:
    block = ToolResultBlock(
        tool_use_id="toolu_test",
        content=None,
        is_error=False,
    )
    assert _parse_render_doc_artefact_block(block) is None
