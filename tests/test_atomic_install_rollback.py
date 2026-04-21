"""atomic_install — B3 rollback on rename failure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _write_bundle(dest: Path, with_tools: bool = True) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        "---\nname: rho\ndescription: X\n---\n",
        encoding="utf-8",
    )
    if with_tools:
        (dest / "tools").mkdir()
        (dest / "tools" / "main.py").write_text("print()\n", encoding="utf-8")


def test_atomic_install_marker_touched(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    report: dict[str, Any] = {"name": "rho"}

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    (pr / "tools").mkdir(parents=True)
    core.atomic_install(bundle, report, project_root=pr)

    assert (pr / "skills" / "rho" / ".0xone-installed").is_file()
    assert (pr / "tools" / "rho" / "main.py").is_file()
    # inner tools/ no longer present under skills/rho
    assert not (pr / "skills" / "rho" / "tools").exists()


def test_atomic_install_rollback_on_tools_collision(tmp_path: Path) -> None:
    """If tools/<name> already exists, atomic_install aborts without
    touching skills/<name> (pre-check)."""
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    report: dict[str, Any] = {"name": "sigma"}

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    (pr / "tools" / "sigma").mkdir(parents=True)

    with pytest.raises(core.InstallError, match="tools/sigma already exists"):
        core.atomic_install(bundle, report, project_root=pr)

    assert not (pr / "skills" / "sigma").exists()


def test_atomic_install_rollback_on_second_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate OSError during the second rename (stage_tools → tools_dst).
    The helper must remove the already-renamed skills/<name>/ so no
    marker survives — next boot retries.
    """
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    report: dict[str, Any] = {"name": "tau"}

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    (pr / "tools").mkdir(parents=True)

    # Monkey-patch Path.rename so the SECOND call raises. We capture the
    # call count in a closure.
    original_rename = Path.rename
    counter = {"n": 0}

    def fake_rename(self: Path, target: Path) -> Path:
        counter["n"] += 1
        if counter["n"] == 2:
            raise OSError(1, "simulated ENOENT")
        return original_rename(self, target)

    monkeypatch.setattr(Path, "rename", fake_rename)
    with pytest.raises(core.InstallError, match="atomic rename failed"):
        core.atomic_install(bundle, report, project_root=pr)

    # Rollback must have happened — neither target dir nor any staging
    # dir must remain.
    assert not (pr / "skills" / "tau").exists()
    assert not (pr / "tools" / "tau").exists()
    # No leftover .tmp-* dirs either.
    assert not any(p.name.startswith(".tmp-") for p in (pr / "skills").iterdir())
    assert not any(p.name.startswith(".tmp-") for p in (pr / "tools").iterdir())


def test_atomic_install_without_tools_subdir(tmp_path: Path) -> None:
    """Skills with no ``tools/`` subdir install cleanly (tools_dst not created)."""
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "bundle"
    _write_bundle(bundle, with_tools=False)
    report: dict[str, Any] = {"name": "upsilon"}

    pr = tmp_path / "proj"
    (pr / "skills").mkdir(parents=True)
    (pr / "tools").mkdir(parents=True)

    core.atomic_install(bundle, report, project_root=pr)
    assert (pr / "skills" / "upsilon" / ".0xone-installed").is_file()
    # tools/upsilon was NOT created.
    assert not (pr / "tools" / "upsilon").exists()


def test_atomic_install_rejects_existing_skill(tmp_path: Path) -> None:
    from assistant.tools_sdk import _installer_core as core

    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    report: dict[str, Any] = {"name": "phi"}
    pr = tmp_path / "proj"
    (pr / "skills" / "phi").mkdir(parents=True)
    (pr / "tools").mkdir(parents=True)
    with pytest.raises(core.InstallError, match="already installed"):
        core.atomic_install(bundle, report, project_root=pr)
