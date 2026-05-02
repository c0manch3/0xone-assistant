"""Phase 9 — :class:`RenderDocSettings` validator + defaults tests.

Covers spec §2.9:

  - default construction is self-consistent (validator does NOT
    reject the deliberately-undersized default ``render_drain_timeout_s``
    per W2-MED-3 NOTE — the default uses ``model_fields_set`` to
    distinguish owner-set vs. default).
  - ``tool_timeout_s < pdf_pandoc + pdf_weasyprint`` is rejected.
  - PDF/DOCX/XLSX size caps must be ≤ 20 MiB (LOW-4 — Telegram cap).
  - Owner-set ``render_drain_timeout_s`` below worst-case PDF
    pipeline is rejected (W2-MED-3).
  - ``pandoc_sigterm + pandoc_sigkill > render_drain`` is rejected
    when drain > 0 (W2-HIGH-1 honesty).
  - Default ``enabled`` is True (owner explicitly asked for the
    feature in scope).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.config import RenderDocSettings


def test_defaults_construct_cleanly() -> None:
    """Defaults must self-validate — fresh checkout boot-time
    construction must not crash."""
    s = RenderDocSettings()
    assert s.enabled is True
    assert s.artefact_ttl_s == 600
    assert s.sweep_interval_s == 60
    assert s.cleanup_threshold_s == 86400
    assert s.max_input_bytes == 1_048_576
    assert s.tool_timeout_s == 60
    assert s.render_max_concurrent == 2
    assert s.audit_log_max_size_mb == 10
    assert s.audit_log_keep_last_n == 5
    assert s.pdf_pandoc_timeout_s == 20
    assert s.pdf_weasyprint_timeout_s == 30
    assert s.pdf_max_bytes == 20 * 1024 * 1024
    assert s.docx_pandoc_timeout_s == 15
    assert s.docx_max_bytes == 10 * 1024 * 1024
    assert s.xlsx_max_rows == 5000
    assert s.xlsx_max_cols == 50
    assert s.xlsx_max_bytes == 10 * 1024 * 1024
    assert s.render_drain_timeout_s == 20.0
    assert s.pandoc_sigterm_grace_s == 5.0
    assert s.pandoc_sigkill_grace_s == 5.0
    assert s.audit_field_truncate_chars == 256


def test_tool_timeout_below_pdf_pipeline_rejected() -> None:
    """``tool_timeout_s`` must accommodate worst-case PDF pipeline."""
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(
            tool_timeout_s=10,
            pdf_pandoc_timeout_s=20,
            pdf_weasyprint_timeout_s=30,
        )
    assert "tool_timeout_s" in str(ei.value)


def test_pdf_max_bytes_above_telegram_cap_rejected() -> None:
    """LOW-4: PDF cap must be ≤ Telegram send_document cap (20 MiB)."""
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(pdf_max_bytes=25 * 1024 * 1024)
    assert "pdf_max_bytes" in str(ei.value)


def test_docx_max_bytes_above_telegram_cap_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(docx_max_bytes=25 * 1024 * 1024)
    assert "docx_max_bytes" in str(ei.value)


def test_xlsx_max_bytes_above_telegram_cap_rejected() -> None:
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(xlsx_max_bytes=25 * 1024 * 1024)
    assert "xlsx_max_bytes" in str(ei.value)


def test_render_max_concurrent_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        RenderDocSettings(render_max_concurrent=0)


def test_owner_set_render_drain_below_pipeline_sum_rejected() -> None:
    """W2-MED-3: owner-set value below sum is a logic error.

    The default-default 20s drain is allowed (deliberate trade-off
    per W2-HIGH-1 honesty paragraph), but if the owner explicitly
    sets ``RENDER_DOC_RENDER_DRAIN_TIMEOUT_S=15`` (or any non-zero
    value below the sum) the validator MUST reject.
    """
    # Default tool_timeout_s=60 satisfies the tool-timeout invariant.
    # Owner-set drain=15 with default pdf timeouts (20+30=50) → reject.
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(render_drain_timeout_s=15.0)
    assert "render_drain_timeout_s" in str(ei.value)


def test_owner_set_render_drain_zero_explicit_no_drain_accepted() -> None:
    """W2-MED-3: explicit zero is the no-drain opt-out path."""
    s = RenderDocSettings(render_drain_timeout_s=0.0)
    assert s.render_drain_timeout_s == 0.0


def test_pandoc_grace_must_fit_drain() -> None:
    """W2-HIGH-1: SIGTERM + SIGKILL grace must fit inside drain."""
    with pytest.raises(ValidationError) as ei:
        RenderDocSettings(
            render_drain_timeout_s=60.0,
            pandoc_sigterm_grace_s=40.0,
            pandoc_sigkill_grace_s=30.0,
        )
    assert "pandoc_sigterm_grace_s" in str(ei.value)
