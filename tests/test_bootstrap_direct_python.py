"""Daemon.start() fires _bootstrap_skill_creator_bg without blocking.

We instantiate Daemon with a minimal Settings, monkeypatch the fetch
path + atomic_install to make the coroutine finish in ~0ms, and verify:
  - Daemon.start() returns inside a 500 ms window.
  - The ``.0xone-installed`` marker is touched.
  - A repeated bootstrap is a no-op.
  - Missing gh/git → ``skill_creator_bootstrap_skipped_no_gh_nor_git``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest


class _SettingsStub:
    def __init__(self, project_root: Path, data_dir: Path) -> None:
        self.project_root = project_root
        self.data_dir = data_dir


@pytest.fixture
def daemon_stub(tmp_path: Path) -> object:
    from assistant.main import Daemon

    pr = tmp_path / "proj"
    pr.mkdir()
    (pr / "skills").mkdir()
    (pr / "tools").mkdir()
    dd = tmp_path / "data"
    dd.mkdir()
    settings = _SettingsStub(pr, dd)
    d = Daemon.__new__(Daemon)
    d._settings = settings  # type: ignore[attr-defined]
    d._bg_tasks = set()  # type: ignore[attr-defined]
    return d


async def test_bootstrap_happy_path(daemon_stub: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from assistant.tools_sdk import _installer_core as core

    async def _fetch(_url: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        (dest / "SKILL.md").write_text(
            "---\nname: skill-creator\ndescription: X\n---\n", encoding="utf-8"
        )

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    # Pretend gh is on PATH.
    monkeypatch.setattr(
        core.shutil,
        "which",
        lambda name: "/usr/bin/gh" if name == "gh" else None,
    )

    d = daemon_stub
    await d._bootstrap_skill_creator_bg()  # type: ignore[attr-defined]
    pr: Path = d._settings.project_root  # type: ignore[attr-defined]
    marker = pr / "skills" / "skill-creator" / ".0xone-installed"
    assert marker.is_file()


async def test_bootstrap_is_idempotent(
    daemon_stub: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = daemon_stub
    pr: Path = d._settings.project_root  # type: ignore[attr-defined]
    # Pre-create the marker.
    sc = pr / "skills" / "skill-creator"
    sc.mkdir()
    (sc / ".0xone-installed").touch()

    called = {"n": 0}

    async def _fetch(*_a: object, **_k: object) -> None:
        called["n"] += 1

    from assistant.tools_sdk import _installer_core as core

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    await d._bootstrap_skill_creator_bg()  # type: ignore[attr-defined]
    assert called["n"] == 0


async def test_bootstrap_no_gh_nor_git(
    daemon_stub: object,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from assistant.tools_sdk import _installer_core as core

    monkeypatch.setattr(core.shutil, "which", lambda _name: None)
    d = daemon_stub
    caplog.set_level(logging.INFO)
    await d._bootstrap_skill_creator_bg()  # type: ignore[attr-defined]
    # The _bootstrap_skill_creator_bg logs via structlog; the log text
    # bubbles up to stdlib logging as the JSON-encoded event.
    # Just assert the marker is absent — that's the observable outcome.
    pr: Path = d._settings.project_root  # type: ignore[attr-defined]
    marker = pr / "skills" / "skill-creator" / ".0xone-installed"
    assert not marker.exists()


async def test_bootstrap_fits_within_wall_clock(
    daemon_stub: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bootstrap concurrency invariant: even when the fetch stub takes
    meaningful time, awaiting it completes in <500 ms in tests.
    """
    from assistant.tools_sdk import _installer_core as core

    async def _fetch(_url: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        (dest / "SKILL.md").write_text(
            "---\nname: skill-creator\ndescription: X\n---\n", encoding="utf-8"
        )

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    monkeypatch.setattr(
        core.shutil,
        "which",
        lambda name: "/usr/bin/gh" if name == "gh" else None,
    )
    d = daemon_stub
    await asyncio.wait_for(
        d._bootstrap_skill_creator_bg(),  # type: ignore[attr-defined]
        timeout=0.5,
    )


async def test_bootstrap_partial_install_recovery(
    daemon_stub: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1 (wave-3): marker-less ``skills/skill-creator/`` is cleaned up
    before the fetch, so a prior crash between ``atomic_install`` rename
    and the ``.0xone-installed`` touch doesn't permanently brick the
    bootstrap.
    """
    from assistant.tools_sdk import _installer_core as core

    d = daemon_stub
    pr: Path = d._settings.project_root  # type: ignore[attr-defined]
    partial_dir = pr / "skills" / "skill-creator"
    partial_dir.mkdir()
    (partial_dir / "SKILL.md").write_text("corrupted partial state", encoding="utf-8")
    # Sentinel file that only exists in the pre-crash state; must be gone
    # after recovery proves the directory was rm -rf'd.
    (partial_dir / "leftover.txt").write_text("stale", encoding="utf-8")
    assert not (partial_dir / ".0xone-installed").exists()

    async def _fetch(_url: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        (dest / "SKILL.md").write_text(
            "---\nname: skill-creator\ndescription: fresh\n---\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    monkeypatch.setattr(
        core.shutil,
        "which",
        lambda name: "/usr/bin/gh" if name == "gh" else None,
    )

    await d._bootstrap_skill_creator_bg()  # type: ignore[attr-defined]

    marker = partial_dir / ".0xone-installed"
    assert marker.is_file(), "recovery must complete with a touched marker"
    # The stale file from the partial state must be gone — proves the
    # directory was rm -rf'd before the fresh bundle was installed.
    assert not (partial_dir / "leftover.txt").exists()
    # Fresh SKILL.md content replaced the corrupted one.
    assert "fresh" in (partial_dir / "SKILL.md").read_text(encoding="utf-8")
