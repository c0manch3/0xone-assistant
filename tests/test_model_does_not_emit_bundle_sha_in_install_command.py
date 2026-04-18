"""B4 behavioural regression: the bash allowlist and installer CLI both
reject any hallucinated `--bundle-sha` flag that the model might emit
after seeing the bundle_sha in a Q1 synthetic history snippet.

Phase 3 review wave 2 removed `--bundle-sha` from the install CLI
because the model was losing the hash between turns. Phase 4 Q1 puts
the hash back into the model's visible history via the synthetic note —
this test is the regression gate that makes sure the model cannot
re-enable a TOCTOU bypass by re-emitting the flag.

We cannot run a real LLM turn here (billed + non-deterministic). The
test instead asserts two downstream guardrails:

1. The installer CLI (`tools/skill_installer/main.py install ...`)
   rejects `--bundle-sha` with argparse exit 2 — no silent acceptance.
2. A Bash argv containing `--bundle-sha` still passes the bash hook
   (the hook's job is to validate the program + argv surface, not the
   semantic correctness of a CLI flag). This is the NON-regression
   assertion: the hook must not have been tightened in a way that
   breaks legitimate `install --confirm --url X` invocations.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from assistant.bridge.hooks import check_bash_command

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER = _PROJECT_ROOT / "tools" / "skill_installer" / "main.py"


def test_cli_rejects_unknown_bundle_sha_flag() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(_INSTALLER),
            "install",
            "--confirm",
            "--url",
            "https://example.com/z",
            "--bundle-sha",
            "deadbeef",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    stderr = proc.stderr.lower()
    assert "bundle-sha" in stderr or "unrecognized" in stderr


def test_bash_hook_allows_clean_install_invocation() -> None:
    """Positive control: legitimate install command still passes the hook.

    If this breaks, phase 4 accidentally regressed the allowlist — the
    model would lose the ability to actually install skills.
    """
    cmd = (
        f"python {_INSTALLER.relative_to(_PROJECT_ROOT)} "
        "install --confirm --url https://example.com/x"
    )
    assert check_bash_command(cmd, _PROJECT_ROOT) is None


def test_bash_hook_passes_bundle_sha_flag_through_to_cli() -> None:
    """If the model hallucinates `--bundle-sha`, the bash hook itself
    does NOT block it (flags are the CLI's concern). Exit 2 from argparse
    is the enforcement point. This test documents the division of labour.
    """
    cmd = (
        f"python {_INSTALLER.relative_to(_PROJECT_ROOT)} "
        "install --confirm --url https://example.com/x --bundle-sha abc"
    )
    # Hook passes it through (no shell metachars, valid argv, within root).
    assert check_bash_command(cmd, _PROJECT_ROOT) is None
