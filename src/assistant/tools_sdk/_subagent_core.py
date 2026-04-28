"""Subagent-tool shared helpers — TRUSTED in-process utilities.

NOT ``@tool``-decorated. The five ``@tool`` handlers in
:mod:`assistant.tools_sdk.subagent` delegate validation / shaping
through these helpers so the public surface stays thin and the trust
boundary is visible at the ``@tool`` layer.

Mirrors the phase-4 memory / phase-5 scheduler core pattern.
"""

from __future__ import annotations

from typing import Any

from assistant.subagent.definitions import SUBAGENT_KINDS
from assistant.subagent.store import SubagentJob

# ---------------------------------------------------------------------------
# Error codes (mirror scheduler / memory ``(code=N)`` convention)
# ---------------------------------------------------------------------------
CODE_VALIDATION = 1
CODE_KIND = 2
CODE_TASK_SIZE = 3
CODE_NOT_FOUND = 6
CODE_NOT_CONFIGURED = 11

_TASK_MAX_BYTES = 4096
_TASK_MIN_BYTES = 1


def tool_error(message: str, code: int) -> dict[str, Any]:
    """MCP-shaped error response. Mirrors :func:`_memory_core.tool_error`."""
    return {
        "content": [
            {"type": "text", "text": f"error: {message} (code={code})"}
        ],
        "is_error": True,
        "error": message,
        "code": code,
    }


def validate_kind(raw: Any) -> str:
    """Return the validated kind, raising :class:`ValueError` on reject."""
    if not isinstance(raw, str):
        raise ValueError("kind must be a string")
    if raw not in SUBAGENT_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(SUBAGENT_KINDS)}; got {raw!r}"
        )
    return raw


def validate_task_text(raw: Any) -> str:
    """Return trimmed task text; raise :class:`ValueError` on reject.

    Size cap is 4096 UTF-8 bytes, mirroring scheduler prompts. The
    smaller-than-scheduler default is intentional: subagent tasks are
    short DIRECTIVES, not multi-paragraph prompts.
    """
    if not isinstance(raw, str):
        raise ValueError("task must be a string")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("task must be non-empty")
    encoded = stripped.encode("utf-8")
    if len(encoded) < _TASK_MIN_BYTES:
        raise ValueError("task must be non-empty")
    if len(encoded) > _TASK_MAX_BYTES:
        raise ValueError(
            f"task exceeds {_TASK_MAX_BYTES} bytes (got {len(encoded)})"
        )
    return stripped


def job_to_dict(job: SubagentJob) -> dict[str, Any]:
    """Render a :class:`SubagentJob` as the JSON-friendly dict the @tool
    surface returns to the model.

    Includes only the fields the model needs for steering decisions
    (id, status, kind, task hint, timestamps, summary preview).
    Internal forensics fields like ``transcript_path`` are intentionally
    omitted to keep the model's context lean.
    """
    return {
        "id": job.id,
        "status": job.status,
        "kind": job.agent_type,
        "task_text": job.task_text,
        "result_summary": job.result_summary,
        "spawned_by_kind": job.spawned_by_kind,
        "callback_chat_id": job.callback_chat_id,
        "cancel_requested": job.cancel_requested,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }
