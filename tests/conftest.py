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
