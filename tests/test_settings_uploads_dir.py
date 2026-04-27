"""Phase 6a fix-pack — ``Settings.uploads_dir`` resolution tests.

Pins the Mac-dev fallback at ``<data_dir>/uploads`` (mirrors the
``vault_dir`` / ``memory_index_path`` convention) and the container
path at ``/app/.uploads``. The previous ``<project_root>/.uploads``
fallback created two ops hazards:

1. ``git clean -fd`` from inside the working tree wipes the entire
   ``.uploads/`` subtree — including ``.failed/`` quarantine forensics
   the owner may want to inspect;
2. the on-disk layout for ephemeral and persistent state diverged
   (vault under ``data_dir``, uploads under ``project_root``).

Anchoring uploads under ``data_dir`` is the spec convention and
restores layout consistency.
"""

from __future__ import annotations

from pathlib import Path

from assistant.config import ClaudeSettings, Settings


def _settings(*, project_root: Path, data_dir: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=data_dir,
        claude=ClaudeSettings(),
    )


def test_uploads_dir_mac_fallback_lives_under_data_dir(tmp_path: Path) -> None:
    """Mac dev (any non-``/app`` project_root): ``<data_dir>/uploads``."""
    project_root = tmp_path / "src_tree"
    project_root.mkdir()
    data_dir = tmp_path / "share" / "0xone"
    data_dir.mkdir(parents=True)

    s = _settings(project_root=project_root, data_dir=data_dir)
    assert s.uploads_dir == (data_dir / "uploads").resolve()


def test_uploads_dir_mac_fallback_does_not_live_in_project_root(tmp_path: Path) -> None:
    """Regression guard: uploads_dir must NOT resolve inside the
    project tree. A future ``git clean -fd`` from the working tree
    must not be able to wipe quarantine forensics.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    s = _settings(project_root=project_root, data_dir=data_dir)
    assert not s.uploads_dir.is_relative_to(project_root.resolve())


def test_uploads_dir_container_path_unchanged() -> None:
    """Container (``project_root == /app``): ``/app/.uploads`` literal.

    The file-tool hook constrains Read/Write/Edit to ``project_root``;
    keeping the tmp dir inside ``/app`` is what makes the hook surface
    single-arg.
    """
    s = _settings(project_root=Path("/app"), data_dir=Path("/var/data"))
    assert s.uploads_dir == Path("/app/.uploads")


def test_uploads_dir_mirrors_vault_dir_convention(tmp_path: Path) -> None:
    """``uploads_dir`` and ``vault_dir`` should both be derived from
    ``data_dir`` so the on-disk layout is uniform.
    """
    project_root = tmp_path / "repo"
    project_root.mkdir()
    data_dir = tmp_path / "share"
    data_dir.mkdir()

    s = _settings(project_root=project_root, data_dir=data_dir)
    # Both should sit directly under data_dir, sibling to each other.
    assert s.uploads_dir.parent == s.vault_dir.parent
    assert s.uploads_dir.parent == data_dir.resolve()
