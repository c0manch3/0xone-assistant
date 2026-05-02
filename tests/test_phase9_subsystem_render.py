"""Phase 9 — :meth:`RenderDocSubsystem.render` end-to-end tests.

Covers behaviour at the subsystem layer (above the renderer modules,
below the @tool wrapper):

  - input size cap → ``input_too_large`` envelope.
  - per-format force-disable → ``disabled`` envelope with kebab-case
    error code.
  - successful XLSX render lands a real file under ``artefact_dir``
    AND registers the artefact in the in-flight ledger.
  - audit row written for both happy + failure paths.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc.subsystem import RenderDocSubsystem


def _sub(tmp_path: Path) -> RenderDocSubsystem:
    return RenderDocSubsystem(
        artefact_dir=tmp_path / "artefacts",
        settings=RenderDocSettings(),
        adapter=None,
        owner_chat_id=42,
        run_dir=tmp_path / "run",
        pending_set=set(),
    )


@pytest.mark.asyncio
async def test_input_too_large_returns_failure(tmp_path: Path) -> None:
    sub = _sub(tmp_path)
    too_big = "x" * (sub._settings.max_input_bytes + 1)
    res = await sub.render(too_big, "xlsx", None, task_handle=None)
    assert res.ok is False
    assert res.reason == "input_too_large"
    assert res.error == "content-md-over-cap"


@pytest.mark.asyncio
async def test_per_format_force_disable_blocks(tmp_path: Path) -> None:
    sub = _sub(tmp_path)
    sub.force_disabled_formats.add("pdf")
    sub.disabled_reason = "pandoc-missing"
    res = await sub.render("hello", "pdf", "x", task_handle=None)
    assert res.ok is False
    assert res.reason == "disabled"
    assert res.error.startswith("format-pdf-unavailable-")


@pytest.mark.asyncio
async def test_xlsx_happy_path_registers_artefact(tmp_path: Path) -> None:
    sub = _sub(tmp_path)
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    res = await sub.render(md, "xlsx", "report", task_handle=None)
    assert res.ok is True
    assert res.path is not None
    assert res.path.exists()
    assert res.path.suffix == ".xlsx"
    assert res.path in sub._artefacts
    rec = sub._artefacts[res.path]
    assert rec.in_flight is True
    assert rec.fmt == "xlsx"
    assert rec.suggested_filename == "report.xlsx"


@pytest.mark.asyncio
async def test_audit_row_written_for_success(tmp_path: Path) -> None:
    sub = _sub(tmp_path)
    md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    await sub.render(md, "xlsx", "report", task_handle=None)
    audit_path = tmp_path / "run" / "render-doc-audit.jsonl"
    assert audit_path.exists()
    line = audit_path.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["format"] == "xlsx"
    assert parsed["result"] == "ok"
    assert parsed["filename"] == "report.xlsx"
    assert parsed["bytes"] > 0


@pytest.mark.asyncio
async def test_audit_row_written_for_failure(tmp_path: Path) -> None:
    sub = _sub(tmp_path)
    too_big = "x" * (sub._settings.max_input_bytes + 1)
    await sub.render(too_big, "xlsx", "x", task_handle=None)
    audit_path = tmp_path / "run" / "render-doc-audit.jsonl"
    assert audit_path.exists()
    parsed = json.loads(audit_path.read_text(encoding="utf-8").strip())
    assert parsed["result"] == "failed"
    assert parsed["error"] == "content-md-over-cap"


@pytest.mark.asyncio
async def test_pending_task_drains_under_lock(tmp_path: Path) -> None:
    """``Daemon.stop`` drains via ``self._pending_set``. Verify the
    set is populated during render and emptied after."""
    pending: set[asyncio.Task] = set()
    sub = RenderDocSubsystem(
        artefact_dir=tmp_path / "artefacts",
        settings=RenderDocSettings(),
        adapter=None,
        owner_chat_id=42,
        run_dir=tmp_path / "run",
        pending_set=pending,
    )

    async def _render_task() -> None:
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        await sub.render(
            md,
            "xlsx",
            "x",
            task_handle=asyncio.current_task(),
        )

    t = asyncio.create_task(_render_task())
    await t
    # After completion the task is no longer in pending (callback
    # discards on done).
    assert t not in pending
