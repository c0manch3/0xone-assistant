"""Phase 6 / commit 7 — ClaudeBridge + subagent integration.

Covers the three constructor additions:

  * `extra_hooks` merges into `_build_options`' `hooks` dict. PreToolUse
    list-unions (phase-3 cancel gate on top of subagent cancel gate);
    other events (SubagentStart/Stop) land under their own key.
  * `agents` in options when non-empty; `"Task"` added to the baseline
    via `baseline_extras` (B-W2-8). The subagent definitions themselves
    do NOT include `"Task"` — enforced by `test_subagent_definitions`.
  * Without `agents` the baseline behaviour is unchanged.

We exercise `_build_options` directly — the full `ask()` streaming path
needs the real SDK (gated by `RUN_SDK_INT`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from assistant.bridge.claude import ClaudeBridge, _effective_allowed_tools
from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.subagent.definitions import build_agents


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(),
    )


# ----------------------------------------------------------------- baseline extras


def test_allowed_tools_includes_task_when_baseline_extras_set() -> None:
    """B-W2-8: baseline_extras={Task} puts Task in the effective list."""
    out = _effective_allowed_tools([], baseline_extras=frozenset({"Task"}))
    assert "Task" in out


def test_allowed_tools_omits_task_without_extras() -> None:
    """Vanilla baseline has no Task — pitfall #13 lock."""
    out = _effective_allowed_tools([])
    assert "Task" not in out


def test_allowed_tools_extras_merge_with_permissive_skill() -> None:
    """A skill with `allowed_tools=None` (permissive default / missing
    frontmatter) inherits the WHOLE baseline. With `baseline_extras=
    {"Task"}` the Task tool flows in through that channel."""
    manifest = [{"name": "legacy", "allowed_tools": None}]
    out = _effective_allowed_tools(manifest, baseline_extras=frozenset({"Task"}))
    assert "Task" in out


def test_allowed_tools_extras_filtered_by_explicit_skill_list() -> None:
    """When every skill declares an explicit allowed_tools list without
    Task, the union excludes Task even with baseline_extras={Task}.

    Rationale: `baseline_extras` widens what the BASELINE permits; it
    does NOT bypass a skill's explicit whitelist. The Daemon ensures
    at least one skill (e.g. the `task` skill itself) includes Task in
    its allowed-tools so the main turn can delegate."""
    manifest = [{"name": "ping", "allowed_tools": ["Bash"]}]
    out = _effective_allowed_tools(manifest, baseline_extras=frozenset({"Task"}))
    assert "Task" not in out
    assert "Bash" in out


def test_allowed_tools_task_skill_contributes_task() -> None:
    """The `task` skill declares `allowed-tools: [Task, Bash]`, which
    intersects with the Task-extended baseline to let Task through."""
    manifest = [{"name": "task", "allowed_tools": ["Task", "Bash"]}]
    out = _effective_allowed_tools(manifest, baseline_extras=frozenset({"Task"}))
    assert "Task" in out
    assert "Bash" in out


# ----------------------------------------------------------------- bridge


def test_bridge_without_agents_options_omits_task(tmp_path: Path) -> None:
    bridge = ClaudeBridge(_settings(tmp_path))
    opts = bridge._build_options(system_prompt="sp")
    allowed = list(opts.allowed_tools or [])
    assert "Task" not in allowed


def test_bridge_with_agents_options_includes_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agents = build_agents(settings)
    bridge = ClaudeBridge(settings, agents=agents)
    opts = bridge._build_options(system_prompt="sp")
    allowed = list(opts.allowed_tools or [])
    assert "Task" in allowed


def test_bridge_with_agents_passes_agents_to_options(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    agents = build_agents(settings)
    bridge = ClaudeBridge(settings, agents=agents)
    opts = bridge._build_options(system_prompt="sp")
    # The `agents` kwarg flows through when set.
    opts_agents = getattr(opts, "agents", None)
    assert opts_agents is not None
    assert set(opts_agents.keys()) == {"general", "worker", "researcher"}


def test_bridge_without_agents_does_not_set_agents_on_options(tmp_path: Path) -> None:
    bridge = ClaudeBridge(_settings(tmp_path))
    opts = bridge._build_options(system_prompt="sp")
    # Not set, or set to None — either is acceptable; the spec says
    # we don't pass it when self._agents is falsy.
    opts_agents = getattr(opts, "agents", None)
    assert not opts_agents


# ----------------------------------------------------------------- extra_hooks


def test_extra_hooks_pretooluse_list_unions_with_phase3(tmp_path: Path) -> None:
    """A PreToolUse matcher in `extra_hooks` must be APPENDED to the
    phase-3 list, not replace it. Phase-3 bash/file/webfetch guards
    still fire first."""
    from claude_agent_sdk import HookMatcher

    async def _cancel_gate(input_data: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        del input_data, tool_use_id, ctx
        return {}

    extra = {"PreToolUse": [HookMatcher(hooks=[_cancel_gate])]}
    bridge = ClaudeBridge(_settings(tmp_path), extra_hooks=extra)
    opts = bridge._build_options(system_prompt="sp")
    hooks_dict = cast(dict[str, Any], opts.hooks or {})
    pre = list(hooks_dict.get("PreToolUse", []))
    # Phase-3: Bash + (Read/Write/Edit/Glob/Grep) + WebFetch = 7 base.
    # Plus the extra one.
    assert len(pre) >= 8, f"PreToolUse list suspiciously short: {pre}"


def test_extra_hooks_subagent_start_stop_added(tmp_path: Path) -> None:
    from claude_agent_sdk import HookMatcher

    async def _noop(input_data: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        del input_data, tool_use_id, ctx
        return {}

    extra = {
        "SubagentStart": [HookMatcher(hooks=[_noop])],
        "SubagentStop": [HookMatcher(hooks=[_noop])],
    }
    bridge = ClaudeBridge(_settings(tmp_path), extra_hooks=extra)
    opts = bridge._build_options(system_prompt="sp")
    hooks_dict = cast(dict[str, Any], opts.hooks or {})
    assert "SubagentStart" in hooks_dict
    assert "SubagentStop" in hooks_dict
    assert len(hooks_dict["SubagentStart"]) >= 1
    assert len(hooks_dict["SubagentStop"]) >= 1


def test_bridge_without_extra_hooks_keeps_phase3_intact(tmp_path: Path) -> None:
    bridge = ClaudeBridge(_settings(tmp_path))
    opts = bridge._build_options(system_prompt="sp")
    hooks_dict = cast(dict[str, Any], opts.hooks or {})
    assert "PreToolUse" in hooks_dict
    assert "PostToolUse" in hooks_dict
    # Neither SubagentStart nor SubagentStop should appear when no
    # extra_hooks are supplied.
    assert "SubagentStart" not in hooks_dict
    assert "SubagentStop" not in hooks_dict
