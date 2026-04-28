"""Phase 6: ClaudeBridge — extra_hooks + agents wiring in _build_options."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import HookMatcher

from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings
from assistant.subagent.definitions import build_agents
from assistant.tools_sdk.subagent import SUBAGENT_TOOL_NAMES


def _settings(tmp_path: Path) -> Settings:
    s = cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )
    # Ensure skills dir exists; bridge reads system prompt template +
    # build_manifest; we work around by using project_root unchanged.
    return s


def _make_template(project_root: Path) -> None:
    """Create a minimal system prompt template at the path the bridge
    reads from. The bridge calls _render_system_prompt which formats
    {project_root} and {skills_manifest}."""
    p = project_root / "src" / "assistant" / "bridge"
    p.mkdir(parents=True, exist_ok=True)
    (p / "system_prompt.md").write_text(
        "stub system prompt {project_root} {skills_manifest}",
        encoding="utf-8",
    )
    (project_root / "skills").mkdir(exist_ok=True)


async def test_build_options_without_agents_omits_task(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_template(tmp_path)
    bridge = ClaudeBridge(s)
    opts = bridge._build_options(system_prompt="stub")
    assert "Task" not in (opts.allowed_tools or [])
    # Subagent @tool surface always present.
    for n in SUBAGENT_TOOL_NAMES:
        assert n in (opts.allowed_tools or [])
    assert opts.agents is None


async def test_build_options_with_agents_includes_task(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_template(tmp_path)
    agents = build_agents(s)
    bridge = ClaudeBridge(s, agents=agents)
    opts = bridge._build_options(system_prompt="stub")
    assert "Task" in (opts.allowed_tools or [])
    assert opts.agents is not None
    assert set(opts.agents.keys()) == {"general", "worker", "researcher"}


async def test_build_options_extra_hooks_pretool_unioned(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_template(tmp_path)

    async def my_pre(input_data: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        return {}

    extra_hooks = {
        "PreToolUse": [HookMatcher(hooks=[my_pre])],
    }
    bridge = ClaudeBridge(s, extra_hooks=extra_hooks)
    opts = bridge._build_options(system_prompt="stub")
    pre = opts.hooks.get("PreToolUse") if opts.hooks else None  # type: ignore[union-attr]
    assert pre is not None
    # The base sandbox stack (Bash + 5 file tools + WebFetch) plus our
    # one cancel-gate hook → at least 8 matchers.
    assert len(pre) >= 8


async def test_build_options_extra_hooks_subagent_event(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_template(tmp_path)

    async def on_start(input_data: Any, tool_use_id: Any, ctx: Any) -> dict[str, Any]:
        return {}

    extra_hooks = {
        "SubagentStart": [HookMatcher(hooks=[on_start])],
    }
    bridge = ClaudeBridge(s, extra_hooks=extra_hooks)
    opts = bridge._build_options(system_prompt="stub")
    assert opts.hooks is not None
    sub_start = opts.hooks.get("SubagentStart")  # type: ignore[union-attr]
    assert sub_start is not None
    assert len(sub_start) == 1


async def test_build_options_subagent_server_in_mcp(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_template(tmp_path)
    bridge = ClaudeBridge(s)
    opts = bridge._build_options(system_prompt="stub")
    assert opts.mcp_servers is not None
    assert "subagent" in opts.mcp_servers  # type: ignore[operator]
