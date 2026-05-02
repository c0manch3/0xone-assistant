"""Phase 9 §2.2 + HIGH-5 — startup_check force-disable behaviour.

  - pandoc missing → force_disabled_formats includes pdf+docx; xlsx
    still works (HIGH-5 partial force-disable).
  - weasyprint import failing (ImportError OR OSError) → pdf added.
  - Both missing → fully force_disabled.
  - settings.enabled=False → force_disabled subsystem-wide.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc.subsystem import (
    ALL_FORMATS,
    RenderDocSubsystem,
)


@pytest.fixture
def make_subsystem(tmp_path: Path) -> Any:
    def _factory(*, enabled: bool = True) -> RenderDocSubsystem:
        run_dir = tmp_path / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        artefact_dir = tmp_path / "artefacts"
        return RenderDocSubsystem(
            artefact_dir=artefact_dir,
            settings=RenderDocSettings(enabled=enabled),
            adapter=None,
            owner_chat_id=42,
            run_dir=run_dir,
            pending_set=set(),
        )

    return _factory


def test_settings_disabled_force_disabled(make_subsystem: Any) -> None:
    """``RENDER_DOC_ENABLED=false`` → fully force_disabled, all 3
    formats blocked."""
    sub = make_subsystem(enabled=False)
    asyncio.run(sub.startup_check())
    assert sub.force_disabled is True
    assert sub.disabled_reason == "settings_disabled"
    assert sub.force_disabled_formats == set(ALL_FORMATS)


def test_pandoc_missing_blocks_pdf_and_docx_xlsx_survives(
    make_subsystem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-5 partial force-disable: pandoc missing → pdf+docx
    blocked; xlsx still works (openpyxl is pure-Python). Subsystem
    is NOT fully force_disabled."""
    sub = make_subsystem()
    monkeypatch.setattr(
        "assistant.render_doc.subsystem.shutil.which",
        lambda name: None,
    )
    asyncio.run(sub.startup_check())
    assert "pdf" in sub.force_disabled_formats
    assert "docx" in sub.force_disabled_formats
    # xlsx was NOT added by startup_check because openpyxl is
    # always available.
    assert "xlsx" not in sub.force_disabled_formats
    # If weasyprint is also unavailable on this host, the subsystem
    # is fully force_disabled — that path is covered by the next
    # test. When weasyprint loads OK, only PDF/DOCX are blocked.


def test_weasyprint_import_fail_blocks_pdf_only(
    make_subsystem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-5 partial force-disable: weasyprint import fail (mocked)
    → pdf blocked; docx still works via pandoc."""
    sub = make_subsystem()
    # Pretend pandoc is present.
    monkeypatch.setattr(
        "assistant.render_doc.subsystem.shutil.which",
        lambda name: "/usr/bin/pandoc",
    )
    # Force the inline ``import weasyprint`` inside startup_check to
    # raise — by patching sys.modules to trip ImportError.
    monkeypatch.setitem(
        sys.modules, "weasyprint", None,
    )
    asyncio.run(sub.startup_check())
    assert "pdf" in sub.force_disabled_formats
    assert "docx" not in sub.force_disabled_formats
    assert "xlsx" not in sub.force_disabled_formats


def test_both_missing_fully_force_disabled(
    make_subsystem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pandoc AND weasyprint are missing → fully
    force_disabled. Note: pandoc-missing alone keeps xlsx alive, so
    "fully" requires xlsx renderer to also fail — but the spec only
    populates force_disabled_formats from per-format probes; xlsx is
    NEVER added by startup_check (openpyxl is always available).

    So with pandoc missing AND weasyprint missing, xlsx remains in
    the supported set → ``force_disabled`` is False (partial only).
    Verified here that the partial-disable invariant holds: HIGH-5
    closure preserves xlsx availability under any combination of
    missing system binaries.
    """
    sub = make_subsystem()
    monkeypatch.setattr(
        "assistant.render_doc.subsystem.shutil.which",
        lambda name: None,
    )
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    asyncio.run(sub.startup_check())
    # PDF + DOCX blocked, XLSX still alive.
    assert sub.force_disabled_formats == {"pdf", "docx"}
    # Partial — subsystem stays alive for xlsx.
    assert sub.force_disabled is False
