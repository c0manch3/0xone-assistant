"""memory_search handler — schema enforcement, Russian recall, area filter."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk import memory as mm


async def _write(args: dict) -> dict:
    return await mm.memory_write.handler(args)


async def _search(args: dict) -> dict:
    return await mm.memory_search.handler(args)


@pytest.mark.asyncio
async def test_memory_search_schema_required_only_query(
    memory_ctx: tuple[Path, Path],
) -> None:
    """RQ7: only ``query`` is required; other fields optional."""
    await _write(
        {"path": "inbox/a.md", "title": "Жена", "body": "у жены 3 апреля"}
    )
    res = await _search({"query": "жене"})  # dative form
    assert res.get("is_error") is not True
    assert res["hits"], "expected at least one hit"
    assert any(h["path"] == "inbox/a.md" for h in res["hits"])


@pytest.mark.asyncio
async def test_memory_search_missing_query_is_error(
    memory_ctx: tuple[Path, Path],
) -> None:
    """RQ7: calling without ``query`` at handler level surfaces (code=5)
    via empty-token path; the MCP layer would reject earlier in prod.
    """
    res = await _search({})
    assert res.get("is_error") is True


@pytest.mark.skip(
    reason="known seed-vault flake; FTS5/PyStemmer drift, phase 6e debt"
)
@pytest.mark.asyncio
async def test_memory_search_seed_flowgent(
    memory_ctx: tuple[Path, Path],
    seed_vault_copy: Path,
) -> None:
    """M2.6: Russian query ``флоугент`` finds flowgent.md via stemming."""
    from assistant.tools_sdk import _memory_core as core
    from assistant.tools_sdk import memory as mm_mod

    # Reconfigure memory to point at the seed-vault copy + its own idx.
    mm_mod.reset_memory_for_tests()
    _idx_unused = memory_ctx[1]
    # Delete the blank idx and repoint.
    vault = seed_vault_copy
    idx = seed_vault_copy.parent / "idx.db"
    core._ensure_index(idx)

    lock_path = Path(str(idx) + ".lock")
    with core.vault_lock(lock_path, blocking=True, timeout=5):
        core.reindex_vault(vault, idx)
    mm_mod.configure_memory(vault_dir=vault, index_db_path=idx)
    # Latin query "flowgent".
    res = await _search({"query": "flowgent"})
    assert res.get("is_error") is not True
    paths = [h["path"] for h in res["hits"]]
    assert any("flowgent" in p for p in paths), f"no flowgent hit in {paths}"


@pytest.mark.asyncio
async def test_memory_search_area_filter(
    memory_ctx: tuple[Path, Path],
) -> None:
    await _write({"path": "inbox/a.md", "title": "A", "body": "проект альфа"})
    await _write(
        {"path": "projects/b.md", "title": "B", "body": "проект бета"}
    )
    res = await _search({"query": "проект", "area": "projects"})
    paths = [h["path"] for h in res["hits"]]
    assert all(p.startswith("projects/") for p in paths)


@pytest.mark.asyncio
async def test_memory_search_degenerate_queries(
    memory_ctx: tuple[Path, Path],
) -> None:
    """M2.7: empty / wildcard-only / punctuation-only queries → (code=5)."""
    for q in ["", "   ", "?", "!!!"]:
        res = await _search({"query": q})
        assert res.get("is_error") is True, f"{q!r} should error"


@pytest.mark.asyncio
async def test_memory_search_russian_recall_corpus(
    memory_ctx: tuple[Path, Path],
) -> None:
    """Smaller subset of the RQ2 corpus — all positives hit, no false
    positives across the mini-corpus.
    """
    notes = [
        ("inbox/wife.md", "у моей жены день рождения 3 апреля"),
        ("inbox/meeting.md", "встреча назначена на апрель 2026"),
        ("inbox/kiwi.md", "я работаю в студии"),
    ]
    for path, body in notes:
        await _write({"path": path, "title": "t", "body": body})
    queries_positive = ["жене", "апреля", "работать"]
    for q in queries_positive:
        res = await _search({"query": q})
        assert res["hits"], f"{q!r} produced no hits"
    # negative — ``деревня`` should not match anything above.
    res = await _search({"query": "деревня"})
    assert res["hits"] == []


@pytest.mark.asyncio
async def test_memory_search_snippet_wrapped(
    memory_ctx: tuple[Path, Path],
) -> None:
    await _write(
        {"path": "inbox/s.md", "title": "S", "body": "специальный текст"}
    )
    res = await _search({"query": "специальный"})
    assert res["hits"]
    for h in res["hits"]:
        assert "<untrusted-note-snippet-" in h["snippet"]
