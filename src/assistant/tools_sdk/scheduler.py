"""Scheduler MCP server — 6 ``@tool`` functions backing the
single-owner recurring-prompt scheduler.

Each handler delegates to :mod:`assistant.tools_sdk._scheduler_core`
helpers after argument validation. Model-facing errors use the
``(code=N)`` suffix convention copied from memory / installer.

Wiring:
  - :data:`SCHEDULER_SERVER` — the :func:`create_sdk_mcp_server` record
    registered by :class:`ClaudeBridge`.
  - :data:`SCHEDULER_TOOL_NAMES` — tuple of fully-qualified
    ``mcp__scheduler__*`` names the model will see in ``allowed_tools``.

Configuration mirrors memory/installer: :func:`configure_scheduler`
populates a module-level ``_CTX`` dict exactly once at daemon boot.
The handlers read the shared :class:`SchedulerStore` reference from
that dict.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.config import SchedulerSettings
from assistant.scheduler.cron import CronParseError, parse_cron
from assistant.scheduler.store import SchedulerStore
from assistant.tools_sdk import _scheduler_core as core

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Error codes — mirror description-v2.md §1 "Error codes" table.
# ---------------------------------------------------------------------------
CODE_CRON_PARSE = 1
CODE_PROMPT_SIZE = 2
CODE_PROMPT_CTRL = 3
CODE_TZ = 4
CODE_CAP = 5
CODE_NOT_FOUND = 6
CODE_NOT_CONFIRMED = 8
CODE_IO = 9
CODE_PROMPT_REJECTED = 10
CODE_NOT_CONFIGURED = 11


# ---------------------------------------------------------------------------
# Module context
# ---------------------------------------------------------------------------
_CTX: dict[str, Any] = {}
_CONFIGURED: bool = False


def configure_scheduler(
    *,
    data_dir: Path,
    owner_chat_id: int,
    settings: SchedulerSettings,
    store: SchedulerStore,
) -> None:
    """Idempotent one-shot configuration.

    Re-calling with a different ``data_dir`` or ``owner_chat_id``
    raises :class:`RuntimeError` — the module-level context would fall
    out of sync with the daemon's actual state. Tests must call
    :func:`reset_scheduler_for_tests` between re-configs.
    """
    global _CONFIGURED
    if _CONFIGURED:
        cur = (_CTX.get("data_dir"), _CTX.get("owner_chat_id"))
        new = (data_dir, owner_chat_id)
        if cur != new:
            raise RuntimeError(
                "configure_scheduler re-called with different params: "
                f"data_dir={data_dir} (was {_CTX.get('data_dir')}), "
                f"owner_chat_id={owner_chat_id} "
                f"(was {_CTX.get('owner_chat_id')})"
            )
        return
    _CTX.update(
        data_dir=data_dir,
        owner_chat_id=owner_chat_id,
        settings=settings,
        store=store,
    )
    _CONFIGURED = True


def reset_scheduler_for_tests() -> None:
    """Test-only: drop module state so successive tests can reconfigure."""
    global _CONFIGURED
    _CTX.clear()
    _CONFIGURED = False


def _need_store() -> tuple[SchedulerStore, SchedulerSettings]:
    try:
        return _CTX["store"], _CTX["settings"]
    except KeyError as exc:
        raise RuntimeError(
            "scheduler not configured; call configure_scheduler() first"
        ) from exc


def _not_configured() -> dict[str, Any]:
    return core.tool_error("scheduler not configured", CODE_NOT_CONFIGURED)


# ---------------------------------------------------------------------------
# schedule_add
# ---------------------------------------------------------------------------
@tool(
    "schedule_add",
    "Schedule a recurring prompt; model provides 5-field cron expression. "
    "Supports *, lists, ranges, steps; Sunday=0 (or 7). Rejects @-aliases "
    "and Quartz extensions. Prompt is snapshot at add-time, not a template.",
    {
        "type": "object",
        "properties": {
            "cron": {
                "type": "string",
                "description": "5-field POSIX cron; supports * , - / (Sun=0).",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Up to 2048 UTF-8 bytes; snapshot at add-time. "
                    "Must NOT begin with '[system-note:' or contain "
                    "sentinel tags."
                ),
            },
            "tz": {
                "type": "string",
                "description": (
                    "IANA tz name (e.g. 'Europe/Moscow'); defaults to "
                    "SCHEDULER_TZ_DEFAULT."
                ),
            },
        },
        "required": ["cron", "prompt"],
    },
)
async def schedule_add(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, settings = _need_store()
    cron_raw = args.get("cron", "")
    prompt_raw = args.get("prompt", "")
    tz_raw = args.get("tz") or settings.tz_default
    try:
        expr = parse_cron(cron_raw)
    except CronParseError as exc:
        return core.tool_error(str(exc), CODE_CRON_PARSE)
    try:
        prompt_ok = core.validate_cron_prompt(prompt_raw)
    except ValueError as exc:
        msg = str(exc)
        if "exceeds" in msg:
            return core.tool_error(msg, CODE_PROMPT_SIZE)
        if "control characters" in msg:
            return core.tool_error(msg, CODE_PROMPT_CTRL)
        # Non-empty / system-note / sentinel rejects land here.
        return core.tool_error(msg, CODE_PROMPT_REJECTED)
    try:
        tz_obj = core.validate_tz(tz_raw)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_TZ)
    try:
        new_id = await store.add_schedule(
            cron=cron_raw,
            prompt=prompt_ok,
            tz=tz_raw,
            max_schedules=settings.max_schedules,
        )
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_CAP)
    # Q8: inline next_fire preview so the owner's reviewing LLM can
    # sanity-check the schedule at add time.
    next_fire = None
    try:
        del expr  # parsed purely to validate; preview re-parses once more
        next_fire = core.fetch_next_fire_preview(
            cron_raw, tz_obj, dt.datetime.now(dt.UTC)
        )
    except (ValueError, CronParseError) as exc:
        _log.warning(
            "schedule_add_preview_failed", id=new_id, error=str(exc)
        )
    nf_iso = next_fire.strftime("%Y-%m-%dT%H:%M:%SZ") if next_fire else None
    text = f"scheduled id={new_id} cron={cron_raw!r} tz={tz_raw}"
    if nf_iso:
        text += f"; next_fire={nf_iso}"
    return {
        "content": [{"type": "text", "text": text}],
        "id": new_id,
        "cron": cron_raw,
        "tz": tz_raw,
        "next_fire": nf_iso,
    }


# ---------------------------------------------------------------------------
# schedule_list
# ---------------------------------------------------------------------------
@tool(
    "schedule_list",
    "Return all scheduled prompts; optionally filter enabled only.",
    {
        "type": "object",
        "properties": {
            "enabled_only": {
                "type": "boolean",
                "default": False,
                "description": "If true, drop disabled schedules.",
            },
        },
        "required": [],
    },
)
async def schedule_list(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _settings = _need_store()
    enabled_only = bool(args.get("enabled_only", False))
    rows = await store.list_schedules(enabled_only=enabled_only)
    # Fix 6 / QA H1 / spec §B.2: wrap every prompt in an
    # ``<untrusted-scheduler-prompt-NONCE>…</…>`` envelope before
    # returning it to the model. Stored prompts are model-authored and
    # must be treated as replay, not live directives.
    wrapped_rows: list[dict[str, Any]] = []
    for r in rows:
        body = str(r["prompt"])
        wrapped_rows.append(
            {**r, "prompt": core.wrap_untrusted_prompt(body)}
        )
    lines = [f"{len(wrapped_rows)} schedule(s):"]
    for r in wrapped_rows:
        flag = "ON " if r["enabled"] else "off"
        lines.append(
            f"- id={r['id']} [{flag}] cron={r['cron']!r} tz={r['tz']} "
            f"last={r['last_fire_at'] or '-'}\n  prompt: {r['prompt']}"
        )
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "schedules": wrapped_rows,
        "count": len(wrapped_rows),
    }


# ---------------------------------------------------------------------------
# schedule_rm (soft-delete — alias of schedule_disable w/ confirmation)
# ---------------------------------------------------------------------------
@tool(
    "schedule_rm",
    "Soft-delete a schedule (enabled=0); history retained. In phase 5 "
    "rm is equivalent to disable but requires confirmed=true.",
    {"id": int, "confirmed": bool},
)
async def schedule_rm(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _settings = _need_store()
    try:
        sched_id = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return core.tool_error("id must be an integer", CODE_NOT_FOUND)
    if sched_id <= 0:
        return core.tool_error("id must be positive", CODE_NOT_FOUND)
    if not bool(args.get("confirmed", False)):
        return core.tool_error(
            "set confirmed=true to remove", CODE_NOT_CONFIRMED
        )
    if not await store.schedule_exists(sched_id):
        return core.tool_error("schedule not found", CODE_NOT_FOUND)
    changed = await store.disable_schedule(sched_id)
    text = (
        f"removed id={sched_id}"
        if changed
        else f"id={sched_id} already disabled"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "id": sched_id,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# schedule_enable
# ---------------------------------------------------------------------------
@tool(
    "schedule_enable",
    "Re-enable a previously disabled schedule.",
    {"id": int},
)
async def schedule_enable(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _settings = _need_store()
    try:
        sched_id = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return core.tool_error("id must be an integer", CODE_NOT_FOUND)
    if sched_id <= 0:
        return core.tool_error("id must be positive", CODE_NOT_FOUND)
    if not await store.schedule_exists(sched_id):
        return core.tool_error("schedule not found", CODE_NOT_FOUND)
    changed = await store.enable_schedule(sched_id)
    text = (
        f"enabled id={sched_id}"
        if changed
        else f"id={sched_id} already enabled"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "id": sched_id,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# schedule_disable
# ---------------------------------------------------------------------------
@tool(
    "schedule_disable",
    "Disable a schedule without deleting it. Idempotent.",
    {"id": int},
)
async def schedule_disable(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _settings = _need_store()
    try:
        sched_id = int(args.get("id") or 0)
    except (TypeError, ValueError):
        return core.tool_error("id must be an integer", CODE_NOT_FOUND)
    if sched_id <= 0:
        return core.tool_error("id must be positive", CODE_NOT_FOUND)
    if not await store.schedule_exists(sched_id):
        return core.tool_error("schedule not found", CODE_NOT_FOUND)
    changed = await store.disable_schedule(sched_id)
    text = (
        f"disabled id={sched_id}"
        if changed
        else f"id={sched_id} already disabled"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "id": sched_id,
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# schedule_history
# ---------------------------------------------------------------------------
@tool(
    "schedule_history",
    "Inspect recent trigger firings; newest first.",
    {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "integer",
                "description": "Filter by schedule id; omit for all.",
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
async def schedule_history(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    store, _settings = _need_store()
    sched_id_raw = args.get("schedule_id")
    sched_id: int | None = None
    if sched_id_raw is not None:
        try:
            sched_id = int(sched_id_raw)
        except (TypeError, ValueError):
            return core.tool_error("schedule_id must be int", CODE_NOT_FOUND)
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    rows = await store.get_schedule_history(
        schedule_id=sched_id, limit=limit
    )
    lines = [f"{len(rows)} recent trigger(s):"]
    for r in rows:
        lines.append(
            f"- id={r['id']} sched={r['schedule_id']} "
            f"scheduled_for={r['scheduled_for']} status={r['status']} "
            f"attempts={r['attempts']}"
        )
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "triggers": rows,
        "count": len(rows),
    }


# ---------------------------------------------------------------------------
# MCP server + canonical tool name tuple
# ---------------------------------------------------------------------------
SCHEDULER_SERVER = create_sdk_mcp_server(
    name="scheduler",
    version="0.1.0",
    tools=[
        schedule_add,
        schedule_list,
        schedule_rm,
        schedule_enable,
        schedule_disable,
        schedule_history,
    ],
)

SCHEDULER_TOOL_NAMES: tuple[str, ...] = (
    "mcp__scheduler__schedule_add",
    "mcp__scheduler__schedule_list",
    "mcp__scheduler__schedule_rm",
    "mcp__scheduler__schedule_enable",
    "mcp__scheduler__schedule_disable",
    "mcp__scheduler__schedule_history",
)
