"""Phase 9 — :func:`render_doc` MCP @tool wrapper tests.

Mocks the subsystem so the @tool body's response shaping (envelope
build + reason/error mapping) is exercised in isolation. Real
renderer integration tests live in
``test_phase9_pdf_renderer_integration.py`` (skip-marked).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc.subsystem import RenderDocSubsystem, RenderResult
from assistant.tools_sdk import _render_doc_core


class _FakeSubsystem(RenderDocSubsystem):
    """Minimal stand-in for :class:`RenderDocSubsystem` used by the
    @tool body. Bypasses the real constructor (we don't need
    asyncio.Lock/Semaphore for the @tool body's response shaping)."""

    def __init__(self) -> None:
        # Skip RenderDocSubsystem.__init__ — we only need the handful
        # of attributes the @tool body reads. ``force_disabled`` on
        # the parent is a @property; set the backing flag directly.
        self._force_disabled = False
        self.force_disabled_formats: set[str] = set()
        self._settings = RenderDocSettings()
        self._next_result: RenderResult | None = None
        self.last_args: tuple[Any, ...] = ()

    async def render(  # type: ignore[override]
        self,
        content_md: str,
        fmt: str,
        filename: str | None,
        *,
        task_handle: Any,
    ) -> RenderResult:
        self.last_args = (content_md, fmt, filename)
        if self._next_result is None:
            return RenderResult(
                ok=False,
                fmt=fmt,
                suggested_filename=f"{filename or 'x'}.{fmt}",
                reason="render_failed_internal",
                error="no-result-configured",
            )
        return self._next_result


@pytest.fixture
def fake_subsystem() -> _FakeSubsystem:
    sub = _FakeSubsystem()
    _render_doc_core._CTX["subsystem"] = sub
    _render_doc_core._CONFIGURED = True
    return sub


@pytest.mark.asyncio
async def test_disabled_subsystem_returns_disabled_envelope() -> None:
    """No subsystem configured → envelope ``ok=False, reason='disabled'``."""
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler({"content_md": "hi", "format": "pdf"})
    assert result["ok"] is False
    assert result["reason"] == "disabled"
    assert result["error"] == "subsystem-not-configured"
    # Envelope mirrors fields into "content" so SDK shows it as text.
    assert isinstance(result["content"], list)
    text = result["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["ok"] is False


@pytest.mark.asyncio
async def test_force_disabled_subsystem_returns_disabled(
    fake_subsystem: _FakeSubsystem,
) -> None:
    fake_subsystem._force_disabled = True
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler({"content_md": "hi", "format": "pdf"})
    assert result["ok"] is False
    assert result["reason"] == "disabled"


@pytest.mark.asyncio
async def test_filename_invalid_envelope(
    fake_subsystem: _FakeSubsystem,
) -> None:
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler(
        {
            "content_md": "hi",
            "format": "pdf",
            "filename": "../etc/passwd",
        }
    )
    assert result["ok"] is False
    assert result["reason"] == "filename_invalid"
    assert result["error"].startswith("sanitize-")


@pytest.mark.asyncio
async def test_unknown_format_returns_internal(
    fake_subsystem: _FakeSubsystem,
) -> None:
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler(
        {"content_md": "hi", "format": "rtf"}
    )
    assert result["ok"] is False
    assert result["reason"] == "render_failed_internal"
    assert result["error"] == "format-unknown"


@pytest.mark.asyncio
async def test_happy_path_returns_artefact_envelope(
    fake_subsystem: _FakeSubsystem,
    tmp_path: Path,
) -> None:
    """The @tool body wraps the subsystem's RenderResult into the
    spec §2.3 artefact envelope."""
    fp = tmp_path / "out.pdf"
    fp.write_bytes(b"%PDF-1.4\n")
    fake_subsystem._next_result = RenderResult(
        ok=True,
        fmt="pdf",
        suggested_filename="report.pdf",
        path=fp,
        bytes_out=fp.stat().st_size,
        duration_ms=42,
    )
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler(
        {"content_md": "hi", "format": "pdf", "filename": "report"}
    )
    assert result["ok"] is True
    assert result["kind"] == "artefact"
    assert result["schema_version"] == 1
    assert result["format"] == "pdf"
    assert result["path"] == str(fp)
    assert result["suggested_filename"] == "report.pdf"
    assert result["bytes"] == fp.stat().st_size
    assert "expires_at" in result
    assert "tool_use_id" in result
    # Content text mirrors the dict.
    text = result["content"][0]["text"]
    parsed = json.loads(text)
    assert parsed["ok"] is True


@pytest.mark.asyncio
async def test_subsystem_failure_reason_preserved(
    fake_subsystem: _FakeSubsystem,
) -> None:
    """Subsystem returns ``ok=False`` with reason+error → @tool body
    forwards verbatim (MED-3 granular reasons)."""
    fake_subsystem._next_result = RenderResult(
        ok=False,
        fmt="pdf",
        suggested_filename="x.pdf",
        reason="render_failed_input_syntax",
        error="weasyprint-url-fetch-blocked",
    )
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler(
        {"content_md": "<img src=file:///etc/passwd>", "format": "pdf"}
    )
    assert result["ok"] is False
    assert result["reason"] == "render_failed_input_syntax"
    assert result["error"] == "weasyprint-url-fetch-blocked"


@pytest.mark.asyncio
async def test_input_too_large_via_subsystem(
    fake_subsystem: _FakeSubsystem,
) -> None:
    """When subsystem returns ``input_too_large`` reason, the @tool
    body forwards it. (Subsystem performs the size check; this test
    documents the contract.)"""
    fake_subsystem._next_result = RenderResult(
        ok=False,
        fmt="pdf",
        suggested_filename="x.pdf",
        reason="input_too_large",
        error="content-md-over-cap",
    )
    from assistant.tools_sdk.render_doc import render_doc

    result = await render_doc.handler(
        {"content_md": "x" * 100, "format": "pdf"}
    )
    assert result["ok"] is False
    assert result["reason"] == "input_too_large"
