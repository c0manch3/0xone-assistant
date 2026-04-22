"""Fix 2 / H2 — the FTS5 index DB and PostToolUse audit log are created
with owner-only permissions (``0o600``).

The index DB stores raw note bodies via FTS5 + the ``notes`` table; the
audit log stores ``tool_input`` dicts (paths, queries, truncated
bodies). Leaking either to a second local account exposes private
memory content. For a single-user macOS host the risk is low, but the
fix is cheap and matches the phase-4 security posture.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, cast

import pytest

from assistant.bridge.hooks import make_posttool_hooks
from assistant.tools_sdk import _memory_core as core


def _is_owner_only(path: Path) -> bool:
    """Return True iff ``path`` has no group/world permission bits set."""
    mode = path.stat().st_mode & 0o777
    # Reject any permission bit outside of the owner octal.
    return (mode & 0o077) == 0


def test_memory_index_db_chmod_0o600(tmp_path: Path) -> None:
    idx = tmp_path / "memory-index.db"
    core._ensure_index(idx)
    assert idx.is_file()
    assert _is_owner_only(idx), f"mode={stat.filemode(idx.stat().st_mode)}"


@pytest.mark.asyncio
async def test_memory_audit_log_chmod_0o600(tmp_path: Path) -> None:
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
    assert memory_hook is not None

    input_data: dict[str, Any] = {
        "tool_name": "mcp__memory__memory_list",
        "tool_input": {"area": "inbox"},
        "tool_response": {"is_error": False, "content": []},
    }
    await memory_hook(input_data, "tu1", cast(Any, {}))  # type: ignore[arg-type]

    audit_path = data_dir / "memory-audit.log"
    assert audit_path.is_file()
    assert _is_owner_only(audit_path), (
        f"mode={stat.filemode(audit_path.stat().st_mode)}"
    )
