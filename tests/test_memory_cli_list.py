"""Memory CLI `list`: all / area-filtered / JSON shape / obsidian exclude."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def _seed(tmp_path: Path) -> tuple[Path, Path]:
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
        stdin="a",
    )
    run_memory(
        "write",
        "projects/x.md",
        "--title",
        "X",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="x",
    )
    return vault, idx


def test_list_all(tmp_path: Path) -> None:
    vault, idx = _seed(tmp_path)
    res = run_memory("list", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    notes = res.json_out["data"]["notes"]
    paths = {n["path"] for n in notes}
    assert paths == {"inbox/a.md", "projects/x.md"}


def test_list_area_filter(tmp_path: Path) -> None:
    vault, idx = _seed(tmp_path)
    res = run_memory("list", "--area", "inbox", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    notes = res.json_out["data"]["notes"]
    assert len(notes) == 1
    assert notes[0]["path"] == "inbox/a.md"


def test_list_skips_obsidian_metadata(tmp_path: Path) -> None:
    vault, idx = _seed(tmp_path)
    obsidian = vault / ".obsidian"
    obsidian.mkdir()
    (obsidian / "workspace.json").write_text('{"x": 1}', encoding="utf-8")
    # Plus a stray .md file under .obsidian (should also be skipped).
    (obsidian / "sneaky.md").write_text("---\ntitle: nope\n---\n", encoding="utf-8")

    res = run_memory("list", vault_dir=vault, index_db=idx)
    paths = {n["path"] for n in res.json_out["data"]["notes"]}
    # Workspace.json is not .md anyway; sneaky.md MUST be excluded by
    # _VAULT_SCAN_EXCLUDES.
    assert ".obsidian/sneaky.md" not in paths
    assert paths == {"inbox/a.md", "projects/x.md"}


def test_list_skips_tmp_and_git(tmp_path: Path) -> None:
    vault, idx = _seed(tmp_path)
    for excluded in (".tmp", ".git", ".trash"):
        d = vault / excluded
        d.mkdir(parents=True, exist_ok=True)
        (d / "hidden.md").write_text("---\ntitle: nope\n---\n", encoding="utf-8")
    res = run_memory("list", vault_dir=vault, index_db=idx)
    paths = {n["path"] for n in res.json_out["data"]["notes"]}
    assert all(not p.startswith((".tmp/", ".git/", ".trash/")) for p in paths)
