"""Phase 4 G6: system-prompt template handles braces in user-authored content.

Also verifies that the rendered prompt carries the configured `vault_dir`
and that SKILL.md descriptions with literal `{x}` do not trigger
`KeyError` from `str.format`.
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(max_concurrent=1, timeout=30, max_turns=3, history_limit=20),
    )


def _write_template(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "src" / "assistant" / "bridge"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "system_prompt.md").write_text(
        "root={project_root}\nvault={vault_dir}\nskills:\n{skills_manifest}\n",
        encoding="utf-8",
    )
    (tmp_path / "skills").mkdir(exist_ok=True)


def test_render_escapes_braces_in_skill_description(tmp_path: Path) -> None:
    _write_template(tmp_path)
    skill = tmp_path / "skills" / "exotic"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        '---\nname: exotic\ndescription: "uses {foo} templating"\nallowed-tools: [Bash]\n---\n',
        encoding="utf-8",
    )
    bridge = ClaudeBridge(_make_settings(tmp_path))
    # Must NOT raise KeyError despite the `{foo}` in the description.
    out = bridge._render_system_prompt()
    assert "{foo}" in out


def test_render_carries_vault_dir(tmp_path: Path) -> None:
    _write_template(tmp_path)
    settings = _make_settings(tmp_path)
    bridge = ClaudeBridge(settings)
    out = bridge._render_system_prompt()
    assert f"vault={settings.vault_dir}" in out


def test_render_carries_project_root(tmp_path: Path) -> None:
    _write_template(tmp_path)
    bridge = ClaudeBridge(_make_settings(tmp_path))
    out = bridge._render_system_prompt()
    assert f"root={tmp_path}" in out
