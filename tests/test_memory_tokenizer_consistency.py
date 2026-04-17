"""Nice-to-have #12: daemon `MemorySettings.fts_tokenizer` and the CLI
`_resolve_tokenizer()` must read the same env variable and default.

Without this the daemon would build the system prompt with one tokenizer
while the CLI would create the index with another — queries would
misbehave after reindex.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from assistant.config import MemorySettings

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _PROJECT_ROOT / "tools" / "memory" / "main.py"


def test_default_tokenizer_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORY_FTS_TOKENIZER", raising=False)
    daemon = MemorySettings().fts_tokenizer

    # Ask the CLI for its resolved tokenizer via a tiny introspection
    # one-liner. This is the tightest integration boundary available
    # without hooking into pydantic from a stdlib-only tool.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(_MAIN.parent)!r}); "
                "from main import _resolve_tokenizer; "
                "print(_resolve_tokenizer())"
            ),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_FTS_TOKENIZER": ""},
        check=True,
        timeout=10,
    )
    cli = proc.stdout.strip()
    assert daemon == cli, f"daemon={daemon!r} vs cli={cli!r}"


def test_env_override_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_FTS_TOKENIZER", "unicode61")
    daemon = MemorySettings().fts_tokenizer
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(_MAIN.parent)!r}); "
                "from main import _resolve_tokenizer; "
                "print(_resolve_tokenizer())"
            ),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_FTS_TOKENIZER": "unicode61"},
        check=True,
        timeout=10,
    )
    assert daemon == "unicode61"
    assert proc.stdout.strip() == "unicode61"
