"""S13 wave-2: unconfirmed skill_install MUST preserve the cache.

A user who calls ``skill_install(confirmed=false)`` by accident (or the
model did so too eagerly) should be able to retry with ``confirmed=true``
without paying the fetch cost again.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_bundle(dest: Path, *, name: str = "alpha") -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Test skill {name}\n---\n\nBody\n",
        encoding="utf-8",
    )


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


async def test_unconfirmed_install_preserves_cache(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, data_dir = configured_installer
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_install, skill_preview

    fetch_calls: list[str] = []

    async def _fetch(url: str, dest: Path) -> None:
        fetch_calls.append(url)
        _write_bundle(dest, name="delta")

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)

    async def _fake_spawn(name: str, **_k: object) -> None:
        del name

    monkeypatch.setattr(core, "spawn_uv_sync_bg", _fake_spawn)

    url = "https://github.com/foo/delta"
    await skill_preview.handler({"url": url})
    assert len(fetch_calls) == 1

    # Unconfirmed → error, but cache untouched.
    result = await skill_install.handler({"url": url, "confirmed": False})
    assert result.get("is_error") is True
    assert result["code"] == 3  # CODE_NOT_CONFIRMED
    # Cache preserved per S13.
    canonical = core.canonicalize_url(url)
    cache_dir = data_dir / "run" / "installer-cache" / core.cache_key(canonical)
    assert (cache_dir / "manifest.json").is_file()
    assert (cache_dir / "bundle").is_dir()
    # Nothing installed on disk.
    assert not (project_root / "skills" / "delta").exists()

    # Retry with confirmed=true → install proceeds; fetch is called
    # again only for re-verification (the TOCTOU second fetch).
    result2 = await skill_install.handler({"url": url, "confirmed": True})
    assert result2.get("installed") is True
    # Exactly 2 fetch calls: preview + re-verify; NOT 3.
    assert len(fetch_calls) == 2
