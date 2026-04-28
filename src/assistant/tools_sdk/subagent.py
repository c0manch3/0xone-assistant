"""Subagent MCP server — 4 ``@tool`` functions for delegating long
tasks to background subagents (phase 6).

Mirrors phase 4 memory / phase 5b scheduler shape:

- :data:`SUBAGENT_SERVER` — :func:`create_sdk_mcp_server` record
  registered by :class:`ClaudeBridge`.
- :data:`SUBAGENT_TOOL_NAMES` — tuple of fully-qualified
  ``mcp__subagent__*`` names exposed in ``allowed_tools``.

Configuration via :func:`configure_subagent` populates a module-level
``_CTX`` exactly once at daemon boot. Handlers read the shared
:class:`SubagentStore` reference from the dict.

Why no ``subagent_wait``? Research RQ1 dropped it — model can poll
with ``subagent_status`` plus the natural latency of the conversation;
no concrete user need justified the extra surface area.
"""

from __future__ import annotations

from typing import Any

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.config import Settings
from assistant.subagent.store import SubagentStore
from assistant.tools_sdk import _subagent_core as core

_log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module context (populated by configure_subagent)
# ---------------------------------------------------------------------------
_CTX: dict[str, Any] = {}
_CONFIGURED: bool = False


def configure_subagent(
    *,
    store: SubagentStore,
    owner_chat_id: int,
    settings: Settings,
) -> None:
    """Idempotent one-shot configuration.

    Re-calling with a different ``owner_chat_id`` raises
    :class:`RuntimeError` — the cached ``_CTX`` would fall out of sync
    with the daemon's actual state. Tests must call
    :func:`reset_subagent_for_tests` between configs.
    """
    global _CONFIGURED
    if _CONFIGURED:
        cur = _CTX.get("owner_chat_id")
        if cur != owner_chat_id:
            raise RuntimeError(
                "configure_subagent re-called with different owner_chat_id: "
                f"{owner_chat_id} (was {cur})"
            )
        _CTX["store"] = store
        _CTX["settings"] = settings
        return
    _CTX.update(
        store=store,
        owner_chat_id=owner_chat_id,
        settings=settings,
    )
    _CONFIGURED = True


def reset_subagent_for_tests() -> None:
    """Test-only: drop module state so successive tests can reconfigure."""
    global _CONFIGURED
    _CTX.clear()
    _CONFIGURED = False


def _need_ctx() -> tuple[SubagentStore, int]:
    try:
        return _CTX["store"], int(_CTX["owner_chat_id"])
    except KeyError as exc:
        raise RuntimeError(
            "subagent not configured; call configure_subagent() first"
        ) from exc


def _not_configured() -> dict[str, Any]:
    return core.tool_error(
        "subagent not configured", core.CODE_NOT_CONFIGURED
    )


# ---------------------------------------------------------------------------
# subagent_spawn
# ---------------------------------------------------------------------------
@tool(
    "subagent_spawn",
    "Queue a long-running task for asynchronous dispatch by a "
    "background subagent. Returns a job_id immediately; the result is "
    "delivered to the owner via Telegram when the subagent finishes. "
    "Use this for tasks > ~30s wallclock — long writing, deep research, "
    "bulk tool sequences. For short delegations, prefer the synchronous "
    "Task tool (it stays in the main turn).",
    {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["general", "worker", "researcher"],
                "description": (
                    "Agent registry key. 'general' = full tool access "
                    "(Bash/Read/Write/Edit/Grep/Glob/WebFetch). "
                    "'worker' = single CLI invocation flavour "
                    "(Bash, Read). 'researcher' = read-only "
                    "(Read, Grep, Glob, WebFetch)."
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "Task text passed verbatim as the subagent's user "
                    "prompt. Up to 4096 UTF-8 bytes. Be specific — the "
                    "subagent will not be able to ask clarifying "
                    "questions."
                ),
            },
            "callback_chat_id": {
                "type": "integer",
                "description": (
                    "Optional callback chat for the result delivery; "
                    "defaults to OWNER_CHAT_ID. Reserved for "
                    "phase-8 multi-chat."
                ),
            },
        },
        "required": ["kind", "task"],
    },
)
async def subagent_spawn(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, owner_chat_id = _need_ctx()
    try:
        kind = core.validate_kind(args.get("kind"))
    except ValueError as exc:
        return core.tool_error(str(exc), core.CODE_KIND)
    try:
        task_text = core.validate_task_text(args.get("task"))
    except ValueError as exc:
        return core.tool_error(str(exc), core.CODE_TASK_SIZE)
    callback_raw = args.get("callback_chat_id")
    if callback_raw is None:
        callback_chat_id = owner_chat_id
    else:
        try:
            callback_chat_id = int(callback_raw)
        except (TypeError, ValueError):
            return core.tool_error(
                "callback_chat_id must be integer", core.CODE_VALIDATION
            )
    # Fix-pack F2 (QA HIGH-1): scheduler-origin turns that delegate to
    # this @tool should be tagged ``spawned_by_kind="scheduler"`` —
    # not the static ``"tool"`` previously hard-coded. The handler sets
    # ``CURRENT_TURN_ORIGIN`` before driving the bridge; we read it
    # here. Default ``"telegram"`` → owner-typed turn → ``"tool"``.
    from assistant.subagent.hooks import CURRENT_TURN_ORIGIN

    origin = CURRENT_TURN_ORIGIN.get()
    spawned_by_kind = "scheduler" if origin == "scheduler" else "tool"
    job_id = await store.record_pending_request(
        agent_type=kind,
        task_text=task_text,
        callback_chat_id=callback_chat_id,
        spawned_by_kind=spawned_by_kind,
        spawned_by_ref=None,
    )
    text = (
        f"queued job_id={job_id} kind={kind}; the result will be "
        "delivered to the owner when the subagent finishes."
    )
    return {
        "content": [{"type": "text", "text": text}],
        "job_id": job_id,
        "status": "requested",
        "kind": kind,
    }


# ---------------------------------------------------------------------------
# subagent_list
# ---------------------------------------------------------------------------
@tool(
    "subagent_list",
    "List recent subagent jobs with optional status / kind filters.",
    {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Optional status filter: requested / started / "
                    "completed / failed / stopped / interrupted / "
                    "error / dropped."
                ),
            },
            "kind": {
                "type": "string",
                "description": (
                    "Optional kind filter: general / worker / researcher."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "default": 20,
            },
        },
        "required": [],
    },
)
async def subagent_list(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _owner = _need_ctx()
    raw_limit = args.get("limit", 20)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    status = args.get("status")
    kind = args.get("kind")
    if status is not None and not isinstance(status, str):
        return core.tool_error(
            "status must be string", core.CODE_VALIDATION
        )
    if kind is not None and not isinstance(kind, str):
        return core.tool_error(
            "kind must be string", core.CODE_VALIDATION
        )
    rows = await store.list_jobs(status=status, kind=kind, limit=limit)
    payload = [core.job_to_dict(j) for j in rows]
    lines = [f"{len(payload)} job(s):"]
    for j in payload:
        lines.append(
            f"- id={j['id']} kind={j['kind']} status={j['status']} "
            f"created={j['created_at']}"
        )
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "jobs": payload,
        "count": len(payload),
    }


