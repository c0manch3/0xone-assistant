"""Memory CLI `search`: FTS5 MATCH, area filter, limit, empty results."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Apple",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="day is Tuesday",
    )
    run_memory(
        "write",
        "projects/x.md",
        "--title",
        "Xerox",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="day is Wednesday",
    )
    return vault, idx


def test_search_match(tmp_path: Path) -> None:
    vault, idx = _prepare(tmp_path)
    res = run_memory("search", "Tuesday", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    hits = res.json_out["data"]["hits"]
    assert len(hits) == 1
    assert hits[0]["path"] == "inbox/a.md"


def test_search_area_filter(tmp_path: Path) -> None:
    vault, idx = _prepare(tmp_path)
    # Both notes contain the word "day"; the area filter narrows it.
    res = run_memory("search", "day", "--area", "inbox", vault_dir=vault, index_db=idx)
    paths = {h["path"] for h in res.json_out["data"]["hits"]}
    assert paths == {"inbox/a.md"}


def test_search_empty_results(tmp_path: Path) -> None:
    vault, idx = _prepare(tmp_path)
    res = run_memory("search", "nonexistent", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    assert res.json_out["data"]["hits"] == []


def test_search_limit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    for i in range(5):
        run_memory(
            "write",
            f"inbox/n{i}.md",
            "--title",
            f"Note {i}",
            "--body",
            "-",
            vault_dir=vault,
            index_db=idx,
            stdin="common",
        )
    res = run_memory("search", "common", "--limit", "2", vault_dir=vault, index_db=idx)
    assert len(res.json_out["data"]["hits"]) == 2
