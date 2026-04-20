from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from assistant.bridge.bootstrap import (
    assert_no_custom_claude_settings,
    ensure_skills_symlink,
)


def _write_settings(project_root: Path, name: str, payload: dict[str, object]) -> None:
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_hooks_key_blocks_startup(tmp_path: Path) -> None:
    """SW3: {"hooks": {}} in settings.json → sys.exit(3)."""
    _write_settings(tmp_path, "settings.json", {"hooks": {"PreToolUse": []}})
    logger = logging.getLogger("test_s15")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_permissions_deny_blocks_startup(tmp_path: Path) -> None:
    """SW3: ``permissions.deny`` alters SDK semantics → block."""
    _write_settings(
        tmp_path,
        "settings.json",
        {"permissions": {"deny": ["Bash(rm:*)"]}},
    )
    logger = logging.getLogger("test_sw3_deny")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_permissions_default_mode_blocks_startup(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "settings.json",
        {"permissions": {"defaultMode": "acceptEdits"}},
    )
    logger = logging.getLogger("test_sw3_dmode")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_permissions_additional_dirs_blocks_startup(tmp_path: Path) -> None:
    _write_settings(
        tmp_path,
        "settings.json",
        {"permissions": {"additionalDirectories": ["/tmp"]}},
    )
    logger = logging.getLogger("test_sw3_addir")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_permissions_allow_is_benign(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SW3: plain ``permissions.allow`` — user-level CLI grants — are
    benign and MUST NOT exit(3). The owner's dev machine ships with
    these out-of-the-box; previous policy was failing first ``just run``."""
    _write_settings(
        tmp_path,
        "settings.json",
        {"permissions": {"allow": ["Bash(ls:*)", "Bash(git status:*)"]}},
    )
    logger = logging.getLogger("test_sw3_allow")
    with caplog.at_level(logging.WARNING, logger="test_sw3_allow"):
        assert_no_custom_claude_settings(tmp_path, logger)
    assert any("user-level" in r.message for r in caplog.records)


def test_permissions_ask_is_benign(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_settings(
        tmp_path,
        "settings.json",
        {"permissions": {"ask": ["Bash(rm:*)"]}},
    )
    logger = logging.getLogger("test_sw3_ask")
    with caplog.at_level(logging.WARNING, logger="test_sw3_ask"):
        assert_no_custom_claude_settings(tmp_path, logger)
    # Should not exit; a warning line is emitted.
    assert any("user-level" in r.message for r in caplog.records)


def test_permissions_mixed_blocks_on_deny_even_with_allow(tmp_path: Path) -> None:
    """If a file has BOTH benign ``allow`` AND blocking ``deny``, the
    blocking wins — we still exit(3)."""
    _write_settings(
        tmp_path,
        "settings.json",
        {
            "permissions": {
                "allow": ["Bash(ls:*)"],
                "deny": ["Bash(rm:*)"],
            }
        },
    )
    logger = logging.getLogger("test_sw3_mixed")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_cosmetic_keys_pass_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Plain settings (statusLine only) → startup succeeds; owner sees a
    warning with redacted content in the logs."""
    _write_settings(
        tmp_path,
        "settings.json",
        {"statusLine": "my-prompt $", "defaultModel": "claude-opus-4-6"},
    )
    logger = logging.getLogger("test_s15_cosmetic")
    with caplog.at_level(logging.WARNING, logger="test_s15_cosmetic"):
        assert_no_custom_claude_settings(tmp_path, logger)
    assert any("present — allowed" in r.message for r in caplog.records)


def test_empty_settings_is_fine(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An empty JSON object carries no blocking keys and no user grants
    — startup continues. The warning we emit is the generic 'present —
    allowed' form; no user-level grants line."""
    _write_settings(tmp_path, "settings.json", {})
    logger = logging.getLogger("test_sw3_empty")
    with caplog.at_level(logging.WARNING, logger="test_sw3_empty"):
        assert_no_custom_claude_settings(tmp_path, logger)
    assert not any("user-level" in r.message for r in caplog.records)


def test_no_settings_file_is_fine(tmp_path: Path) -> None:
    """No .claude/settings*.json present → quiet pass."""
    logger = logging.getLogger("test_s15_empty_fs")
    assert_no_custom_claude_settings(tmp_path, logger)


def test_settings_local_also_checked(tmp_path: Path) -> None:
    _write_settings(tmp_path, "settings.local.json", {"hooks": {}})
    logger = logging.getLogger("test_s15_local")
    with pytest.raises(SystemExit) as exc:
        assert_no_custom_claude_settings(tmp_path, logger)
    assert exc.value.code == 3


def test_symlink_absolute_target(tmp_path: Path) -> None:
    """S4: symlink target is absolute (resolves to project_root/skills)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    ensure_skills_symlink(tmp_path)
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink()
    # readlink returns the stored target — must be absolute.
    stored = link.readlink()
    assert stored.is_absolute()
    assert stored == skills.resolve()


def test_symlink_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    ensure_skills_symlink(tmp_path)
    ensure_skills_symlink(tmp_path)  # no-op on second call
    link = tmp_path / ".claude" / "skills"
    assert link.is_symlink()


def test_symlink_replaces_stale_relative_target(tmp_path: Path) -> None:
    """If a prior rebuild left a relative `../skills` link, ensure_skills_symlink
    replaces it with an absolute target."""
    (tmp_path / "skills").mkdir()
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "skills").symlink_to("../skills", target_is_directory=True)

    ensure_skills_symlink(tmp_path)
    stored = (claude_dir / "skills").readlink()
    assert stored.is_absolute()
