"""Memory CLI `read`: existing, not-found, frontmatter parse error."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_read_happy_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="hello [[other]] world",
    )
    res = run_memory("read", "inbox/a.md", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    data = res.json_out["data"]
    assert data["frontmatter"]["title"] == "T"
    assert "hello" in data["body"]
    assert data["wikilinks"] == ["other"]


def test_read_not_found(tmp_path: Path) -> None:
    res = run_memory(
        "read", "inbox/missing.md", vault_dir=tmp_path / "v", index_db=tmp_path / "i.db"
    )
    assert res.rc == 7


def test_read_invalid_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    bad = vault / "inbox"
    bad.mkdir()
    (bad / "corrupt.md").write_text("no frontmatter here\n", encoding="utf-8")
    res = run_memory("read", "inbox/corrupt.md", vault_dir=vault, index_db=tmp_path / "i.db")
    assert res.rc == 3
    assert "frontmatter" in res.json_err["error"].lower()
