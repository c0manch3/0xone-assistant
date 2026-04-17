"""Memory CLI concurrency: 2 simultaneous writes both land intact."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests._helpers.memory_cli import run_memory

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _PROJECT_ROOT / "tools" / "memory" / "main.py"


def _popen_write(
    rel_path: str, body: str, vault: Path, idx: Path
) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["MEMORY_VAULT_DIR"] = str(vault)
    env["MEMORY_INDEX_DB_PATH"] = str(idx)
    return subprocess.Popen(
        [
            sys.executable,
            str(_MAIN),
            "write",
            rel_path,
            "--title",
            rel_path,
            "--body",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def test_parallel_writes_both_visible(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"

    # Ensure vault+index exist (otherwise the lock probe races on first run).
    run_memory(
        "write",
        "inbox/seed.md",
        "--title",
        "Seed",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="s",
    )

    p1 = _popen_write("inbox/a.md", "alphabodyalpha", vault, idx)
    p2 = _popen_write("inbox/b.md", "betabodybeta", vault, idx)
    out1, err1 = p1.communicate(input="alphabodyalpha", timeout=30)
    out2, err2 = p2.communicate(input="betabodybeta", timeout=30)
    assert p1.returncode == 0, (out1, err1)
    assert p2.returncode == 0, (out2, err2)

    # Both files on disk.
    assert (vault / "inbox" / "a.md").exists()
    assert (vault / "inbox" / "b.md").exists()

    # Search returns both.
    r_a = run_memory("search", "alphabodyalpha", vault_dir=vault, index_db=idx)
    r_b = run_memory("search", "betabodybeta", vault_dir=vault, index_db=idx)
    assert len(r_a.json_out["data"]["hits"]) == 1
    assert len(r_b.json_out["data"]["hits"]) == 1
    assert r_a.json_out["data"]["hits"][0]["path"] == "inbox/a.md"
    assert r_b.json_out["data"]["hits"][0]["path"] == "inbox/b.md"
