"""Phase 4 memory MCP server registration parity with installer."""

from __future__ import annotations

from pathlib import Path


def test_memory_tool_names_has_six_entries() -> None:
    from assistant.tools_sdk.memory import MEMORY_TOOL_NAMES

    assert len(MEMORY_TOOL_NAMES) == 6
    assert len(set(MEMORY_TOOL_NAMES)) == 6
    for name in MEMORY_TOOL_NAMES:
        assert name.startswith("mcp__memory__")


def test_memory_tool_names_match_server() -> None:
    """Every canonical name has a matching @tool handler."""
    from assistant.tools_sdk import memory as mod
    from assistant.tools_sdk.memory import MEMORY_TOOL_NAMES

    decorated: set[str] = set()
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if hasattr(attr, "name") and hasattr(attr, "handler"):
            decorated.add(f"mcp__memory__{attr.name}")
    for n in MEMORY_TOOL_NAMES:
        assert n in decorated, f"{n!r} missing as @tool handler"


def test_memory_server_is_sdk_type() -> None:
    from assistant.tools_sdk.memory import MEMORY_SERVER

    assert MEMORY_SERVER["type"] == "sdk"
    assert MEMORY_SERVER["name"] == "memory"


def test_bridge_allows_all_memory_tools(tmp_path: Path) -> None:
    """Bridge options include memory allowed_tools + memory mcp_server."""
    from assistant.bridge.claude import ClaudeBridge
    from assistant.config import ClaudeSettings, Settings
    from assistant.tools_sdk.installer import configure_installer
    from assistant.tools_sdk.memory import (
        MEMORY_TOOL_NAMES,
        configure_memory,
    )

    pr = tmp_path / "project"
    pr.mkdir()
    configure_installer(project_root=pr, data_dir=pr)
    vault = tmp_path / "vault"
    idx = tmp_path / "memory-index.db"
    configure_memory(vault_dir=vault, index_db_path=idx)
    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=pr,
        data_dir=pr,
        claude=ClaudeSettings(),
    )
    bridge = ClaudeBridge(settings)
    opts = bridge._build_options(system_prompt="test")
    for name in MEMORY_TOOL_NAMES:
        assert name in opts.allowed_tools
    assert "memory" in (opts.mcp_servers or {})
    assert "installer" in (opts.mcp_servers or {})
