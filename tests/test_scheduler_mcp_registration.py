"""SCHEDULER_SERVER + SCHEDULER_TOOL_NAMES invariants."""

from __future__ import annotations

from assistant.tools_sdk.scheduler import (
    SCHEDULER_SERVER,
    SCHEDULER_TOOL_NAMES,
)


def test_tool_names_canonical_prefix() -> None:
    for name in SCHEDULER_TOOL_NAMES:
        assert name.startswith("mcp__scheduler__"), (
            f"tool name {name!r} missing mcp__scheduler__ prefix"
        )


def test_six_tools_exposed() -> None:
    assert len(SCHEDULER_TOOL_NAMES) == 6
    expected = {
        "schedule_add",
        "schedule_list",
        "schedule_rm",
        "schedule_enable",
        "schedule_disable",
        "schedule_history",
    }
    suffixes = {n.split("__", 2)[-1] for n in SCHEDULER_TOOL_NAMES}
    assert suffixes == expected


def test_server_object_truthy() -> None:
    assert SCHEDULER_SERVER is not None
