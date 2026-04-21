"""Tests for ``mcp__installer__skill_preview``.

The ``@tool`` handler is invoked via its ``.handler(args)`` attribute
attached by the SDK's :func:`claude_agent_sdk.tool` decorator. Network
fetches are replaced by a monkeypatched :func:`core.fetch_bundle_async`
that populates the destination with a minimal valid bundle.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
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


def _fake_fetch_factory(name: str) -> Callable[[str, Path], Awaitable[None]]:
    async def _fetch(url: str, dest: Path) -> None:
        del url
        _write_bundle(dest, name=name)

    return _fetch


async def test_skill_preview_writes_manifest(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, data_dir = configured_installer
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_preview

    monkeypatch.setattr(core, "fetch_bundle_async", _fake_fetch_factory("alpha"))

    url = "https://github.com/foo/alpha"
    result = await skill_preview.handler({"url": url})

    assert "preview" in result
    assert result["preview"]["name"] == "alpha"
    assert result["preview"]["description"] == "Test skill alpha"
    assert result["preview"]["file_count"] == 1
    assert result["preview"]["has_tools_dir"] is False
    assert len(result["preview"]["source_sha"]) == 64
    assert "confirm_hint" in result

    # Manifest file lives under the installer cache.
    canonical = core.canonicalize_url(url)
    cache_dir = data_dir / "run" / "installer-cache" / core.cache_key(canonical)
    manifest = json.loads((cache_dir / "manifest.json").read_text("utf-8"))
    assert manifest["url"] == canonical
    assert manifest["file_count"] == 1
    assert manifest["report"]["name"] == "alpha"


async def test_skill_preview_bad_url(
    configured_installer: tuple[Path, Path],
) -> None:
    from assistant.tools_sdk.installer import skill_preview

    result = await skill_preview.handler({"url": "ftp://example.com/foo"})
    assert result.get("is_error") is True
    assert result["code"] == 1  # CODE_URL_BAD


async def test_skill_preview_validation_failure(
    configured_installer: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetching a bundle with no SKILL.md raises ValidationError → code 5."""
    from assistant.tools_sdk import _installer_core as core
    from assistant.tools_sdk.installer import skill_preview

    async def _fetch(url: str, dest: Path) -> None:
        del url
        dest.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 — test stub
        (dest / "README.md").write_text("no SKILL.md here", encoding="utf-8")

    monkeypatch.setattr(core, "fetch_bundle_async", _fetch)
    result = await skill_preview.handler({"url": "https://github.com/foo/bad"})
    assert result.get("is_error") is True
    assert result["code"] == 5  # CODE_VALIDATION
