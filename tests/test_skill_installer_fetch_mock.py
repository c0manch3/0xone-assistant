"""Unit tests around fetch.py dispatch table (URL-shape routing).

The git/gh/urllib side-effects are mocked; we only assert that fetch_bundle
picks the right branch for each URL shape and that the SSRF gate runs first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import _lib.fetch as fetch_mod


def test_git_repo_https_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def _stub(url: str, dest: Path) -> None:
        calls.append((url, dest))
        dest.mkdir()
        (dest / "SKILL.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_git_clone", _stub)
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)
    fetch_mod.fetch_bundle("https://github.com/anthropics/skills", tmp_path / "dest")
    assert calls == [("https://github.com/anthropics/skills", tmp_path / "dest")]


def test_git_repo_ssh_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def _stub(url: str, dest: Path) -> None:
        calls.append((url, dest))
        dest.mkdir()

    monkeypatch.setattr(fetch_mod, "_git_clone", _stub)
    # SSH URL doesn't pass through classify_url; no monkeypatch needed there.
    fetch_mod.fetch_bundle("git@github.com:anthropics/skills.git", tmp_path / "dest")
    assert len(calls) == 1


def test_github_tree_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Path]] = []

    def _stub(match: Any, dest: Path) -> None:
        calls.append((match, dest))
        dest.mkdir()
        (dest / "SKILL.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_fetch_github_tree", _stub)
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)
    fetch_mod.fetch_bundle(
        "https://github.com/anthropics/skills/tree/main/skills/pdf",
        tmp_path / "dest",
    )
    assert len(calls) == 1
    match = calls[0][0]
    assert match.group(1) == "anthropics"
    assert match.group(2) == "skills"
    assert match.group(3) == "main"
    assert match.group(4) == "skills/pdf"


def test_raw_skill_md_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Path]] = []

    def _stub(url: str, dest: Path) -> None:
        calls.append((url, dest))
        dest.mkdir()
        (dest / "SKILL.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_fetch_raw_skill_md", _stub)
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)
    fetch_mod.fetch_bundle(
        "https://raw.githubusercontent.com/x/y/main/foo/SKILL.md",
        tmp_path / "dest",
    )
    assert len(calls) == 1


def test_ssrf_gate_runs_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate_called = {"n": 0}

    def _gate(url: str, **_: Any) -> str | None:
        gate_called["n"] += 1
        return "private IP"

    monkeypatch.setattr(fetch_mod, "classify_url_sync", _gate)
    with pytest.raises(fetch_mod.FetchError, match="SSRF gate"):
        fetch_mod.fetch_bundle("https://github.com/x/y.git", tmp_path / "dest")
    assert gate_called["n"] == 1


def test_fetch_bundle_rejects_dotdot_in_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review fix #9: the regex allows `..` as a full segment; the explicit
    # `_reject_dotdot_segments` must catch it before dispatch.
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)
    with pytest.raises(fetch_mod.FetchError, match="not allowed"):
        fetch_mod.fetch_bundle(
            "https://github.com/anthropics/../secret",
            tmp_path / "dest",
        )
