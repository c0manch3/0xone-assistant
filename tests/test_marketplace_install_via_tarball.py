"""Must-fix #5: marketplace install fetches the whole repo as a single
tar.gz, not 84+ `gh api /contents/` calls.

We build a fake tar.gz in memory that mirrors the real anthropic/skills
shape (`<owner>-<repo>-<sha>/skills/<NAME>/...`), mock `subprocess.run`
to return it as `gh api /tarball/<ref>` stdout, and assert
`_fetch_via_tarball` extracts only the requested subtree.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path
from typing import Any

import _lib.fetch as fetch_mod
import pytest


def _build_fake_tarball(*, prefix: str, files: dict[str, bytes]) -> bytes:
    """Make a tar.gz whose top-level dir is `<prefix>/` + supplied files."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        root = tarfile.TarInfo(name=prefix)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        root.mtime = int(time.time())
        tf.addfile(root)
        for rel, data in files.items():
            info = tarfile.TarInfo(name=f"{prefix}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeRun:
    def __init__(self, stdout: bytes, *, rc: int = 0, stderr: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_via_tarball_extracts_only_requested_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "anthropics-skills-deadbeef"
    skill_md = b"---\nname: skill-creator\ndescription: make skills\n---\n"
    files = {
        "skills/skill-creator/SKILL.md": skill_md,
        "skills/skill-creator/scripts/init.py": b"pass\n",
        "skills/skill-creator/assets/logo.png": b"\x89PNG\r\n\x1a\n",
        "skills/pdf/SKILL.md": b"should-not-appear",
        "README.md": b"repo root file -- should not appear",
    }
    tarball = _build_fake_tarball(prefix=prefix, files=files)

    captured_cmd: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeRun:
        captured_cmd.append(cmd)
        return _FakeRun(tarball)

    monkeypatch.setattr(fetch_mod.subprocess, "run", _fake_run)

    dest = tmp_path / "bundle"
    dest.mkdir()
    fetch_mod._fetch_via_tarball("anthropics", "skills", "main", "skills/skill-creator", dest)

    # Only the skill-creator subtree survives.
    assert (dest / "SKILL.md").read_bytes() == skill_md
    assert (dest / "scripts" / "init.py").read_bytes() == b"pass\n"
    assert (dest / "assets" / "logo.png").exists()
    assert not (dest / "skills").exists()  # other skills pruned
    assert not (dest / "README.md").exists()  # repo-root files pruned

    assert len(captured_cmd) == 1
    assert captured_cmd[0] == [
        "gh",
        "api",
        "/repos/anthropics/skills/tarball/main",
    ]


def test_fetch_via_tarball_rejects_empty_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetch_mod.subprocess, "run", lambda *a, **kw: _FakeRun(b""))
    dest = tmp_path / "bundle"
    dest.mkdir()
    with pytest.raises(fetch_mod.FetchError, match="empty stdout"):
        fetch_mod._fetch_via_tarball("a", "b", "main", "skills/x", dest)


def test_fetch_via_tarball_rejects_gh_rc_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fetch_mod.subprocess,
        "run",
        lambda *a, **kw: _FakeRun(b"", rc=1, stderr=b"rate limit"),
    )
    dest = tmp_path / "bundle"
    dest.mkdir()
    with pytest.raises(fetch_mod.FetchError, match="tarball rc=1"):
        fetch_mod._fetch_via_tarball("a", "b", "main", "skills/x", dest)


def test_fetch_via_tarball_rejects_missing_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "anthropics-skills-deadbeef"
    tarball = _build_fake_tarball(
        prefix=prefix,
        files={"skills/pdf/SKILL.md": b"x"},  # no skill-creator
    )
    monkeypatch.setattr(fetch_mod.subprocess, "run", lambda *a, **kw: _FakeRun(tarball))
    dest = tmp_path / "bundle"
    dest.mkdir()
    with pytest.raises(fetch_mod.FetchError, match="not found in tarball"):
        fetch_mod._fetch_via_tarball("a", "b", "main", "skills/skill-creator", dest)


def test_marketplace_install_uses_exactly_one_gh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `fetch_bundle` on a tree URL goes through exactly one
    `gh api` invocation when `gh` is on PATH — proving we dropped the
    84-calls walker and got under the 60 req/h anonymous cap."""
    prefix = "anthropics-skills-deadbeef"
    tarball = _build_fake_tarball(
        prefix=prefix,
        files={
            "skills/skill-creator/SKILL.md": b"---\nname: skill-creator\ndescription: ok\n---\n",
            "skills/skill-creator/scripts/a.py": b"pass\n",
            "skills/skill-creator/scripts/b.py": b"pass\n",
        },
    )
    captured: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeRun:
        captured.append(cmd)
        return _FakeRun(tarball)

    monkeypatch.setattr(fetch_mod.shutil, "which", lambda name: "/fake/gh")
    monkeypatch.setattr(fetch_mod, "classify_url_sync", lambda url, **kw: None)
    monkeypatch.setattr(fetch_mod.subprocess, "run", _fake_run)

    fetch_mod.fetch_bundle(
        "https://github.com/anthropics/skills/tree/main/skills/skill-creator",
        tmp_path / "dest",
    )
    assert len(captured) == 1, f"expected exactly one gh call, got {len(captured)}: {captured}"
    assert (tmp_path / "dest" / "SKILL.md").exists()
    assert (tmp_path / "dest" / "scripts" / "a.py").exists()
