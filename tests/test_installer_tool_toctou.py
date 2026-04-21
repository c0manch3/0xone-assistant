"""TOCTOU: bundle changes between preview and install → CODE_TOCTOU."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def configured_installer(tmp_path: Path) -> tuple[Path, Path]:
    from assistant.tools_sdk.installer import configure_installer

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "skills").mkdir()
    (project_root / "tools").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    configure_installer(project_root=project_root, data_dir=data_dir)
    return project_root, data_dir


async def test_toctou_detected(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, data_dir = configured_installer
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_install, skill_preview

    call_count = {"n": 0}

    async def _fetch(url: str, dest: Path) -> None:
        del url
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        call_count["n"] += 1
        content = "Body v1" if call_count["n"] == 1 else "Body v2 (mutated)"
        (dest / "SKILL.md").write_text(
            f"---\nname: epsilon\ndescription: Test\n---\n\n{content}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)

    async def _fake_spawn(name: str, **_k: object) -> None:
        del name

    monkeypatch.setattr(core, "spawn_uv_sync_bg", _fake_spawn)

    url = "https://github.com/foo/epsilon"
    preview = await skill_preview.handler({"url": url})
    assert preview["preview"]["name"] == "epsilon"

    # Second fetch returns mutated content → SHA mismatch.
    result = await skill_install.handler({"url": url, "confirmed": True})
    assert result.get("is_error") is True
    assert result["code"] == 7  # CODE_TOCTOU
    # Cache cleared after TOCTOU detection.
    canonical = core.canonicalize_url(url)
    cache_dir = data_dir / "run" / "installer-cache" / core.cache_key(canonical)
    assert not cache_dir.exists()
    # Nothing installed.
    assert not (project_root / "skills" / "epsilon").exists()
