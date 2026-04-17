"""`ClaudeBridge._check_skills_sentinel` unlinks the sentinel and forces
the manifest cache to rebuild on the next turn."""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.claude import ClaudeBridge
from assistant.bridge.skills import build_manifest, invalidate_cache
from assistant.config import ClaudeSettings, Settings


def _write_skill(skills_dir: Path, name: str, description: str) -> Path:
    sk = skills_dir / name
    sk.mkdir(parents=True, exist_ok=True)
    md = sk / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {description}\nallowed-tools: [Bash]\n---\n",
        encoding="utf-8",
    )
    return md


def _settings(tmp_path: Path) -> Settings:
    # Point the system-prompt template at a minimal file so the bridge
    # renders without needing the real repo layout inside tmp_path.
    bridge_pkg = tmp_path / "src" / "assistant" / "bridge"
    bridge_pkg.mkdir(parents=True)
    (bridge_pkg / "system_prompt.md").write_text(
        "root={project_root}\nmanifest={skills_manifest}\n", encoding="utf-8"
    )
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


def test_sentinel_triggers_manifest_invalidate(tmp_path: Path) -> None:
    invalidate_cache()
    settings = _settings(tmp_path)
    bridge = ClaudeBridge(settings)
    skills = tmp_path / "skills"
    _write_skill(skills, "echo", "Echoes back.")
    # Prime the cache via a normal build_manifest call.
    first = build_manifest(skills)
    assert "echo" in first

    # Simulate PostToolUse by touching the sentinel + writing a second skill.
    # Without invalidation, the cached manifest would hide the new skill.
    (settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "run" / "skills.dirty").touch()
    _write_skill(skills, "weather", "Weather lookups.")

    # Calling _render_system_prompt must see + consume the sentinel.
    prompt = bridge._render_system_prompt()
    assert "echo" in prompt
    assert "weather" in prompt
    # Sentinel was consumed exactly once.
    assert not (settings.data_dir / "run" / "skills.dirty").exists()


def test_sentinel_absent_is_noop(tmp_path: Path) -> None:
    invalidate_cache()
    settings = _settings(tmp_path)
    bridge = ClaudeBridge(settings)
    (tmp_path / "skills").mkdir()
    prompt = bridge._render_system_prompt()
    assert prompt  # no exception, no sentinel touch needed
    assert not (settings.data_dir / "run" / "skills.dirty").exists()
