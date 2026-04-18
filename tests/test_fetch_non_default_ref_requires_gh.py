"""Must-fix #3: non-default ref + gh absent → FetchError, not silent clone-of-main.

Previously `_fetch_github_tree_fallback` would clone `main` even for
`tree/v2.0/...` URLs — a supply-chain downgrade where the preview-SHA
matched install-SHA (both clones of main). Now: gh absent + non-default
ref raises with a helpful message; gh absent + default ref still takes
the clone path for compatibility.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import tools.skill_installer._lib.fetch as fetch_mod


def _allow_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)


def test_non_default_ref_without_gh_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_everything(monkeypatch)
    monkeypatch.setattr(fetch_mod.shutil, "which", lambda name: None)

    url = "https://github.com/anthropics/skills/tree/v2.0/skills/foo"
    with pytest.raises(fetch_mod.FetchError, match="requires `gh`"):
        fetch_mod.fetch_bundle(url, tmp_path / "dest")
    assert not (tmp_path / "dest").exists()


def test_default_ref_without_gh_still_falls_back_to_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh absent + ref=main: legacy clone-and-subtree path still works."""
    _allow_everything(monkeypatch)
    monkeypatch.setattr(fetch_mod.shutil, "which", lambda name: None)

    clone_calls: list[tuple[str, Path]] = []

    def _fake_clone(url: str, dest: Path) -> None:
        clone_calls.append((url, dest))
        dest.mkdir(parents=True)
        (dest / "skills").mkdir()
        (dest / "skills" / "foo").mkdir()
        (dest / "skills" / "foo" / "SKILL.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_git_clone", _fake_clone)

    url = "https://github.com/anthropics/skills/tree/main/skills/foo"
    fetch_mod.fetch_bundle(url, tmp_path / "dest")
    # clone happened, subtree moved, SKILL.md present at dest.
    assert len(clone_calls) == 1
    assert (tmp_path / "dest" / "SKILL.md").exists()


def test_non_default_ref_with_gh_goes_through_tarball(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh available + ref=v2.0: takes the tarball path (via `_fetch_via_tarball`)."""
    _allow_everything(monkeypatch)
    monkeypatch.setattr(fetch_mod.shutil, "which", lambda name: "/fake/gh")

    tarball_calls: list[tuple[str, str, str, str, Path]] = []

    def _fake_tarball(owner: str, repo: str, ref: str, path: str, dest: Path) -> None:
        tarball_calls.append((owner, repo, ref, path, dest))
        (dest / "SKILL.md").write_text("a", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_fetch_via_tarball", _fake_tarball)

    url = "https://github.com/anthropics/skills/tree/v2.0/skills/foo"
    fetch_mod.fetch_bundle(url, tmp_path / "dest")
    assert len(tarball_calls) == 1
    _, _, ref, _, _ = tarball_calls[0]
    assert ref == "v2.0"


def test_dispatch_picks_tarball_when_gh_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _allow_everything(monkeypatch)
    monkeypatch.setattr(fetch_mod.shutil, "which", lambda name: "/fake/gh")

    tarball_hits: list[Any] = []

    def _fake_tarball(owner: str, repo: str, ref: str, path: str, dest: Path) -> None:
        tarball_hits.append((owner, repo, ref, path, dest))
        (dest / "SKILL.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(fetch_mod, "_fetch_via_tarball", _fake_tarball)
    fetch_mod.fetch_bundle(
        "https://github.com/anthropics/skills/tree/main/skills/pdf",
        tmp_path / "dest",
    )
    assert tarball_hits == [("anthropics", "skills", "main", "skills/pdf", tmp_path / "dest")]
