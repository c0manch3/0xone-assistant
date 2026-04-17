"""Review wave 3: cmd_delete returns exit 7 under concurrent delete.

The original `exists()` check sat outside the lock. Two simultaneous
`memory delete` runs both saw True, one unlinked, the second raised
FileNotFoundError → EXIT_IO instead of the contractual EXIT_NOT_FOUND.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests._helpers.memory_cli import run_memory

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _PROJECT_ROOT / "tools" / "memory" / "main.py"


def _popen_delete(rel_path: str, vault: Path, idx: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["MEMORY_VAULT_DIR"] = str(vault)
    env["MEMORY_INDEX_DB_PATH"] = str(idx)
    return subprocess.Popen(
        [sys.executable, str(_MAIN), "delete", rel_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def test_concurrent_delete_second_returns_exit_7(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    # Seed one note.
    run_memory(
        "write",
        "inbox/shared.md",
        "--title",
        "Shared",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="content",
    )
    # Two deletes race; one wins with rc=0, the other must return rc=7
    # (EXIT_NOT_FOUND) rather than rc=4 (EXIT_IO) — contract invariant.
    p1 = _popen_delete("inbox/shared.md", vault, idx)
    p2 = _popen_delete("inbox/shared.md", vault, idx)
    out1, err1 = p1.communicate(timeout=15)
    out2, err2 = p2.communicate(timeout=15)

    rcs = sorted([p1.returncode, p2.returncode])
    assert rcs == [0, 7], (rcs, out1, err1, out2, err2)


def test_sequential_delete_not_found_exits_7(tmp_path: Path) -> None:
    """Sanity — single-shot delete of missing path still returns 7."""
    res = run_memory(
        "delete",
        "inbox/missing.md",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
    )
    assert res.rc == 7
