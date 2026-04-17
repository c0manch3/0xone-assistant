"""Phase 4 Q8: per-skill `allowed-tools` intersection with global baseline."""

from __future__ import annotations

from assistant.bridge.claude import _GLOBAL_BASELINE, _effective_allowed_tools


def test_empty_manifest_falls_back_to_baseline() -> None:
    assert _effective_allowed_tools([]) == sorted(_GLOBAL_BASELINE)


def test_two_skills_narrow_to_union() -> None:
    manifest = [
        {"name": "ping", "allowed_tools": ["Bash"]},
        {"name": "memory", "allowed_tools": ["Bash", "Read"]},
    ]
    assert _effective_allowed_tools(manifest) == ["Bash", "Read"]


def test_none_allowed_tools_expands_to_baseline() -> None:
    manifest = [
        {"name": "legacy", "allowed_tools": None},
    ]
    assert _effective_allowed_tools(manifest) == sorted(_GLOBAL_BASELINE)


def test_empty_list_contributes_nothing() -> None:
    """One `[]` lockdown alongside a `[Bash]` skill → union is `[Bash]`."""
    manifest = [
        {"name": "lockdown", "allowed_tools": []},
        {"name": "active", "allowed_tools": ["Bash"]},
    ]
    assert _effective_allowed_tools(manifest) == ["Bash"]


def test_all_lockdown_returns_empty() -> None:
    manifest = [
        {"name": "a", "allowed_tools": []},
        {"name": "b", "allowed_tools": []},
    ]
    assert _effective_allowed_tools(manifest) == []


def test_out_of_baseline_tools_dropped() -> None:
    manifest = [
        {"name": "exotic", "allowed_tools": ["Bash", "Redis", "unknown-tool"]},
    ]
    # Only Bash is in the baseline.
    assert _effective_allowed_tools(manifest) == ["Bash"]


def test_realistic_3_skill_set() -> None:
    """After phase 4: ping=[Bash], skill-installer=[Bash], memory=[Bash, Read]."""
    manifest = [
        {"name": "ping", "allowed_tools": ["Bash"]},
        {"name": "skill-installer", "allowed_tools": ["Bash"]},
        {"name": "memory", "allowed_tools": ["Bash", "Read"]},
    ]
    assert _effective_allowed_tools(manifest) == ["Bash", "Read"]
