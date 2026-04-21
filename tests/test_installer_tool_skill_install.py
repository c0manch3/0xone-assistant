"""Tests for ``mcp__installer__skill_install`` — preview→confirm→install."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_bundle(dest: Path, *, name: str = "alpha", with_tools: bool = False) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}\n---\n\nBody\n",
        encoding="utf-8",
    )
    if with_tools:
        tools = dest / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "main.py").write_text("print('hi')\n", encoding="utf-8")


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


async def test_skill_install_happy_path(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, data_dir = configured_installer
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_install, skill_preview

    async def _fetch(url: str, dest: Path) -> None:
        del url
        _write_bundle(dest, name="beta")

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    # Skip actual uv sync invocation.
    called: list[str] = []

    async def _fake_spawn(name: str, **_k: object) -> None:
        called.append(name)

    monkeypatch.setattr(core, "spawn_uv_sync_bg", _fake_spawn)

    url = "https://github.com/foo/beta"
    preview = await skill_preview.handler({"url": url})
    assert preview["preview"]["name"] == "beta"

    install = await skill_install.handler({"url": url, "confirmed": True})
    assert install.get("installed") is True
    assert install["name"] == "beta"
    assert install["sync_pending"] is False  # no tools/ subdir

    marker = project_root / "skills" / "beta" / ".0xone-installed"
    assert marker.is_file()
    sentinel = data_dir / "run" / "skills.dirty"
    assert sentinel.is_file()
    # Cache was cleaned up after install.
    canonical = core.canonicalize_url(url)
    cache_dir = data_dir / "run" / "installer-cache" / core.cache_key(canonical)
    assert not cache_dir.exists()


async def test_skill_install_with_tools_sync_pending(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, _ = configured_installer
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_install, skill_preview

    async def _fetch(url: str, dest: Path) -> None:
        del url
        _write_bundle(dest, name="gamma", with_tools=True)

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    called: list[str] = []

    async def _fake_spawn(name: str, **_k: object) -> None:
        called.append(name)

    monkeypatch.setattr(core, "spawn_uv_sync_bg", _fake_spawn)

    url = "https://github.com/foo/gamma"
    await skill_preview.handler({"url": url})
    install = await skill_install.handler({"url": url, "confirmed": True})
    assert install.get("installed") is True
    assert install["sync_pending"] is True
    assert called == ["gamma"]
    # tools/ was moved into project_root/tools/gamma/
    assert (project_root / "tools" / "gamma" / "main.py").is_file()
    # and is no longer inside skills/<name>/
    assert not (project_root / "skills" / "gamma" / "tools").exists()


async def test_skill_install_no_preview(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_install

    result = await skill_install.handler({"url": "https://github.com/foo/nope", "confirmed": True})
    assert result.get("is_error") is True
    assert result["code"] == 2  # CODE_NOT_PREVIEWED
