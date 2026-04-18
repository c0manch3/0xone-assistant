"""TOCTOU detection: install re-fetches, compares SHA, exits 7 with diff
in stderr if the bundle changed since preview."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import tools.skill_installer._lib.fetch as fetch_mod
import tools.skill_installer.main as installer_main


def _materialise_bundle(content: str) -> Callable[[str, Path], None]:
    def _write(url: str, dest: Path) -> None:
        del url
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(
            f"---\nname: echo\ndescription: {content}\n---\n", encoding="utf-8"
        )

    return _write


def _materialise_bundle_v2_with_extra(content: str) -> Callable[[str, Path], None]:
    def _write(url: str, dest: Path) -> None:
        del url
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text(
            f"---\nname: echo\ndescription: {content}\n---\n", encoding="utf-8"
        )
        # Extra file → ADDED entry in the diff output.
        (dest / "NEW.md").write_text("new\n", encoding="utf-8")

    return _write


def _scripted_fetches(
    responses: list[Callable[[str, Path], None]],
) -> Callable[[str, Path], None]:
    it: Iterator[Callable[[str, Path], None]] = iter(responses)

    def _dispatch(url: str, dest: Path) -> None:
        next(it)(url, dest)

    return _dispatch


URL = "https://github.com/example/repo/tree/main/skills/echo"


def _setup_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fetches: list[Callable[..., Any]]
) -> Path:
    """Wire the installer to a tmp data_dir + project_root."""
    data_dir = tmp_path / "data"
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)
    (project / "tools").mkdir(parents=True)
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(installer_main, "_project_root", lambda: project)
    monkeypatch.setattr(fetch_mod, "fetch_bundle", _scripted_fetches(fetches))
    # main.py imported fetch_bundle at import time — patch the installed name too.
    monkeypatch.setattr(installer_main, "fetch_bundle", _scripted_fetches(fetches))
    return project


def test_toctou_mismatch_exits_7_with_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _setup_project(
        tmp_path,
        monkeypatch,
        [_materialise_bundle("v1"), _materialise_bundle_v2_with_extra("v2")],
    )
    rc = installer_main.main(["preview", URL])
    assert rc == installer_main.EXIT_OK, capsys.readouterr()

    rc = installer_main.main(["install", "--confirm", "--url", URL])
    captured = capsys.readouterr()
    assert rc == installer_main.EXIT_TOCTOU, captured
    assert "bundle on source changed" in captured.err
    # diff_trees output: at minimum ADDED: NEW.md + CHANGED: SKILL.md
    assert "ADDED: NEW.md" in captured.err or "CHANGED: SKILL.md" in captured.err
    # Install path must NOT have been taken.
    assert not (project / "skills" / "echo").exists()
    # Cache entry must be deleted so the operator is forced to preview again.
    cache_root = tmp_path / "data" / "run" / "installer-cache"
    # A fresh preview left manifest.json; install's finally rmtree's the dir.
    dirs_left = [p.name for p in cache_root.iterdir()] if cache_root.exists() else []
    # Cache may or may not be present depending on finally ordering; regardless
    # the manifest.json for this URL must not survive.
    for d in dirs_left:
        assert not (cache_root / d / "manifest.json").exists()


def test_install_without_preview_exits_no_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_project(tmp_path, monkeypatch, [])
    rc = installer_main.main(["install", "--confirm", "--url", URL])
    assert rc == installer_main.EXIT_NO_CACHE
    assert "no cached preview" in capsys.readouterr().err


def test_install_without_confirm_exits_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_project(tmp_path, monkeypatch, [])
    rc = installer_main.main(["install", "--url", URL])
    assert rc == installer_main.EXIT_USAGE
    assert "--confirm" in capsys.readouterr().err


def test_full_install_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # preview + install using the SAME bundle content both times.
    project = _setup_project(
        tmp_path,
        monkeypatch,
        [_materialise_bundle("same"), _materialise_bundle("same")],
    )
    rc = installer_main.main(["preview", URL])
    assert rc == 0
    rc = installer_main.main(["install", "--confirm", "--url", URL])
    out = capsys.readouterr()
    assert rc == installer_main.EXIT_OK, out
    assert (project / "skills" / "echo" / "SKILL.md").exists()
    # Sentinel written.
    assert (tmp_path / "data" / "run" / "skills.dirty").exists()
