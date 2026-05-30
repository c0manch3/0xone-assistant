"""Phase 10 — WebSearch built-in tool gating invariants.

Mirrors ``tests/test_phase8_disabled_invariants.py``. WebSearch is a
SERVER-SIDE, BILLED tool ($10/1000 searches), so it MUST be default-OFF
and only ever surfaced on the owner bridge when
``settings.websearch.enabled`` is True.

Pinned invariants:

  - Default ``Settings`` → ``websearch.enabled`` and
    ``websearch.subagent_enabled`` are both ``False``.
  - ``ClaudeBridge(settings)`` (default kwargs) → ``"WebSearch"`` NOT in
    ``allowed_tools``; no ``"websearch"`` mcp_servers entry EVER (it is a
    CLI built-in, not an MCP server).
  - ``ClaudeBridge(settings, websearch_tool_visible=True)`` → ``"WebSearch"``
    IN ``allowed_tools``.
  - Explicit ``websearch_tool_visible=False`` suppresses the tool even
    when ``settings.websearch.enabled=True`` (picker / audio invariant).
  - ``build_agents`` researcher tools include ``"WebSearch"`` iff
    ``settings.websearch.subagent_enabled`` is True; general / worker
    unchanged.
  - Baseline tools (Bash/Read/.../WebFetch/Skill) always present.
  - ``max_budget_usd`` only flows to ``ClaudeAgentOptions`` when websearch
    is visible AND a cap is configured.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, WebSearchSettings
from assistant.subagent.definitions import build_agents


def _make_settings(
    tmp_path: Path,
    *,
    ws: WebSearchSettings | None = None,
) -> Settings:
    """Build a minimal :class:`Settings` rooted at ``tmp_path`` with the
    given websearch subsection (defaults when omitted)."""
    kwargs: dict[str, object] = {
        "telegram_bot_token": "x" * 20,
        "owner_chat_id": 42,
        "project_root": tmp_path,
        "data_dir": tmp_path / "data",
    }
    if ws is not None:
        kwargs["websearch"] = ws
    return Settings(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Config defaults
# ----------------------------------------------------------------------
def test_default_websearch_disabled() -> None:
    """Default config → both gates OFF, no budget."""
    ws = WebSearchSettings()
    assert ws.enabled is False
    assert ws.subagent_enabled is False
    assert ws.max_budget_usd is None


def test_root_settings_expose_websearch_default_off(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    assert settings.websearch.enabled is False
    assert settings.websearch.subagent_enabled is False


def test_subagent_enabled_requires_enabled() -> None:
    """``subagent_enabled=True`` without ``enabled=True`` is a hard
    config error — the unattended background search path must never be a
    superset of the interactive one."""
    with pytest.raises(ValueError, match="subagent_enabled=True requires"):
        WebSearchSettings(subagent_enabled=True)


def test_negative_budget_rejected() -> None:
    with pytest.raises(ValueError, match="max_budget_usd must be > 0"):
        WebSearchSettings(enabled=True, max_budget_usd=0)


# ----------------------------------------------------------------------
# Bridge gating
# ----------------------------------------------------------------------
def test_default_bridge_omits_websearch(tmp_path: Path) -> None:
    """Default bridge (``websearch_tool_visible=False``) MUST NOT
    advertise ``"WebSearch"`` and MUST NOT add a ``"websearch"``
    mcp_servers entry."""
    settings = _make_settings(tmp_path)
    bridge = ClaudeBridge(settings)
    opts = bridge._build_options(system_prompt="test")
    assert "WebSearch" not in (opts.allowed_tools or [])
    assert "websearch" not in (opts.mcp_servers or {})


def test_visible_bridge_exposes_websearch(tmp_path: Path) -> None:
    """``websearch_tool_visible=True`` → ``"WebSearch"`` IN allowed_tools,
    still NO mcp_servers entry (built-in, not MCP)."""
    settings = _make_settings(tmp_path)
    bridge = ClaudeBridge(settings, websearch_tool_visible=True)
    opts = bridge._build_options(system_prompt="test")
    assert "WebSearch" in (opts.allowed_tools or [])
    assert "websearch" not in (opts.mcp_servers or {})


def test_explicit_false_overrides_enabled_settings(tmp_path: Path) -> None:
    """Picker / audio invariant: an explicit ``websearch_tool_visible=False``
    suppresses the tool even when ``settings.websearch.enabled=True``.
    This is exactly how the picker / audio bridges are constructed (they
    never pass the kwarg, so it defaults False)."""
    ws = WebSearchSettings(enabled=True)
    settings = _make_settings(tmp_path, ws=ws)
    # Owner-style wiring would pass enabled; picker passes nothing.
    bridge = ClaudeBridge(settings, websearch_tool_visible=False)
    opts = bridge._build_options(system_prompt="test")
    assert "WebSearch" not in (opts.allowed_tools or [])


def test_owner_wiring_mirror_enables_websearch(tmp_path: Path) -> None:
    """Mirror ``Daemon.start`` owner-bridge wiring: pass
    ``websearch_tool_visible=settings.websearch.enabled``."""
    ws = WebSearchSettings(enabled=True)
    settings = _make_settings(tmp_path, ws=ws)
    bridge = ClaudeBridge(
        settings,
        websearch_tool_visible=settings.websearch.enabled,
    )
    opts = bridge._build_options(system_prompt="test")
    assert "WebSearch" in (opts.allowed_tools or [])


@pytest.mark.parametrize("websearch_tool_visible", [True, False])
def test_baseline_tools_present_regardless_of_flag(
    tmp_path: Path,
    websearch_tool_visible: bool,
) -> None:
    """The phase-3..6 baseline tools are always present; WebSearch adds
    on TOP of them and never displaces one."""
    ws = WebSearchSettings(enabled=True) if websearch_tool_visible else None
    settings = _make_settings(tmp_path, ws=ws)
    bridge = ClaudeBridge(
        settings, websearch_tool_visible=websearch_tool_visible
    )
    opts = bridge._build_options(system_prompt="test")
    tools = opts.allowed_tools or []
    for baseline in (
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebFetch",
        "Skill",
    ):
        assert baseline in tools
    assert ("WebSearch" in tools) == websearch_tool_visible


# ----------------------------------------------------------------------
# max_budget_usd wiring
# ----------------------------------------------------------------------
def test_budget_applied_only_when_visible(tmp_path: Path) -> None:
    """``max_budget_usd`` flows to ClaudeAgentOptions only when websearch
    is visible AND a cap is configured."""
    ws = WebSearchSettings(enabled=True, max_budget_usd=0.75)
    settings = _make_settings(tmp_path, ws=ws)
    visible = ClaudeBridge(
        settings,
        websearch_tool_visible=True,
        websearch_max_budget_usd=settings.websearch.max_budget_usd,
    )
    assert (
        visible._build_options(system_prompt="t").max_budget_usd == 0.75
    )
    # Same budget but tool hidden → never clip non-search bridges.
    hidden = ClaudeBridge(
        settings,
        websearch_tool_visible=False,
        websearch_max_budget_usd=settings.websearch.max_budget_usd,
    )
    assert hidden._build_options(system_prompt="t").max_budget_usd is None


def test_budget_none_leaves_default(tmp_path: Path) -> None:
    """A websearch-enabled bridge with no configured cap leaves
    ``max_budget_usd`` at the SDK default (``None``) so long non-search
    turns are never clipped."""
    ws = WebSearchSettings(enabled=True)
    settings = _make_settings(tmp_path, ws=ws)
    bridge = ClaudeBridge(settings, websearch_tool_visible=True)
    opts = bridge._build_options(system_prompt="test")
    assert opts.max_budget_usd is None


# ----------------------------------------------------------------------
# Subagent researcher gating
# ----------------------------------------------------------------------
def test_researcher_omits_websearch_by_default(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    agents = build_agents(settings)
    assert "WebSearch" not in agents["researcher"].tools
    assert "WebFetch" in agents["researcher"].tools


def test_researcher_gains_websearch_when_subagent_enabled(
    tmp_path: Path,
) -> None:
    ws = WebSearchSettings(enabled=True, subagent_enabled=True)
    settings = _make_settings(tmp_path, ws=ws)
    agents = build_agents(settings)
    assert "WebSearch" in agents["researcher"].tools


def test_interactive_enabled_does_not_leak_to_researcher(
    tmp_path: Path,
) -> None:
    """Only ``subagent_enabled`` (not the interactive ``enabled``) grants
    the researcher search — resolves the picker/background leak."""
    ws = WebSearchSettings(enabled=True, subagent_enabled=False)
    settings = _make_settings(tmp_path, ws=ws)
    agents = build_agents(settings)
    assert "WebSearch" not in agents["researcher"].tools


# ----------------------------------------------------------------------
# Picker-bridge wiring (review must-fix #1): the researcher subagent runs
# inside the picker bridge's SDK session, so the budget cap must ride the
# picker bridge — gated by ``subagent_enabled``, not the interactive
# ``enabled``.
# ----------------------------------------------------------------------
def test_picker_wiring_carries_budget_when_subagent_enabled(
    tmp_path: Path,
) -> None:
    """Mirror ``Daemon.start`` picker-bridge wiring: pass
    ``websearch_tool_visible=settings.websearch.subagent_enabled`` and
    ``websearch_max_budget_usd=settings.websearch.max_budget_usd``. With
    ``subagent_enabled=True`` and a configured cap, the picker bridge
    advertises ``WebSearch`` AND attaches ``max_budget_usd`` — so the
    unattended ``maxTurns=15`` researcher path is USD-capped."""
    ws = WebSearchSettings(
        enabled=True, subagent_enabled=True, max_budget_usd=0.75
    )
    settings = _make_settings(tmp_path, ws=ws)
    picker = ClaudeBridge(
        settings,
        websearch_tool_visible=settings.websearch.subagent_enabled,
        websearch_max_budget_usd=settings.websearch.max_budget_usd,
    )
    opts = picker._build_options(system_prompt="test")
    assert "WebSearch" in (opts.allowed_tools or [])
    assert opts.max_budget_usd == 0.75


def test_picker_wiring_no_budget_when_subagent_disabled(
    tmp_path: Path,
) -> None:
    """Interactive search ON but background search OFF: the picker bridge
    must NOT advertise ``WebSearch`` and must NOT attach the cap — even
    though a cap is configured for the owner bridge. This is the gating
    invariant: WebSearch reaches the researcher ONLY via
    ``subagent_enabled``."""
    ws = WebSearchSettings(
        enabled=True, subagent_enabled=False, max_budget_usd=0.75
    )
    settings = _make_settings(tmp_path, ws=ws)
    picker = ClaudeBridge(
        settings,
        websearch_tool_visible=settings.websearch.subagent_enabled,
        websearch_max_budget_usd=settings.websearch.max_budget_usd,
    )
    opts = picker._build_options(system_prompt="test")
    assert "WebSearch" not in (opts.allowed_tools or [])
    assert opts.max_budget_usd is None


@pytest.mark.parametrize("subagent_enabled", [True, False])
def test_general_and_worker_unchanged_by_websearch(
    tmp_path: Path,
    subagent_enabled: bool,
) -> None:
    """``general`` and ``worker`` tool lists never gain WebSearch."""
    ws = (
        WebSearchSettings(enabled=True, subagent_enabled=True)
        if subagent_enabled
        else WebSearchSettings()
    )
    settings = _make_settings(tmp_path, ws=ws)
    agents = build_agents(settings)
    assert "WebSearch" not in agents["general"].tools
    assert "WebSearch" not in agents["worker"].tools
    assert agents["worker"].tools == ["Bash", "Read"]
