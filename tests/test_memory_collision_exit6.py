"""Memory CLI collision: write existing without --overwrite -> exit 6."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_collision_exits_6(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    first = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="v1",
    )
    assert first.rc == 0

    second = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="v2",
    )
    assert second.rc == 6
    err = second.json_err
    assert err["ok"] is False
    assert "collision" in err["error"].lower()


def test_overwrite_accepts_existing(tmp_path: Path) -> None:
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
        stdin="v1",
    )
    third = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--overwrite",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="v2",
    )
    assert third.rc == 0
    assert "v2" in (vault / "inbox" / "a.md").read_text(encoding="utf-8")
