"""Phase 4 G1: WARN log when a permissive skill collapses the union."""

from __future__ import annotations

import pytest
import structlog
from structlog.testing import capture_logs

from assistant.bridge.claude import (
    _effective_allowed_tools,
    _reset_allowed_tools_warn_cache,
)


@pytest.fixture(autouse=True)
def _structlog_capture_ready() -> None:
    """Reset structlog + WARN cache so each test starts from zero."""
    structlog.reset_defaults()
    _reset_allowed_tools_warn_cache()


def test_none_allowed_tools_emits_warn_log() -> None:
    with capture_logs() as cap:
        result = _effective_allowed_tools(
            [
                {"name": "legacy-skill", "allowed_tools": None},
            ]
        )
    # Baseline because the None entry contributed everything.
    assert "Bash" in result
    matches = [e for e in cap if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    assert matches, f"expected the warn event; got {cap}"
    assert "legacy-skill" in matches[0].get("skills", [])


def test_narrowing_skills_do_not_emit_warn() -> None:
    with capture_logs() as cap:
        _effective_allowed_tools(
            [
                {"name": "ping", "allowed_tools": ["Bash"]},
                {"name": "memory", "allowed_tools": ["Bash", "Read"]},
            ]
        )
    assert not any(e["event"] == "allowed_tools_union_collapsed_to_baseline" for e in cap), cap


def test_unnamed_permissive_skill_tagged_unnamed() -> None:
    with capture_logs() as cap:
        _effective_allowed_tools([{"allowed_tools": None}])
    matches = [e for e in cap if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    assert matches
    assert "<unnamed>" in matches[0].get("skills", [])


def test_repeat_with_same_set_does_not_re_log() -> None:
    """Should-fix #7: steady-state permissive skill must not spam logs."""
    manifest = [{"name": "legacy", "allowed_tools": None}]
    with capture_logs() as cap1:
        _effective_allowed_tools(manifest)
    with capture_logs() as cap2:
        _effective_allowed_tools(manifest)
    first = [e for e in cap1 if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    second = [e for e in cap2 if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    assert len(first) == 1
    assert second == []


def test_changed_set_re_logs() -> None:
    """When the offending skill set changes, a fresh WARN fires."""
    with capture_logs() as cap1:
        _effective_allowed_tools([{"name": "legacy-a", "allowed_tools": None}])
    with capture_logs() as cap2:
        _effective_allowed_tools([{"name": "legacy-b", "allowed_tools": None}])
    first = [e for e in cap1 if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    second = [e for e in cap2 if e["event"] == "allowed_tools_union_collapsed_to_baseline"]
    assert len(first) == 1
    assert len(second) == 1
    assert "legacy-a" in first[0]["skills"]
    assert "legacy-b" in second[0]["skills"]
