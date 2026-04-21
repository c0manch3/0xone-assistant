"""Verify the installer MCP server exposes exactly seven tools with the
canonical names that the bridge's allowed_tools list depends on.

Note: the SDK's init ``tools`` list is not asserted here; per RS-4 that
list picks up ambient CLI tools and should be tested via subset-assert
in the live-CLI variant (skipped in CI).
"""

from __future__ import annotations

from pathlib import Path


def test_installer_tool_names_has_seven_entries() -> None:
    """Static cross-check: exactly seven canonical names."""
    from assistant.tools_sdk.installer import INSTALLER_TOOL_NAMES

    assert len(INSTALLER_TOOL_NAMES) == 7
    assert len(set(INSTALLER_TOOL_NAMES)) == 7  # no duplicates
    for name in INSTALLER_TOOL_NAMES:
        assert name.startswith("mcp__installer__")


def test_installer_tool_names_match_decorated_handlers() -> None:
    """Every entry in INSTALLER_TOOL_NAMES must have a matching @tool
    function imported at module scope. If someone adds a new @tool but
    forgets to update INSTALLER_TOOL_NAMES, this test flags it.
    """
    from assistant.tools_sdk import installer as mod
    from assistant.tools_sdk.installer import INSTALLER_TOOL_NAMES

    decorated_names: set[str] = set()
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        # SdkMcpTool is a dataclass with `.name` attribute attached by @tool.
        if hasattr(attr, "name") and hasattr(attr, "handler"):
            decorated_names.add(f"mcp__installer__{attr.name}")

    # All canonical names must be present as decorated handlers.
    for n in INSTALLER_TOOL_NAMES:
        assert n in decorated_names, f"{n!r} not found as @tool handler"


def test_installer_server_is_sdk_type() -> None:
    """Sanity: the server is an SDK-type MCP server, not stdio/http."""
    from assistant.tools_sdk.installer import INSTALLER_SERVER

    assert INSTALLER_SERVER["type"] == "sdk"
    assert INSTALLER_SERVER["name"] == "installer"
    # instance must be a runtime mcp.server.lowlevel.server.Server.
    assert hasattr(INSTALLER_SERVER["instance"], "call_tool")
    assert hasattr(INSTALLER_SERVER["instance"], "list_tools")


def test_bridge_allows_all_installer_tools(tmp_path: Path) -> None:
    """The canonical seven ``mcp__installer__*`` names appear in
    ``ClaudeBridge._build_options().allowed_tools``.
    """
    from assistant.bridge.claude import ClaudeBridge
    from assistant.config import ClaudeSettings, Settings
    from assistant.tools_sdk.installer import (
        INSTALLER_TOOL_NAMES,
        configure_installer,
    )

    pr = tmp_path / "project"
    pr.mkdir()
    configure_installer(project_root=pr, data_dir=pr)

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=pr,
        data_dir=pr,
        claude=ClaudeSettings(),
    )
    bridge = ClaudeBridge(settings)
    opts = bridge._build_options(system_prompt="test")
    for name in INSTALLER_TOOL_NAMES:
        assert name in opts.allowed_tools, (
            f"installer tool {name!r} missing from allowed_tools {opts.allowed_tools}"
        )
    # The bridge must also advertise the installer MCP server.
    assert "installer" in (opts.mcp_servers or {})
