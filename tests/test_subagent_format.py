"""Phase 6 / commit 4 — notification formatter.

Covers:
  * Footer shape: `[job N <status> in <D>s, kind=<k>(, cost=$.XXXX)?]`.
  * `cost_usd=None` OMITS the `cost=` segment entirely (fix-pack
    HIGH #2 / devil H-7 — phase 6 never populates cost, so the
    pre-fix `$?` placeholder was UX cruft on every notify).
  * `cost_usd` set renders with 4-decimal formatting.
  * UTF-8 truncation respects character boundaries (no half-emoji).
  * Missing timestamps → duration 0s (graceful fallback, not crash).
"""

from __future__ import annotations

from assistant.subagent.format import format_notification
from assistant.subagent.store import SubagentJob


def _mkjob(**overrides: object) -> SubagentJob:
    defaults = dict(
        id=7,
        sdk_agent_id="agent-1",
        sdk_session_id="sess-child",
        parent_session_id="sess-parent",
        agent_type="general",
        task_text="write poem",
        transcript_path=None,
        status="completed",
        cancel_requested=False,
        result_summary=None,
        cost_usd=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
        depth=0,
        created_at="2026-04-17T10:00:00Z",
        started_at="2026-04-17T10:00:00Z",
        finished_at="2026-04-17T10:00:17Z",
    )
    defaults.update(overrides)
    return SubagentJob(**defaults)  # type: ignore[arg-type]


def test_footer_shape_basic() -> None:
    job = _mkjob()
    out = format_notification(
        result_text="hello world",
        job=job,
        max_body_bytes=4096,
    )
    # Body first, then divider, then footer.
    assert out.startswith("hello world")
    assert "\n\n---\n" in out
    # Fix-pack HIGH #2: cost segment OMITTED when NULL (phase 6
    # default). Segments joined with ", " per the reviewer-supplied
    # snippet. Plan §Q4's locked format required the cost segment
    # itself; we preserve the rest of the shape and drop ONLY the
    # NULL-cost case.
    assert "[job 7, completed, in 17s, kind=general]" in out


def test_footer_omits_cost_segment_when_cost_usd_is_none() -> None:
    """Fix-pack HIGH #2 (devil H-7): NULL cost must NOT render as
    `cost=$?`. Phase 6 always stores NULL (GAP #11), so the segment
    would be pure cruft on every notify."""
    job = _mkjob(cost_usd=None)
    out = format_notification(
        result_text="body",
        job=job,
        max_body_bytes=4096,
    )
    # No 'cost=' anywhere in the footer.
    footer = out.split("\n\n---\n")[-1]
    assert "cost=" not in footer, footer
    assert "$?" not in footer, footer


def test_footer_includes_cost_when_set() -> None:
    job = _mkjob(cost_usd=0.1234)
    out = format_notification(
        result_text="ok",
        job=job,
        max_body_bytes=4096,
    )
    assert "cost=$0.1234" in out


def test_truncation_marker_appended_when_over_cap() -> None:
    # 10 KB body, cap 1 KB
    big = "x" * 10_000
    job = _mkjob()
    out = format_notification(
        result_text=big,
        job=job,
        max_body_bytes=1024,
    )
    assert "[truncated]" in out
    # Body portion <= 1024 bytes (plus the truncation marker and footer).
    body_section = out.split("\n\n---\n")[0]
    # Allow +20 bytes for the \n\n[truncated] suffix.
    assert len(body_section.encode("utf-8")) <= 1024 + 30


def test_truncation_preserves_utf8_char_boundary() -> None:
    """UTF-8 multi-byte chars at the cap boundary must not split mid-code-point.
    With `errors='ignore'` on decode, the decode drops the orphan bytes."""
    # Use 3-byte char '€' (0xE2 0x82 0xAC); 4 bytes/char would also do.
    body = "€" * 400  # 1200 bytes
    job = _mkjob()
    out = format_notification(
        result_text=body,
        job=job,
        max_body_bytes=1000,  # arbitrary mid-codepoint cap
    )
    # Must be valid UTF-8 — no UnicodeDecodeError when re-encoded.
    body_section = out.split("\n\n---\n")[0]
    body_section.encode("utf-8")  # should not raise
    assert "[truncated]" in out


def test_missing_timestamps_yields_zero_duration() -> None:
    job = _mkjob(started_at=None, finished_at=None)
    out = format_notification(
        result_text="x",
        job=job,
        max_body_bytes=4096,
    )
    assert "in 0s" in out


def test_unparseable_timestamp_yields_zero_duration() -> None:
    job = _mkjob(started_at="not-an-iso", finished_at="also-not-iso")
    out = format_notification(
        result_text="x",
        job=job,
        max_body_bytes=4096,
    )
    assert "in 0s" in out


def test_trailing_whitespace_stripped_from_body() -> None:
    job = _mkjob()
    out = format_notification(
        result_text="hello  \n\n",
        job=job,
        max_body_bytes=4096,
    )
    assert out.startswith("hello\n")
    # No trailing whitespace immediately before the divider.
    assert "hello\n\n---" in out
