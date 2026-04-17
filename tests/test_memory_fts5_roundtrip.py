"""Memory CLI FTS5 roundtrip: write → search returns the note we wrote."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_title_searchable(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Wife Birthday",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="on 3 april",
    )
    r = run_memory("search", "Birthday", vault_dir=vault, index_db=idx)
    hits = r.json_out["data"]["hits"]
    assert len(hits) == 1
    assert hits[0]["path"] == "inbox/a.md"


def test_body_searchable(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Wife Birthday",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="on 3 april",
    )
    r = run_memory("search", "april", vault_dir=vault, index_db=idx)
    hits = r.json_out["data"]["hits"]
    assert len(hits) == 1


def test_tags_searchable(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/people.md",
        "--title",
        "People",
        "--tags",
        "family,wife",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="notes",
    )
    r = run_memory("search", "family", vault_dir=vault, index_db=idx)
    assert len(r.json_out["data"]["hits"]) == 1
