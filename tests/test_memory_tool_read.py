"""memory_read handler — happy, not-found, path-escape, wikilink variants."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


async def _read(args: dict) -> dict:
    return await mm.memory_read.handler(args)


@pytest.mark.asyncio
async def test_memory_read_seed_note_structured_output(
    memory_ctx: tuple[Path, Path],
) -> None:
    """M2.6: all keys in structured output are JSON-serializable."""
    await _write(
        {
            "path": "projects/x.md",
            "title": "Project X",
            "body": "Body with [[other-note]].",
            "tags": ["project"],
        }
    )
    res = await _read({"path": "projects/x.md"})
    import json as _json

    _json.dumps(res, ensure_ascii=False)  # must not raise
    assert res["frontmatter"]["title"] == "Project X"
    assert res["wikilinks"] == ["other-note"]


@pytest.mark.asyncio
async def test_memory_read_happy_h1_fallback_title(
    memory_ctx: tuple[Path, Path],
) -> None:
    """H1: missing frontmatter title → derive from first H1."""
    vault, _idx = memory_ctx
    # Write directly to disk with no title field.
    (vault / "inbox").mkdir(parents=True, exist_ok=True)
    (vault / "inbox" / "h1.md").write_text(
        "---\ntags: []\n---\n\n# My Note Heading\nbody\n",
        encoding="utf-8",
    )
    res = await _read({"path": "inbox/h1.md"})
    assert res["frontmatter"]["title"] == "My Note Heading"


@pytest.mark.asyncio
async def test_memory_read_stem_fallback_title(
    memory_ctx: tuple[Path, Path],
) -> None:
    """Neither frontmatter nor H1 → title from filename stem."""
    vault, _idx = memory_ctx
    (vault / "inbox").mkdir(parents=True, exist_ok=True)
    (vault / "inbox" / "no-heading.md").write_text(
        "---\ntags: []\n---\n\njust body\n", encoding="utf-8"
    )
    res = await _read({"path": "inbox/no-heading.md"})
    assert res["frontmatter"]["title"] == "No Heading"


@pytest.mark.asyncio
async def test_memory_read_not_found(memory_ctx: tuple[Path, Path]) -> None:
    res = await _read({"path": "inbox/absent.md"})
    assert res.get("is_error") is True
    assert "(code=2)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_read_path_escape(memory_ctx: tuple[Path, Path]) -> None:
    res = await _read({"path": "../escape.md"})
    assert res.get("is_error") is True
    assert "(code=1)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_read_wikilink_alias_stripped(
    memory_ctx: tuple[Path, Path],
) -> None:
    """H6: ``[[target|alias]]`` → target only."""
    await _write(
        {
            "path": "inbox/w.md",
            "title": "W",
            "body": "see [[studio44-platform|Платформа]]",
        }
    )
    res = await _read({"path": "inbox/w.md"})
    assert res["wikilinks"] == ["studio44-platform"]


@pytest.mark.asyncio
async def test_memory_read_wikilink_block_ref_stripped(
    memory_ctx: tuple[Path, Path],
) -> None:
    """M2.4: ``[[target#section]]`` and ``[[target^id]]`` → target only."""
    await _write(
        {
            "path": "inbox/b.md",
            "title": "B",
            "body": "see [[foo#sec]] and [[bar^block-id]]",
        }
    )
    res = await _read({"path": "inbox/b.md"})
    assert res["wikilinks"] == ["foo", "bar"]


@pytest.mark.asyncio
async def test_memory_read_body_wrapped_with_nonce(
    memory_ctx: tuple[Path, Path],
) -> None:
    await _write({"path": "inbox/n.md", "title": "N", "body": "safe body"})
    res = await _read({"path": "inbox/n.md"})
    text = res["content"][0]["text"]
    assert "<untrusted-note-body-" in text
    assert "</untrusted-note-body-" in text


@pytest.mark.asyncio
async def test_memory_read_moc_rejected(memory_ctx: tuple[Path, Path]) -> None:
    res = await _read({"path": "_index.md"})
    assert res.get("is_error") is True
    assert "(code=1)" in res["content"][0]["text"]
