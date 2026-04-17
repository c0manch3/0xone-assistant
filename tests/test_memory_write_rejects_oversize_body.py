"""S4: body exceeding MEMORY_MAX_BODY_BYTES is rejected."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_oversize_body_exits_3(tmp_path: Path) -> None:
    big = "x" * 3000
    res = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Big",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin=big,
        env_extra={"MEMORY_MAX_BODY_BYTES": "2000"},
    )
    assert res.rc == 3
    err = res.json_err
    assert "MEMORY_MAX_BODY_BYTES" in err["error"]


def test_allows_body_under_cap(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Small",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="x" * 500,
        env_extra={"MEMORY_MAX_BODY_BYTES": "2000"},
    )
    assert res.rc == 0
