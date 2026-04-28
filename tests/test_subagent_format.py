"""Phase 6: notify formatter (footer shape, truncation, cost-omit)."""

from __future__ import annotations

import pytest

from assistant.subagent.notify import (
    _compute_duration_s,
    _truncate_utf8,
    format_notification,
)
from assistant.subagent.store import SubagentJob


def _job(
    *,
    job_id: int = 17,
    agent_type: str = "general",
    status: str = "completed",
    cost_usd: float | None = None,
    started_at: str | None = "2026-04-27T12:00:00Z",
    finished_at: str | None = "2026-04-27T12:02:30Z",
    created_at: str = "2026-04-27T11:59:59Z",
) -> SubagentJob:
    return SubagentJob(
        id=job_id,
        sdk_agent_id="agent-x",
        sdk_session_id=None,
        parent_session_id=None,
        agent_type=agent_type,
        task_text="task",
        transcript_path=None,
        status=status,
        cancel_requested=False,
        result_summary=None,
        cost_usd=cost_usd,
        callback_chat_id=42,
        spawned_by_kind="tool",
        spawned_by_ref=None,
        attempts=0,
        last_error=None,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
    )


def test_footer_locked_format() -> None:
    body = format_notification(
        result_text="hello world",
        job=_job(),
        max_body_bytes=1024,
    )
    assert body.startswith("hello world")
    assert "---\n" in body
    assert "[job 17, completed, in 150s, kind=general]" in body


def test_footer_includes_cost_when_present() -> None:
    body = format_notification(
        result_text="x",
        job=_job(cost_usd=0.012345),
        max_body_bytes=1024,
    )
    assert "cost=$0.0123" in body
    assert "kind=general" in body


def test_footer_omits_cost_when_null() -> None:
    """Description.md §criteria + research RQ4: when cost_usd IS NULL
    the footer must omit the cost segment entirely (no 'cost=$?')."""
    body = format_notification(
        result_text="x", job=_job(cost_usd=None), max_body_bytes=1024
    )
    assert "cost=" not in body
    assert "[job 17, completed, in 150s, kind=general]" in body


def test_truncation_under_cap_passes_through() -> None:
    body = format_notification(
        result_text="short", job=_job(), max_body_bytes=10_000
    )
    assert "[truncated]" not in body


def test_truncation_over_cap_appends_marker() -> None:
    body = format_notification(
        result_text="x" * 10_000,
        job=_job(),
        max_body_bytes=100,
    )
    assert "[truncated]" in body


def test_truncate_utf8_keeps_codepoint_boundary() -> None:
    """Cyrillic safe — never splits a multi-byte codepoint."""
    text = "Привет " * 50
    out = _truncate_utf8(text, max_bytes=20)
    # Encoding the truncated body must succeed without UnicodeError.
    assert out.encode("utf-8")
    assert "[truncated]" in out


def test_truncate_utf8_no_truncation_for_short() -> None:
    out = _truncate_utf8("hello", max_bytes=100)
    assert out == "hello"


def test_compute_duration_started_to_finished() -> None:
    job = _job(
        started_at="2026-04-27T12:00:00Z",
        finished_at="2026-04-27T12:00:30Z",
    )
    assert _compute_duration_s(job) == pytest.approx(30.0)


def test_compute_duration_falls_back_to_created_at() -> None:
    job = _job(
        started_at=None,
        finished_at="2026-04-27T12:00:30Z",
        created_at="2026-04-27T12:00:00Z",
    )
    assert _compute_duration_s(job) == pytest.approx(30.0)


def test_compute_duration_unparseable_returns_zero() -> None:
    job = _job(started_at="bogus", finished_at=None, created_at="bogus")
    assert _compute_duration_s(job) == 0.0


def test_status_is_lowercase_in_footer() -> None:
    body = format_notification(
        result_text="x",
        job=_job(status="failed"),
        max_body_bytes=1024,
    )
    assert "[job 17, failed" in body


def test_body_strip_leading_trailing_whitespace() -> None:
    body = format_notification(
        result_text="\n\n  hello  \n\n",
        job=_job(),
        max_body_bytes=1024,
    )
    assert body.startswith("hello")


def test_kind_appears_in_footer() -> None:
    body = format_notification(
        result_text="x",
        job=_job(agent_type="researcher"),
        max_body_bytes=1024,
    )
    assert "kind=researcher" in body


def test_format_notification_minimum_complete_shape() -> None:
    """Sanity: the joined string parses as expected for downstream
    Telegram chunker (existing split_for_telegram is bytes-aware)."""
    body = format_notification(
        result_text="result",
        job=_job(),
        max_body_bytes=1024,
    )
    assert body.endswith("]")
