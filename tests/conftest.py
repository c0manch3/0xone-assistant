"""Test-suite wiring.

Phase-7 (Q9a tech-debt close): `tools/` is a real Python package now —
test modules import as `from tools.skill_installer._lib.foo import bar`
and `from tools.memory._lib.frontmatter import sanitize_body`. The
legacy `_INSTALLER_DIR` / `_MEMORY_DIR` `sys.path` shims that prepended
the bare-name `_lib` packages were removed in commit
"phase 7: _lib package refactor"; bare `from _lib...` imports no longer
work.

Kept intentionally minimal so a missing import surfaces as a real
`ModuleNotFoundError` at collection time rather than being papered over
by a sys.path side-effect.
"""

from __future__ import annotations
