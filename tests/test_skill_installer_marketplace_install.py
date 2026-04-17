"""End-to-end marketplace install: list -> tree URL -> preview -> install.

The fetch stage is mocked; marketplace constants are exercised verbatim.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import main as installer_main
import pytest

import _lib.fetch as fetch_mod
import _lib.marketplace as mkt


def _write_bundle(dest: Path) -> None:
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text(
        "---\nname: skill-creator\ndescription: Scaffold a new skill.\n---\n",
        encoding="utf-8",
    )
    (dest / "scripts" / "init_skill.py").parent.mkdir(parents=True)
    (dest / "scripts" / "init_skill.py").write_text(
        "if __name__ == '__main__':\n    pass\n", encoding="utf-8"
    )


def test_marketplace_install_builds_tree_url_and_invokes_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marketplace install skill-creator --confirm` resolves to the tree URL,
    runs preview + install, and drops the bundle into skills/skill-creator."""
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)
    (project / "tools").mkdir(parents=True)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(installer_main, "_project_root", lambda: project)

    # Same bundle for preview + install re-fetch.
    def _script() -> Callable[[str, Path], None]:
        responses: Iterator[int] = iter([0, 0])

        def _call(url: str, dest: Path) -> None:
            del url
            next(responses)
            _write_bundle(dest)

        return _call

    monkeypatch.setattr(fetch_mod, "fetch_bundle", _script())
    monkeypatch.setattr(installer_main, "fetch_bundle", _script())

    rc = installer_main.main(["marketplace", "install", "skill-creator", "--confirm"])
    assert rc == installer_main.EXIT_OK
    assert (project / "skills" / "skill-creator" / "SKILL.md").exists()
    assert (project / "skills" / "skill-creator" / "scripts" / "init_skill.py").exists()
    assert (data_dir / "run" / "skills.dirty").exists()


def test_marketplace_install_without_confirm_is_preview_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(installer_main, "_project_root", lambda: project)

    def _fetch(url: str, dest: Path) -> None:
        del url
        _write_bundle(dest)

    monkeypatch.setattr(fetch_mod, "fetch_bundle", _fetch)
    monkeypatch.setattr(installer_main, "fetch_bundle", _fetch)

    rc = installer_main.main(["marketplace", "install", "skill-creator"])
    assert rc == installer_main.EXIT_OK
    # preview-only: skill not installed yet, just cached.
    assert not (project / "skills" / "skill-creator").exists()
    out = capsys.readouterr().out
    assert "Preview of" in out
    assert "To install run" in out


def test_marketplace_constants_are_canonical() -> None:
    assert mkt.MARKETPLACE_URL == "https://github.com/anthropics/skills"
    assert mkt.MARKETPLACE_REPO == "anthropics/skills"
    assert mkt.MARKETPLACE_BASE_PATH == "skills"
    assert mkt.MARKETPLACE_REF == "main"