# ---------------------------------------------------------------------------
# subagent_status
# ---------------------------------------------------------------------------
@tool(
    "subagent_status",
    "Get the full state of one subagent job by id.",
    {
        "type": "object",
        "properties": {
            "job_id": {"type": "integer"},
        },
        "required": ["job_id"],
    },
)
async def subagent_status(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _owner = _need_ctx()
    raw = args.get("job_id")
    try:
        job_id = int(raw or 0)
    except (TypeError, ValueError):
        return core.tool_error(
            "job_id must be integer", core.CODE_VALIDATION
        )
    if job_id <= 0:
        return core.tool_error(
            "job_id must be positive", core.CODE_VALIDATION
        )
    job = await store.get_by_id(job_id)
    if job is None:
        return core.tool_error("job not found", core.CODE_NOT_FOUND)
    payload = core.job_to_dict(job)
    text = (
        f"job {payload['id']} kind={payload['kind']} "
        f"status={payload['status']} created={payload['created_at']} "
        f"finished={payload['finished_at']}"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "job": payload,
    }


# ---------------------------------------------------------------------------
# subagent_cancel
# ---------------------------------------------------------------------------
@tool(
    "subagent_cancel",
    "Request cancellation of a non-terminal subagent job. The cancel "
    "flag is polled by the subagent's PreToolUse hook on each tool call "
    "— a tool-free subagent cannot be cancelled this way.",
    {
        "type": "object",
        "properties": {
            "job_id": {"type": "integer"},
        },
        "required": ["job_id"],
    },
)
async def subagent_cancel(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _owner = _need_ctx()
    raw = args.get("job_id")
    try:
        job_id = int(raw or 0)
    except (TypeError, ValueError):
        return core.tool_error(
            "job_id must be integer", core.CODE_VALIDATION
        )
    if job_id <= 0:
        return core.tool_error(
            "job_id must be positive", core.CODE_VALIDATION
        )
    result = await store.set_cancel_requested(job_id)
    if result.get("error") == "job not found":
        return core.tool_error("job not found", core.CODE_NOT_FOUND)
    if "already_terminal" in result:
        text = (
            f"job {job_id} already terminal "
            f"(status={result['already_terminal']}); cancel ignored."
        )
    else:
        text = (
            f"cancel requested for job {job_id} "
            f"(was {result['previous_status']})"
        )
    return {
        "content": [{"type": "text", "text": text}],
        **result,
    }


# ---------------------------------------------------------------------------
# subagent_wait — research RQ1: omitted from the surface (no concrete need).
# Phase 7+ may reintroduce as a discrete commit with its own tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MCP server + canonical tool name tuple
# ---------------------------------------------------------------------------
SUBAGENT_SERVER = create_sdk_mcp_server(
    name="subagent",
    version="0.1.0",
    tools=[
        subagent_spawn,
        subagent_list,
        subagent_status,
        subagent_cancel,
    ],
)

SUBAGENT_TOOL_NAMES: tuple[str, ...] = (
    "mcp__subagent__subagent_spawn",
    "mcp__subagent__subagent_list",
    "mcp__subagent__subagent_status",
    "mcp__subagent__subagent_cancel",
)
