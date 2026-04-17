"""Test helper: run `tools/memory/main.py` under the project venv interpreter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAIN = _PROJECT_ROOT / "tools" / "memory" / "main.py"


@dataclass(frozen=True, slots=True)
class MemoryRun:
    rc: int
    stdout: str
    stderr: str

    @property
    def json_out(self) -> dict[str, object]:
        return json.loads(self.stdout.strip().splitlines()[-1])

    @property
    def json_err(self) -> dict[str, object]:
        return json.loads(self.stderr.strip().splitlines()[-1])


def run_memory(
    *argv: str,
    vault_dir: Path,
    index_db: Path,
    stdin: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> MemoryRun:
    """Invoke the CLI as a subprocess, isolated to `vault_dir`/`index_db`."""
    env = dict(os.environ)
    env["MEMORY_VAULT_DIR"] = str(vault_dir)
    env["MEMORY_INDEX_DB_PATH"] = str(index_db)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(_MAIN), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    return MemoryRun(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
