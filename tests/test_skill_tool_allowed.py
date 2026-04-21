"""Skill tool must be in allowed_tools (phase 2 smoke hotfix)."""

from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def test_skill_tool_in_allowed_list() -> None:
    """Regression: Skill tool must be discoverable by model, else skills are decorative."""
    # Build minimal settings suitable for ClaudeBridge init
    # (doesn't spawn subprocess — only builds options dict)
    settings = Settings(
        telegram_bot_token="dummy-token-ignored",
        owner_chat_id=1,
        claude=ClaudeSettings(),  # defaults OK
    )
    bridge = ClaudeBridge(settings)
    options = bridge._build_options(system_prompt="test")
    assert "Skill" in options.allowed_tools, (
        f"Skill tool missing from allowed_tools: {options.allowed_tools}. "
        "Without it, Claude cannot invoke skills even when manifest lists them."
    )
