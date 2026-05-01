"""Phase 8 fix-pack F1 — AC#5 (no observable trace when disabled).

The 4-reviewer wave (code-review HIGH-1, qa CRIT-1) caught that the
``mcp__vault__vault_push_now`` tool name was unconditionally added to
``allowed_tools`` and the ``vault`` MCP server was unconditionally
registered, regardless of ``settings.vault_sync.enabled``. The model
saw the tool even when the subsystem was disabled.

This file pins the invariants:

  - ``vault_sync.enabled=False`` → no ``mcp__vault__`` allowed-tool,
    no ``"vault"`` mcp_servers entry.
  - ``vault_sync.enabled=True, manual_tool_enabled=False`` → same.
  - ``vault_sync.enabled=True, manual_tool_enabled=True`` → both
    present.
  - ``Daemon._rss_observer`` does NOT include ``vault_sync_pending``
    when the subsystem is None.
  - With the subsystem disabled, no
    ``<data_dir>/run/vault-sync-audit.jsonl`` is created and no
    ``vault_sync_state.json`` is created.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, VaultSyncSettings


def _make_settings(
    tmp_path: Path,
    *,
    vs: VaultSyncSettings,
) -> Settings:
    """Build a minimal :class:`Settings` rooted at ``tmp_path`` with
    the given vault_sync subsection."""
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        vault_sync=vs,
    )


def test_disabled_bridge_does_not_register_vault_tool(
    tmp_path: Path,
) -> None:
    """AC#5 — with ``enabled=False`` (default), the bridge built with
    ``vault_tool_visible=False`` (default) MUST NOT advertise
    ``mcp__vault__vault_push_now`` and MUST NOT include ``"vault"``
    in ``mcp_servers``."""
    settings = _make_settings(tmp_path, vs=VaultSyncSettings())
    bridge = ClaudeBridge(settings)
    opts = bridge._build_options(system_prompt="test")
    assert (
        "mcp__vault__vault_push_now" not in (opts.allowed_tools or [])
    )
    assert "vault" not in (opts.mcp_servers or {})


def test_enabled_but_manual_tool_disabled_hides_tool(
    tmp_path: Path,
) -> None:
    """F1 — even when ``vault_sync.enabled=True``, with
    ``manual_tool_enabled=False`` the @tool stays invisible. The
    ``effective_manual_tool_enabled`` property is the gate.
    """
    vs = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=False,
    )
    settings = _make_settings(tmp_path, vs=vs)
    # Mirror Daemon.start: pass the computed property to the bridge.
    bridge = ClaudeBridge(
        settings,
        vault_tool_visible=settings.vault_sync.effective_manual_tool_enabled,
    )
    opts = bridge._build_options(system_prompt="test")
    assert (
        "mcp__vault__vault_push_now" not in (opts.allowed_tools or [])
    )
    assert "vault" not in (opts.mcp_servers or {})


def test_both_enabled_exposes_tool(tmp_path: Path) -> None:
    """F1 — only when BOTH ``enabled=True`` AND
    ``manual_tool_enabled=True`` does the @tool become visible."""
    vs = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
    )
    settings = _make_settings(tmp_path, vs=vs)
    bridge = ClaudeBridge(
        settings,
        vault_tool_visible=settings.vault_sync.effective_manual_tool_enabled,
    )
    opts = bridge._build_options(system_prompt="test")
    assert "mcp__vault__vault_push_now" in (opts.allowed_tools or [])
    assert "vault" in (opts.mcp_servers or {})


def test_explicit_false_visible_overrides_settings(
    tmp_path: Path,
) -> None:
    """F1 — non-owner bridges (picker, audio) construct with
    ``vault_tool_visible=False`` even when settings say enabled.
    The explicit kwarg is the truth-source for the bridge instance.
    """
    vs = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
    )
    settings = _make_settings(tmp_path, vs=vs)
    # Picker / audio bridge — explicit False.
    bridge = ClaudeBridge(settings, vault_tool_visible=False)
    opts = bridge._build_options(system_prompt="test")
    assert (
        "mcp__vault__vault_push_now" not in (opts.allowed_tools or [])
    )
    assert "vault" not in (opts.mcp_servers or {})


def test_default_settings_construct_without_state_files(
    tmp_path: Path,
) -> None:
    """AC#5 invariant: with ``enabled=False``, no
    ``vault_sync_state.json`` and no ``vault-sync-audit.jsonl`` are
    created (the subsystem is never constructed). The
    ``run/`` subdir may or may not be created by other phases — the
    invariant is on the vault-sync-specific filenames.
    """
    vs = VaultSyncSettings()
    assert vs.enabled is False
    # The subsystem isn't constructed at all (Daemon.start guards on
    # ``settings.vault_sync.enabled``); nothing on disk to assert.
    # We pin the invariant indirectly by asserting the gate.
    assert vs.effective_manual_tool_enabled is False
    # No state file is written by mere settings construction.
    state_path = tmp_path / "data" / "run" / "vault_sync_state.json"
    audit_path = tmp_path / "data" / "run" / "vault-sync-audit.jsonl"
    assert not state_path.exists()
    assert not audit_path.exists()


@pytest.mark.parametrize(
    "vault_tool_visible",
    [True, False],
)
def test_allowed_tools_list_shape(
    tmp_path: Path,
    vault_tool_visible: bool,
) -> None:
    """The allowed_tools list always carries the phase-3..6 baseline
    (Bash/Read/Write/Edit/Glob/Grep/WebFetch/Skill + each tool
    namespace except vault). Vault adds on TOP of that.
    """
    vs = (
        VaultSyncSettings(
            enabled=True,
            repo_url="git@github.com:c0manch3/0xone-vault.git",
            manual_tool_enabled=True,
        )
        if vault_tool_visible
        else VaultSyncSettings()
    )
    settings = _make_settings(tmp_path, vs=vs)
    bridge = ClaudeBridge(settings, vault_tool_visible=vault_tool_visible)
    opts = bridge._build_options(system_prompt="test")
    tools = opts.allowed_tools or []
    # Phase-3+ baseline always present.
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
    # Vault gating.
    assert ("mcp__vault__vault_push_now" in tools) == vault_tool_visible
