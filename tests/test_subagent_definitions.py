"""Phase 6 / commit 3 — AgentDefinition registry.

Covers:
  * Three kinds registered: general, worker, researcher.
  * Each `tools` list matches the spec (§3.5). Crucially, NONE contains
    `"Task"` — pitfall #2 / Q4 regression lock.
  * `model="inherit"` on every kind (Q10).
  * `background=True` on every kind (B-W2-1 forward-compat; SDK 0.1.59
    ignores but we keep the flag).
  * `maxTurns` per kind reflects the `SubagentSettings.max_turns_*`
    knob so operators can tune without a code change.
  * `prompt` substitutes `project_root` / `vault_dir` — a misformatted
    template would raise KeyError at `build_agents` time, which the
    tests lock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.subagent.definitions import build_agents


def _settings(tmp_path: Path, sub: SubagentSettings | None = None) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=sub or SubagentSettings(),
    )


def test_registry_has_three_kinds(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    assert set(agents.keys()) == {"general", "worker", "researcher"}


def test_no_kind_includes_task_in_tools(tmp_path: Path) -> None:
    """Pitfall #2 / Q4 regression lock: the depth cap is enforced by
    NOT advertising `Task` to any subagent. If a future refactor adds
    it, this test fails loudly."""
    agents = build_agents(_settings(tmp_path))
    for name, ad in agents.items():
        tools = ad.tools or []
        assert "Task" not in tools, f"{name!r} must not include Task in tools"


def test_general_tool_list_matches_spec(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    general = agents["general"]
    assert set(general.tools or []) == {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Grep",
        "Glob",
        "WebFetch",
    }


def test_worker_tool_list_matches_spec(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    worker = agents["worker"]
    assert set(worker.tools or []) == {"Bash", "Read"}


def test_researcher_tool_list_matches_spec(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    researcher = agents["researcher"]
    assert set(researcher.tools or []) == {"Read", "Grep", "Glob", "WebFetch"}


def test_all_kinds_inherit_model_and_run_in_background(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    for name, ad in agents.items():
        assert ad.model == "inherit", f"{name} model must be 'inherit' (Q10)"
        # B-W2-1 forward-compat: the flag stays on even though 0.1.59
        # ignores it.
        assert ad.background is True, f"{name} background must be True"


def test_max_turns_reflects_subagent_settings(tmp_path: Path) -> None:
    sub = SubagentSettings(
        max_turns_general=7,
        max_turns_worker=3,
        max_turns_researcher=11,
    )
    agents = build_agents(_settings(tmp_path, sub))
    assert agents["general"].maxTurns == 7
    assert agents["worker"].maxTurns == 3
    assert agents["researcher"].maxTurns == 11


def test_prompts_interpolate_project_root_and_vault_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agents = build_agents(settings)
    vault_str = str(settings.vault_dir)
    project_str = str(settings.project_root)
    for name, ad in agents.items():
        assert project_str in ad.prompt, f"{name} prompt missing project_root"
        assert vault_str in ad.prompt, f"{name} prompt missing vault_dir"


def test_build_agents_is_pure(tmp_path: Path) -> None:
    """Two calls return equivalent (but possibly distinct) dicts. No
    shared mutable state. The Daemon constructs the registry once;
    regressions here would cause subtle state leaks between bridges."""
    a = build_agents(_settings(tmp_path))
    b = build_agents(_settings(tmp_path))
    assert a.keys() == b.keys()
    for k in a:
        assert a[k].description == b[k].description
        assert a[k].tools == b[k].tools
        assert a[k].model == b[k].model
        assert a[k].maxTurns == b[k].maxTurns


def test_build_agents_accepts_permission_mode_default(tmp_path: Path) -> None:
    agents = build_agents(_settings(tmp_path))
    for ad in agents.values():
        assert ad.permissionMode == "default"


@pytest.mark.parametrize("kind", ["general", "worker", "researcher"])
def test_prompt_is_non_empty(tmp_path: Path, kind: str) -> None:
    agents = build_agents(_settings(tmp_path))
    assert len(agents[kind].prompt) > 50, f"{kind} prompt suspiciously short"
