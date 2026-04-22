"""Fix 1 / C4-W3 — PostToolUse memory audit hook caps every string value
in ``tool_input`` at 2048 chars before JSON-encoding the line.

Without this cap, a single ``memory_write`` with a 1 MiB body writes a
1 MiB line into ``memory-audit.log`` — one agent-gone-wild turn fills
dozens of MB in seconds. The cap is a defence against log explosion;
``body`` is the worst offender but ``path`` / ``query`` / etc. can
also carry model-controlled megabytes before downstream validators
reject them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.bridge.hooks import _truncate_strings, make_posttool_hooks


def test_truncate_strings_caps_long_str() -> None:
    out = _truncate_strings("a" * 10_000, max_len=100)
    assert isinstance(out, str)
    assert len(out) <= 100 + len("...<truncated>")
    assert out.endswith("...<truncated>")


def test_truncate_strings_preserves_short_str() -> None:
    out = _truncate_strings("short value")
    assert out == "short value"


def test_truncate_strings_walks_nested_dict_and_list() -> None:
    obj = {
        "body": "x" * 3000,
        "tags": ["ok", "y" * 3000],
        "nested": {"path": "z" * 4000, "n": 42},
    }
    out = cast(dict[str, Any], _truncate_strings(obj, max_len=2048))
    assert isinstance(out["body"], str)
    assert out["body"].endswith("...<truncated>")
    assert len(out["body"]) == 2048 + len("...<truncated>")
    assert out["tags"][0] == "ok"
    assert out["tags"][1].endswith("...<truncated>")
    assert out["nested"]["path"].endswith("...<truncated>")
    # Non-string leaves are untouched.
    assert out["nested"]["n"] == 42


@pytest.mark.asyncio
async def test_memory_audit_log_entry_body_truncated(tmp_path: Path) -> None:
    """Integration: invoke the memory PostToolUse hook with a 10 KiB
    body and assert the on-disk JSONL entry stores ~2 KiB, not 10 KiB.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    matchers = make_posttool_hooks(project_root, data_dir)
    memory_hook = None
    for matcher in matchers:
        if matcher.matcher == r"mcp__memory__.*":
            memory_hook = matcher.hooks[0]
            break
    assert memory_hook is not None, "memory PostToolUse hook not registered"

    huge_body = "Z" * 10_000
    input_data: dict[str, Any] = {
        "tool_name": "mcp__memory__memory_write",
        "tool_input": {
            "path": "inbox/x.md",
            "title": "T",
            "body": huge_body,
        },
        "tool_response": {"is_error": False, "content": []},
    }
    # The hook is declared ``async def``; the SDK wraps the call.
    await memory_hook(input_data, "tool_use_1", cast(Any, {}))  # type: ignore[arg-type]

    audit_path = data_dir / "memory-audit.log"
    assert audit_path.is_file()
    line = audit_path.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    stored_body = entry["tool_input"]["body"]
    # Truncated to ~2 KiB (+ suffix).
    assert len(stored_body) <= 2048 + len("...<truncated>")
    assert stored_body.endswith("...<truncated>")
    # Sanity: the original 10 KiB was not persisted verbatim.
    assert huge_body not in line
