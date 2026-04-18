"""Notification body formatter for phase-6 subagent completions.

Pure-function helpers: `format_notification` composes the Telegram body
from the subagent's final assistant text plus a metadata footer (kind,
status, duration, cost). The footer format is locked by plan §Q4 /
description.md §criteria — any change requires a plan update.

UTF-8 truncation respects character boundaries so a mid-emoji chop
doesn't ship invalid bytes to Telegram.
"""

from __future__ import annotations

from datetime import UTC, datetime

from assistant.subagent.store import SubagentJob


def format_notification(
    *,
    result_text: str,
    job: SubagentJob,
    max_body_bytes: int,
) -> str:
    """Render the final Telegram notify body.

    Footer format (locked):
        [job {id} {status} in {duration_s}s, kind={kind}, cost=${cost}]

    `max_body_bytes` caps the RESULT portion (the subagent's assistant
    text) in UTF-8 bytes — not characters — so the Telegram chunker
    doesn't get a half-emoji. If truncation happens we append the
    `[truncated]` marker before the footer so the operator can tell.
    """
    body = result_text.strip()
    body_bytes = body.encode("utf-8")
    truncated = False
    if len(body_bytes) > max_body_bytes:
        # Decode up to the cap with `errors="ignore"` which drops any
        # partial multi-byte sequence at the boundary cleanly.
        body = body_bytes[:max_body_bytes].decode("utf-8", errors="ignore").rstrip()
        truncated = True

    duration = _compute_duration_s(job)
    # Fix-pack HIGH #2 (devil H-7): omit the `cost=` segment when the
    # value is NULL. Phase 6 never stores a cost (GAP #11 — deferred
    # to phase 9), so every notify was ending with `cost=$?`, a piece
    # of cruft that added noise without conveying information. Plan
    # §Q4's "cost=${cost}" format is preserved for the case where the
    # value is set (phase 9).
    segments = [
        f"job {job.id}",
        job.status,
        f"in {duration:.0f}s",
        f"kind={job.agent_type}",
    ]
    if job.cost_usd is not None:
        segments.append(f"cost=${job.cost_usd:.4f}")
    footer = f"\n\n---\n[{', '.join(segments)}]"
    if truncated:
        body += "\n\n[truncated]"
    return body + footer


def _compute_duration_s(job: SubagentJob) -> float:
    """Parse started_at / finished_at ISO strings; return seconds.

    Both stamps are expected at notify time. If either is missing (e.g.
    row recovered from an older schema or a logic bug), return 0.0
    rather than crashing the notify — the footer still carries useful
    info, and a 0s duration is visually obvious.
    """
    if not job.started_at or not job.finished_at:
        return 0.0
    try:
        start = _parse_iso(job.started_at)
        end = _parse_iso(job.finished_at)
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _parse_iso(s: str) -> datetime:
    """Parse `YYYY-MM-DDTHH:MM:SSZ` → tz-aware UTC datetime."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)
