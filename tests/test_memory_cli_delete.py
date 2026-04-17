"""Memory CLI `delete`: success, FTS removal, not-found."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_delete_happy_path(tmp_path: Path) -> None:
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
        stdin="content",
    )
    res_del = run_memory("delete", "inbox/a.md", vault_dir=vault, index_db=idx)
    assert res_del.rc == 0
    assert res_del.json_out["data"]["deleted"] is True
    assert not (vault / "inbox" / "a.md").exists()

    # Confirm FTS removal: search returns no hit.
    res_search = run_memory("search", "content", vault_dir=vault, index_db=idx)
    assert res_search.json_out["data"]["hits"] == []


def test_delete_not_found(tmp_path: Path) -> None:
    res = run_memory(
        "delete",
        "inbox/missing.md",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
    )
    assert res.rc == 7
