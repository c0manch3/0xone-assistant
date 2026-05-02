"""Phase 9 render_doc MCP server — single @tool for PDF/DOCX/XLSX
generation.

Mirrors phase-4 / 5b / 6 / 8 surface shape:

  - :data:`RENDER_DOC_SERVER` — :func:`create_sdk_mcp_server` record
    registered by :class:`~assistant.bridge.claude.ClaudeBridge` when
    ``render_doc_tool_visible=True``.
  - :data:`RENDER_DOC_TOOL_NAMES` — tuple of fully-qualified
    ``mcp__render_doc__*`` names exposed in ``allowed_tools``.

The ``render_doc`` handler delegates to
:meth:`~assistant.render_doc.subsystem.RenderDocSubsystem.render`.
That method enforces:

  - subsystem + per-format force-disable check (HIGH-5),
  - input size cap,
  - filename sanitisation (CRIT-5),
  - concurrency semaphore,
  - per-call timeout (``tool_timeout_s``),
  - audit log row,
  - in-flight ledger registration (CRIT-3 §2.13).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.logger import get_logger
from assistant.render_doc._validate_paths import (
    FilenameInvalid,
    _sanitize_filename,
)
from assistant.tools_sdk._render_doc_core import (
    configure_render_doc,
    get_configured_subsystem,
    reset_render_doc_for_tests,
)

__all__ = [
    "RENDER_DOC_SERVER",
    "RENDER_DOC_TOOL_NAMES",
    "configure_render_doc",
    "render_doc",
    "reset_render_doc_for_tests",
]

_log = get_logger("tools_sdk.render_doc")


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload as an MCP @tool result (text content + dict)."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload)}
        ],
        **payload,
    }


def _ok_envelope(
    *,
    fmt: str,
    path: str,
    suggested_filename: str,
    bytes_out: int,
    expires_at: str,
    tool_use_id: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "result": "rendered",
        "kind": "artefact",
        "schema_version": 1,
        "format": fmt,
        "path": path,
        "suggested_filename": suggested_filename,
        "bytes": bytes_out,
        "expires_at": expires_at,
        "tool_use_id": tool_use_id,
    }


def _fail_envelope(
    *,
    reason: str,
    error: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "kind": "error",
        "schema_version": 1,
        "reason": reason,
        "error": error,
    }


@tool(
    "render_doc",
    (
        "Render markdown content to a downloadable document file. "
        "Used for owner-facing reports, tables, summaries that benefit "
        "from formatted typography (PDF), Word-compatible editing "
        "(DOCX), or spreadsheet review (XLSX). Returns an artefact "
        "envelope; the bot delivers the file via Telegram automatically. "
        "DO NOT call this tool to log internal data — write to memory "
        "instead. Triggers: 'сделай PDF/DOCX/XLSX', 'сгенерь отчёт', "
        "'дай excel/word/pdf', 'render document'."
    ),
    {
        "type": "object",
        "properties": {
            "content_md": {
                "type": "string",
                "description": (
                    "Markdown source. For xlsx, must contain "
                    "exactly one pipe-syntax table."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "docx", "xlsx"],
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional suggested filename without extension. "
                    "Sanitized server-side; path components rejected."
                ),
            },
        },
        "required": ["content_md", "format"],
    },
)
async def render_doc(args: dict[str, Any]) -> dict[str, Any]:
    """Render markdown to PDF/DOCX/XLSX. See module docstring."""
    sub = get_configured_subsystem()
    if sub is None or sub.force_disabled:
        return _envelope(
            _fail_envelope(
                reason="disabled",
                error="subsystem-not-configured",
            )
        )

    content_md = args.get("content_md", "")
    fmt = args.get("format", "")
    raw_filename = args.get("filename")

    if fmt not in ("pdf", "docx", "xlsx"):
        # SDK enum should reject this BEFORE the body runs (MED-2).
        # Defensive branch: surface as render_failed_internal.
        return _envelope(
            _fail_envelope(
                reason="render_failed_internal",
                error="format-unknown",
            )
        )

    # CRIT-5: filename sanitisation.
    try:
        sanitized = _sanitize_filename(
            raw_filename if isinstance(raw_filename, str) else None
        )
    except FilenameInvalid as exc:
        return _envelope(
            _fail_envelope(
                reason="filename_invalid",
                error=f"sanitize-{exc.code}",
            )
        )

    if not isinstance(content_md, str):
        return _envelope(
            _fail_envelope(
                reason="render_failed_internal",
                error="content-md-not-str",
            )
        )

    # tool_use_id — the SDK doesn't surface the parent tool_use_id to
    # the @tool body, so we synthesise a deterministic-ish id from the
    # asyncio task name + UTC timestamp. Bridge keys the ledger by
    # this id when yielding the ArtefactBlock; collision is bounded by
    # SDK's tool_use uniqueness contract within a single conversation.
    task = asyncio.current_task()
    task_name = task.get_name() if task is not None else "no-task"
    tool_use_id = f"render-doc-{task_name}-{id(task)}"

    # Per-call timeout per spec §2.4.
    try:
        result = await asyncio.wait_for(
            sub.render(
                content_md,
                fmt,
                sanitized,
                task_handle=task,
            ),
            timeout=sub._settings.tool_timeout_s,
        )
    except TimeoutError:
        _log.warning(
            "render_doc_timeout",
            fmt=fmt,
            timeout_s=sub._settings.tool_timeout_s,
        )
        return _envelope(
            _fail_envelope(
                reason="timeout",
                error="tool-timeout-exceeded",
            )
        )

    if not result.ok:
        return _envelope(
            _fail_envelope(
                reason=result.reason or "render_failed_internal",
                error=result.error or "unknown",
            )
        )

    assert result.path is not None  # ok=True branch
    expires_at = (
        dt.datetime.now(dt.UTC).replace(microsecond=0)
        + dt.timedelta(seconds=sub._settings.artefact_ttl_s)
    ).isoformat()
    return _envelope(
        _ok_envelope(
            fmt=fmt,
            path=str(result.path),
            suggested_filename=result.suggested_filename,
            bytes_out=result.bytes_out,
            expires_at=expires_at,
            tool_use_id=tool_use_id,
        )
    )


# ---------------------------------------------------------------------------
# MCP server + canonical tool name tuple
# ---------------------------------------------------------------------------
RENDER_DOC_SERVER = create_sdk_mcp_server(
    name="render_doc",
    version="0.1.0",
    tools=[render_doc],
)

RENDER_DOC_TOOL_NAMES: tuple[str, ...] = (
    "mcp__render_doc__render_doc",
)
