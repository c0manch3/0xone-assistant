"""system_prompt must be preset-dict form so Claude Code preset defaults load.

Per Anthropic docs, a raw string ``system_prompt`` REPLACES the default preset.
Replacing the default drops the built-in directive that tells Claude to follow
the body of an auto-injected ``Skill`` invocation — the model then receives the
skill body but never follows it, so phase-2 end-to-end skill execution breaks.

Fix: wrap our rendered template in
``{"type": "preset", "preset": "claude_code", "append": ...}`` so the default
Claude Code preset stays in place and our project-specific instructions are
layered on top.
"""

from __future__ import annotations

from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def test_system_prompt_is_preset_dict() -> None:
    """Regression: ``ClaudeAgentOptions.system_prompt`` must be the preset dict."""
    settings = Settings(
        telegram_bot_token="dummy-token-ignored",
        owner_chat_id=1,
        claude=ClaudeSettings(),
    )
    bridge = ClaudeBridge(settings)
    options = bridge._build_options(system_prompt="project-template-goes-here")

    sp = options.system_prompt
    assert isinstance(sp, dict), (
        f"system_prompt must be a preset dict, got {type(sp).__name__}. "
        "A raw string replaces the claude_code preset and breaks skill auto-follow."
    )
    assert sp.get("type") == "preset", f"expected type='preset', got {sp.get('type')!r}"
    assert sp.get("preset") == "claude_code", (
        f"expected preset='claude_code', got {sp.get('preset')!r}"
    )
    assert sp.get("append") == "project-template-goes-here", (
        "the rendered project template must be passed through as the `append` field "
        "so it layers on top of the default preset, rather than replacing it"
    )


def test_system_prompt_excludes_dynamic_sections() -> None:
    """Regression: dynamic sections (cwd/auto-memory/git) must be stripped.

    Including them makes the system prompt vary per-session, busting prompt
    caching and occasionally surfacing directives that conflict with our
    skill-following rule. With ``exclude_dynamic_sections=True`` the stripped
    content is re-injected into the first user message so the model still
    sees it, but the preset prefix stays stable and cacheable.
    """
    settings = Settings(
        telegram_bot_token="dummy-token-ignored",
        owner_chat_id=1,
        claude=ClaudeSettings(),
    )
    bridge = ClaudeBridge(settings)
    options = bridge._build_options(system_prompt="project-template-goes-here")

    sp = options.system_prompt
    assert isinstance(sp, dict)
    assert sp.get("exclude_dynamic_sections") is True, (
        "system_prompt preset must set exclude_dynamic_sections=True so the "
        "prefix stays cache-stable and dynamic claude_code sections don't "
        "override the skill-following directive in our appended template"
    )
