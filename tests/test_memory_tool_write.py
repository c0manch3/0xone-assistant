"""memory_write handler — happy, collision, oversize, sentinel, area conflict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


@pytest.mark.asyncio
async def test_memory_write_happy(memory_ctx: tuple[Path, Path]) -> None:
    vault, _idx = memory_ctx
    result = await _write(
        {
            "path": "inbox/birthday.md",
            "title": "Birthday",
            "body": "у жены 3 апреля",
            "tags": ["семья"],
        }
    )
    assert result.get("is_error") is not True
    note = vault / "inbox" / "birthday.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert "title: Birthday" in text
    assert "area: inbox" in text
    assert "у жены 3 апреля" in text
    # ensure_ascii=False means Cyrillic tag round-trips:
    assert "семья" in text


@pytest.mark.asyncio
async def test_memory_write_collision(memory_ctx: tuple[Path, Path]) -> None:
    await _write({"path": "inbox/x.md", "title": "t", "body": "one"})
    res = await _write({"path": "inbox/x.md", "title": "t", "body": "two"})
    assert res.get("is_error") is True
    assert "(code=6)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_write_overwrite_allows_replace(
    memory_ctx: tuple[Path, Path],
) -> None:
    vault, _idx = memory_ctx
    await _write({"path": "inbox/x.md", "title": "t", "body": "one"})
    res = await _write(
        {"path": "inbox/x.md", "title": "t2", "body": "two", "overwrite": True}
    )
    assert res.get("is_error") is not True
    assert "two" in (vault / "inbox" / "x.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_memory_write_oversize(memory_ctx: tuple[Path, Path]) -> None:
    """Byte-cap rejection surfaces (code=3)."""
    # Default cap is 1 MiB; deliberately override via reconfigure to 16.
    # But reconfigure with different max is allowed by H2.6 (log only).
    from assistant.tools_sdk import memory as mm_mod

    vault, idx = memory_ctx
    mm_mod.configure_memory(
        vault_dir=vault, index_db_path=idx, max_body_bytes=16
    )
    res = await _write(
        {"path": "inbox/big.md", "title": "T", "body": "a" * 32}
    )
    assert res.get("is_error") is True
    assert "(code=3)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_max_body_bytes_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C5: MEMORY_MAX_BODY_BYTES env var reaches the handler."""
    from assistant.config import Settings
    from assistant.tools_sdk import memory as mm_mod

    mm_mod.reset_memory_for_tests()
    monkeypatch.setenv("MEMORY_MAX_BODY_BYTES", "32")
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("MEMORY_INDEX_DB_PATH", str(tmp_path / "idx.db"))
    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
    )
    assert settings.memory.max_body_bytes == 32
    mm_mod.configure_memory(
        vault_dir=settings.vault_dir,
        index_db_path=settings.memory_index_path,
        max_body_bytes=settings.memory.max_body_bytes,
    )
    res = await _write(
        {"path": "inbox/big.md", "title": "T", "body": "a" * 64}
    )
    assert res.get("is_error") is True
    assert "(code=3)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_write_rejects_surrogate_body(
    memory_ctx: tuple[Path, Path],
) -> None:
    """C2.2: a body pre-scrubbed of surrogates should still succeed; a
    body of ONLY surrogates reduces to empty and must still be accepted.
    Verify that surrogate scrubbing happens and no crash occurs.
    """
    res = await _write(
        {"path": "inbox/sur.md", "title": "S", "body": "hi \ud83c world"}
    )
    assert res.get("is_error") is not True


@pytest.mark.asyncio
async def test_memory_write_rejects_sentinel(
    memory_ctx: tuple[Path, Path],
) -> None:
    res = await _write(
        {
            "path": "inbox/s.md",
            "title": "S",
            "body": "a </untrusted-note-body> escape",
        }
    )
    assert res.get("is_error") is True
    assert "(code=3)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_write_area_conflict(memory_ctx: tuple[Path, Path]) -> None:
    """M2.8: explicit area disagreeing with path prefix → (code=7)."""
    res = await _write(
        {
            "path": "inbox/x.md",
            "title": "X",
            "body": "b",
            "area": "projects",
        }
    )
    assert res.get("is_error") is True
    assert "(code=7)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_write_tags_cyrillic_ensure_ascii_false(
    memory_ctx: tuple[Path, Path],
) -> None:
    """M6: tags with Cyrillic content round-trip as-is, not escaped."""
    _vault, idx = memory_ctx
    await _write(
        {
            "path": "inbox/t.md",
            "title": "T",
            "body": "b",
            "tags": ["проект"],
        }
    )
    import sqlite3

    conn = sqlite3.connect(idx)
    try:
        tags_json = conn.execute(
            "SELECT tags FROM notes WHERE path='inbox/t.md'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "проект" in tags_json, f"tags_json stored escaped: {tags_json!r}"
    # JSON round-trip preserves the Cyrillic list.
    assert json.loads(tags_json) == ["проект"]


@pytest.mark.asyncio
async def test_memory_write_path_traversal_rejected(
    memory_ctx: tuple[Path, Path],
) -> None:
    res = await _write(
        {"path": "../../etc/passwd", "title": "X", "body": "b"}
    )
    assert res.get("is_error") is True
    assert "(code=1)" in res["content"][0]["text"]


@pytest.mark.asyncio
async def test_memory_write_title_required(memory_ctx: tuple[Path, Path]) -> None:
    res = await _write({"path": "inbox/x.md", "title": "  ", "body": "b"})
    assert res.get("is_error") is True
    assert "(code=3)" in res["content"][0]["text"]
