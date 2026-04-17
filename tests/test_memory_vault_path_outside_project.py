"""Phase 4 X4: vault paths outside project_root are rejected by phase-2 file hook.

This is the isolation invariant: the model CAN'T Read a vault note
directly via the SDK because `check_file_path` requires the path to be
inside `project_root`. The only way in is through `memory read` via Bash.
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.hooks import check_file_path
from assistant.config import ClaudeSettings, Settings


def test_vault_path_denied_by_file_guard(tmp_path: Path) -> None:
    # XDG-style layout: data_dir is NOT under project_root.
    project_root = tmp_path / "repo"
    project_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    settings = Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=project_root,
        data_dir=data_dir,
        claude=ClaudeSettings(),
    )

    vault_md = settings.vault_dir / "inbox" / "a.md"
    vault_md.parent.mkdir(parents=True)
    vault_md.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")

    reason = check_file_path(str(vault_md), project_root.resolve())
    assert reason is not None, "vault paths MUST be rejected by the file guard"
    assert "project_root" in reason or "escapes" in reason.lower()


def test_project_root_path_allowed(tmp_path: Path) -> None:
    """Sanity: the same guard still allows paths inside project_root."""
    project_root = tmp_path / "repo"
    (project_root / "src").mkdir(parents=True)
    inside = project_root / "src" / "main.py"
    inside.write_text("# ok", encoding="utf-8")
    assert check_file_path(str(inside), project_root.resolve()) is None
