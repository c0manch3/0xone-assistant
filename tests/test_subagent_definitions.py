"""Phase 6: AgentDefinition registry — kinds, tool restrictions, prompts."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from assistant.config import Settings
from assistant.subagent.definitions import SUBAGENT_KINDS, build_agents


def _settings(tmp_path: Path) -> Settings:
    """Build a minimal Settings for unit tests."""
    return cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )


def test_build_agents_three_kinds(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    assert set(agents.keys()) == {"general", "worker", "researcher"}


def test_subagent_kinds_constant_matches_registry(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    assert set(agents.keys()) == SUBAGENT_KINDS


def test_no_recursion_lock_task_omitted_from_tools(tmp_path: Path) -> None:
    """Pitfall #2 + S-6-0 Q4: depth cap is structural — none of the
    three kinds may include 'Task' in their tools list."""
    s = _settings(tmp_path)
    agents = build_agents(s)
    for kind, defn in agents.items():
        assert defn.tools is not None, f"{kind} missing tools"
        assert "Task" not in defn.tools, (
            f"{kind} would enable recursion — phase 6 caps at depth 1"
        )


def test_general_full_tool_access(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    tools = agents["general"].tools
    assert tools is not None
    assert set(tools) == {
        "Bash", "Read", "Write", "Edit", "Grep", "Glob", "WebFetch"
    }


def test_worker_minimal_tool_access(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    assert agents["worker"].tools == ["Bash", "Read"]


def test_researcher_read_only(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    tools = agents["researcher"].tools
    assert tools is not None
    assert set(tools) == {"Read", "Grep", "Glob", "WebFetch"}
    assert "Write" not in tools
    assert "Edit" not in tools
    assert "Bash" not in tools


def test_prompt_substitutes_project_root(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    general_prompt = agents["general"].prompt
    assert str(s.project_root) in general_prompt
    assert str(s.vault_dir) in general_prompt


def test_max_turns_uses_settings(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    agents = build_agents(s)
    assert agents["general"].maxTurns == s.subagent.max_turns_general
    assert agents["worker"].maxTurns == s.subagent.max_turns_worker
    assert agents["researcher"].maxTurns == s.subagent.max_turns_researcher


def test_background_flag_set_for_forward_compat(tmp_path: Path) -> None:
    """Pitfall #5: background=True is unobserved on 0.1.59-0.1.63 but
    we keep it set so a future SDK that honours the flag enables async
    behaviour automatically."""
    s = _settings(tmp_path)
    agents = build_agents(s)
    for defn in agents.values():
        assert defn.background is True


def test_model_inherit(tmp_path: Path) -> None:
    """S-6-0 Q10: 'inherit' is runtime-valid and the right default."""
    s = _settings(tmp_path)
    agents = build_agents(s)
    for defn in agents.values():
        assert defn.model == "inherit"
