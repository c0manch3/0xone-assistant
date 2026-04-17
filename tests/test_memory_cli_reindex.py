"""Memory CLI `reindex`: wipe + rebuild from FS."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_reindex_restores_index(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "A",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="uniquetoken",
    )

    # Corrupt the index by wiping notes (FTS5 trigger cascade removes notes_fts).
    conn = sqlite3.connect(idx)
    conn.execute("DELETE FROM notes")
    conn.commit()
    conn.close()

    miss = run_memory("search", "uniquetoken", vault_dir=vault, index_db=idx)
    assert miss.json_out["data"]["hits"] == []

    rx = run_memory("reindex", vault_dir=vault, index_db=idx)
    assert rx.rc == 0
    assert rx.json_out["data"]["reindexed"] == 1

    hit = run_memory("search", "uniquetoken", vault_dir=vault, index_db=idx)
    assert len(hit.json_out["data"]["hits"]) == 1
    assert hit.json_out["data"]["hits"][0]["path"] == "inbox/a.md"


def test_reindex_reports_parse_errors(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    # Seed one valid note, then drop a malformed one.
    run_memory(
        "write",
        "inbox/good.md",
        "--title",
        "Good",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="good",
    )
    bad = vault / "inbox" / "bad.md"
    bad.write_text("no frontmatter\n", encoding="utf-8")

    rx = run_memory("reindex", vault_dir=vault, index_db=idx)
    assert rx.rc == 0
    data = rx.json_out["data"]
    assert data["reindexed"] == 1
    assert len(data["parse_errors"]) == 1
    assert data["parse_errors"][0]["path"] == "inbox/bad.md"
