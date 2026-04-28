"""Telegram notify formatter for subagent results.

Pure-function module — no I/O, no globals. The hook in
:mod:`assistant.subagent.hooks` builds the body via
:func:`format_notification` and dispatches via the adapter.

Footer format (locked by description.md §criteria + research RQ4):
    [job N <status> in Xs, kind=K, cost=$Y]
When ``cost_usd IS NULL`` (phase-6 norm — accounting is phase-9),
the ``cost=`` segment is OMITTED so footers don't read ``cost=$?``.
"""

from __future__ import annotations

import datetime as _dt

from assistant.subagent.store import SubagentJob


def _parse_iso_z(raw: str | None) -> _dt.datetime | None:
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _compute_duration_s(job: SubagentJob) -> float:
    """Return seconds between ``started_at`` (or ``created_at``) and
    ``finished_at`` (or now if missing). Returns 0.0 on parse failure.

    Subagents whose Start hook never fired (orphan recovery, native-Task
    spawn race) have no ``started_at``; we fall back to ``created_at``
    so the footer still carries SOMETHING reasonable.
    """
    end = _parse_iso_z(job.finished_at) or _dt.datetime.now(_dt.UTC)
    begin = _parse_iso_z(job.started_at) or _parse_iso_z(job.created_at)
    if begin is None:
        return 0.0
    if end.tzinfo is None:
        end = end.replace(tzinfo=_dt.UTC)
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=_dt.UTC)
    return max(0.0, (end - begin).total_seconds())


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to ``max_bytes`` UTF-8 bytes on a codepoint
    boundary. Adds a ``[truncated]`` trailer when truncation occurred.

    Implementation note: encoding+decoding with ``errors='ignore'``
    cleanly drops a partial trailing codepoint without Cyrillic surprises.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return truncated + "\n\n[truncated]"


def format_notification(
    *,
    result_text: str,
    job: SubagentJob,
    max_body_bytes: int,
) -> str:
    """Render the final Telegram notify body.

    * Body is the subagent's last assistant text, trimmed and capped to
      ``max_body_bytes`` UTF-8 bytes.
    * Footer carries job id, terminal status, duration in seconds, kind,
      and (when populated) cost.
    """
    body = _truncate_utf8(result_text.strip(), max_body_bytes)
    duration = _compute_duration_s(job)
    segments = [
        f"job {job.id}",
        job.status,
        f"in {duration:.0f}s",
        f"kind={job.agent_type}",
    ]
    if job.cost_usd is not None:
        segments.append(f"cost=${job.cost_usd:.4f}")
    footer = "\n\n---\n[" + ", ".join(segments) + "]"
    return body + footer
