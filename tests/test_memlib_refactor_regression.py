"""Phase-7 commit-2 regression: every CLI under `tools/` is launchable in
both invocation forms after the package refactor that closed the legacy
`_lib` bare-name shim (Q9a tech debt).

Two contracts under test:

1. **Both invocations work.** `python tools/<name>/main.py --help` (the
   form the owner usually types in a shell) AND
   `python -m tools.<name>.main --help` (the form a subprocess spawn
   prefers because it avoids cwd assumptions) must both exit 0 and
   print an argparse usage line. The CLI's `sys.path` pragma at the
   top of each `main.py` is what makes the cwd-launch form resolve
   `from tools.<name>._lib...` imports without a separate shim.

2. **Package imports resolve cleanly.** `from tools.memory._lib...` and
   `from tools.skill_installer._lib...` import without any `sys.path`
   side-effect from `tests/conftest.py`. The legacy `_INSTALLER_DIR` /
   `_MEMORY_DIR` shims and the bare `_lib` package names they enabled
   are gone; if pytest can collect this module the `tools/__init__.py`
   marker is in place and the canonical paths resolve.

If either contract regresses, every downstream phase-7 tool commit
(transcribe / genimage / extract_doc / render_doc) loses its sub-process
spawn invariant — keep this test wired to CI on every PR touching
`tools/`.

Naming note (user-task vs spec): the user's ad-hoc task list mentioned
`scheduler` and `skill-creator`. The actual on-disk tool directory is
`tools/schedule/` (no `tools/scheduler/`); `skill-creator` is a runtime-
installed *skill*, not a `tools/` package. The canonical regression
inventory in `plan/phase7/detailed-plan.md` §11.5 covers the four
real `tools/` packages — `memory`, `schedule`, `task`, `skill_installer` —
and is what we exercise here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Ordered to match `plan/phase7/detailed-plan.md` §11.5 exactly.
_TOOLS: tuple[str, ...] = ("memory", "schedule", "task", "skill_installer")


def _cwd_argv(tool: str) -> list[str]:
    return [sys.executable, f"tools/{tool}/main.py", "--help"]


def _module_argv(tool: str) -> list[str]:
    return [sys.executable, "-m", f"tools.{tool}.main", "--help"]


_INVOCATIONS: tuple[tuple[str, callable], ...] = (
    ("cwd", _cwd_argv),
    ("module", _module_argv),
)


@pytest.mark.parametrize("tool", _TOOLS)
@pytest.mark.parametrize("mode,argv_fn", _INVOCATIONS, ids=[m for m, _ in _INVOCATIONS])
def test_cli_invocation_works(tool: str, mode: str, argv_fn) -> None:
    """Both `python tools/X/main.py --help` and `python -m tools.X.main --help`
    exit 0 with an argparse `usage:` anchor on stdout.

    ``cwd`` mode exercises the sys.path pragma at the top of each main.py
    (project root injected so `from tools.X._lib...` resolves under direct
    invocation, where `__package__` is empty).

    ``module`` mode exercises canonical package resolution (no pragma path
    needed; importlib uses `tools/__init__.py` directly).
    """
    proc = subprocess.run(
        argv_fn(tool),
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{tool}/{mode} invocation failed (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "usage:" in proc.stdout.lower(), (
        f"{tool}/{mode}: argparse `usage:` line missing from --help stdout\n"
        f"stdout:\n{proc.stdout}"
    )


def test_import_sanity() -> None:
    """Package imports resolve without sys.path tricks.

    `tools/memory/_lib/` and `tools/skill_installer/_lib/` are the only
    `_lib` sub-packages today (`schedule` and `task` import from
    `assistant.*` directly). If `tools/__init__.py` is missing or the
    `_lib` rename did not land, this collection-time import fails first
    and the parametrised CLI tests below never run.
    """
    from tools.memory._lib import frontmatter as _mem_frontmatter  # noqa: F401
    from tools.memory._lib import vault as _mem_vault  # noqa: F401
    from tools.skill_installer._lib import fetch as _si_fetch  # noqa: F401
    from tools.skill_installer._lib import validate as _si_validate  # noqa: F401
