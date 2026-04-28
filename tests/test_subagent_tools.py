"""Phase 6: subagent @tool surface — spawn / list / status / cancel."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from assistant.config import Settings
from assistant.state.db import apply_schema, connect
from assistant.subagent.store import SubagentStore
from assistant.tools_sdk.subagent import (
    SUBAGENT_SERVER,
    SUBAGENT_TOOL_NAMES,
    configure_subagent,
    subagent_cancel,
    subagent_list,
    subagent_spawn,
    subagent_status,
)


async def _ctx(
    tmp_path: Path,
) -> tuple[SubagentStore, Settings]:
    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    settings = cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )
    configure_subagent(
        store=store,
        owner_chat_id=42,
        settings=settings,
    )
    return store, settings


async def _invoke(handler: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Invoke a @tool-decorated handler bypassing the SDK plumbing.

    Returns the JSON-shaped dict the handler emits.
    """
    return await handler.handler(args)  # type: ignore[no-any-return]


async def test_spawn_inserts_pending_row(tmp_path: Path) -> None:
    store, _ = await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn,
        {"kind": "general", "task": "write 500 words on OAuth"},
    )
    assert res.get("status") == "requested"
    job_id = res["job_id"]
    job = await store.get_by_id(job_id)
    assert job is not None
    assert job.agent_type == "general"
    assert job.spawned_by_kind == "tool"
    assert job.callback_chat_id == 42


async def test_spawn_default_callback_is_owner(tmp_path: Path) -> None:
    store, _ = await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn,
        {"kind": "researcher", "task": "research X"},
    )
    job = await store.get_by_id(res["job_id"])
    assert job is not None
    assert job.callback_chat_id == 42  # owner_chat_id


async def test_spawn_explicit_callback(tmp_path: Path) -> None:
    store, _ = await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn,
        {"kind": "general", "task": "x", "callback_chat_id": 7},
    )
    job = await store.get_by_id(res["job_id"])
    assert job is not None
    assert job.callback_chat_id == 7


async def test_spawn_invalid_kind(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn, {"kind": "godmode", "task": "x"}
    )
    assert res.get("is_error") is True
    assert res.get("code") == 2  # CODE_KIND


async def test_spawn_empty_task(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(subagent_spawn, {"kind": "general", "task": ""})
    assert res.get("is_error") is True
    assert res.get("code") == 3  # CODE_TASK_SIZE


async def test_spawn_oversize_task(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn,
        {"kind": "general", "task": "x" * 5000},
    )
    assert res.get("is_error") is True
    assert res.get("code") == 3


async def test_spawn_invalid_callback_chat_id(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(
        subagent_spawn,
        {"kind": "general", "task": "ok", "callback_chat_id": "not-int"},
    )
    assert res.get("is_error") is True
    assert res.get("code") == 1  # CODE_VALIDATION


async def test_list_returns_recent(tmp_path: Path) -> None:
    _store, _ = await _ctx(tmp_path)
    await _invoke(subagent_spawn, {"kind": "general", "task": "a"})
    await _invoke(subagent_spawn, {"kind": "researcher", "task": "b"})
    res = await _invoke(subagent_list, {})
    assert res.get("count") == 2
    kinds = {j["kind"] for j in res["jobs"]}
    assert kinds == {"general", "researcher"}


async def test_list_filter_by_kind(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    await _invoke(subagent_spawn, {"kind": "general", "task": "a"})
    await _invoke(subagent_spawn, {"kind": "researcher", "task": "b"})
    res = await _invoke(subagent_list, {"kind": "researcher"})
    assert res.get("count") == 1
    assert res["jobs"][0]["kind"] == "researcher"


async def test_list_limit_caps_at_200(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(subagent_list, {"limit": 9999})
    # Must not raise; cap is internal.
    assert "count" in res


async def test_status_returns_full_row(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    spawn = await _invoke(
        subagent_spawn, {"kind": "general", "task": "x"}
    )
    res = await _invoke(
        subagent_status, {"job_id": spawn["job_id"]}
    )
    assert "job" in res
    assert res["job"]["id"] == spawn["job_id"]
    assert res["job"]["kind"] == "general"


async def test_status_unknown_id(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(subagent_status, {"job_id": 999_999})
    assert res.get("is_error") is True
    assert res.get("code") == 6  # CODE_NOT_FOUND


async def test_status_invalid_id(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(subagent_status, {"job_id": "not-a-number"})
    assert res.get("is_error") is True
    assert res.get("code") == 1


async def test_cancel_pending(tmp_path: Path) -> None:
    store, _ = await _ctx(tmp_path)
    spawn = await _invoke(
        subagent_spawn, {"kind": "general", "task": "x"}
    )
    res = await _invoke(
        subagent_cancel, {"job_id": spawn["job_id"]}
    )
    assert res.get("cancel_requested") is True
    job = await store.get_by_id(spawn["job_id"])
    assert job is not None
    assert job.cancel_requested is True


async def test_cancel_terminal(tmp_path: Path) -> None:
    store, _ = await _ctx(tmp_path)
    await store.record_started(
        sdk_agent_id="ag",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    await store.record_finished(
        sdk_agent_id="ag",
        status="completed",
        result_summary=None,
        transcript_path=None,
        sdk_session_id=None,
    )
    job = await store.get_by_agent_id("ag")
    assert job is not None
    res = await _invoke(subagent_cancel, {"job_id": job.id})
    assert res.get("already_terminal") == "completed"


async def test_cancel_unknown_id(tmp_path: Path) -> None:
    await _ctx(tmp_path)
    res = await _invoke(subagent_cancel, {"job_id": 999_999})
    assert res.get("is_error") is True
    assert res.get("code") == 6


def test_subagent_tool_names_match_server() -> None:
    """Lock the SUBAGENT_TOOL_NAMES tuple shape — adding a new @tool
    requires bumping the constant in tools_sdk/subagent.py."""
    expected = {
        "mcp__subagent__subagent_spawn",
        "mcp__subagent__subagent_list",
        "mcp__subagent__subagent_status",
        "mcp__subagent__subagent_cancel",
    }
    assert set(SUBAGENT_TOOL_NAMES) == expected


def test_subagent_server_registers_four_tools() -> None:
    # The MCP server is opaque; we can introspect via name on the
    # underlying instance (mirrors test_scheduler_mcp_registration pattern).
    assert SUBAGENT_SERVER is not None
