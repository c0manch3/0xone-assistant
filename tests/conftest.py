"""Test-suite wiring.

Puts `tools/skill-installer/` on `sys.path` before any test module is
imported so tests can `from _lib.fetch import ...` without having to
import a side-effect module first (which was brittle under ruff's
isort-style import reordering).
"""

from __future__ import annotations

import sys
from pathlib import Path

_INSTALLER_DIR = Path(__file__).resolve().parents[1] / "tools" / "skill-installer"
if str(_INSTALLER_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALLER_DIR))

# Phase 4: memory lives under `tools/memory/_memlib/` — a distinct package
# name from skill-installer's `_lib` so adding the memory dir to sys.path
# does not shadow the installer's namespace. `tools/memory/main.py` is NOT
# accessible as `import main` because of the same name collision with
# skill-installer; memory tests use the subprocess helper
# `tests._helpers.memory_cli.run_memory` instead. Appending (not inserting
# at index 0) preserves skill-installer's priority for bare names like
# `main`.
_MEMORY_DIR = Path(__file__).resolve().parents[1] / "tools" / "memory"
if str(_MEMORY_DIR) not in sys.path:
    sys.path.append(str(_MEMORY_DIR))
