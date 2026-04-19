#!/usr/bin/env python3
"""Phase 8 R-15 / B7: HOME discovery for `gh auth status` from daemon context.

Tests:
  1. HOME=/tmp/empty-nonsense → gh auth status exits non-zero.
  2. HOME=Path.home() → gh auth status exits 0 (this machine is logged in).
  3. Reference implementation `_verify_gh_config_accessible(home)` returns
     `(ok, home, reason)` tuple — exercised on both fixtures.

This gives coder the exact exit-code / stderr shape for the Daemon.start
preflight the plan mandates (§4 C6 preflight).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class ProbeResult:
    ok: bool
    home: str
    rc: int | None
    reason: str
    stdout: str = ""
    stderr: str = ""


def _verify_gh_config_accessible(home: str | Path, timeout_s: float = 10.0) -> ProbeResult:
    """Reference implementation for phase-8 `_verify_gh_config_accessible`.

    Spawns `gh auth status --hostname github.com` with HOME=home, token env
    vars scrubbed. Returns `ProbeResult` — `ok=True` iff rc==0.
    """
    if shutil.which("gh") is None:
        return ProbeResult(
            ok=False, home=str(home), rc=None, reason="gh_not_on_path"
        )
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
    }
    # PATH must include /opt/homebrew/bin or wherever gh lives. Copy
    # selected dirs if they exist.
    proc = subprocess.run(
        ["gh", "auth", "status", "--hostname", "github.com"],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    ok = proc.returncode == 0
    reason = "authed" if ok else _classify_stderr(proc.stderr)
    return ProbeResult(
        ok=ok,
        home=str(home),
        rc=proc.returncode,
        reason=reason,
        stdout=proc.stdout[:500],
        stderr=proc.stderr[:500],
    )


def _classify_stderr(stderr: str) -> str:
    low = stderr.lower()
    if "not logged in" in low or "not logged into" in low:
        return "not_logged_in"
    if "could not find config" in low:
        return "no_config"
    if "authenticated" not in low:
        return "unknown_error"
    return "unknown_error"


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_empty_home_") as td:
        empty = _verify_gh_config_accessible(td)
        report["empty_home"] = asdict(empty)

    real = _verify_gh_config_accessible(Path.home())
    report["real_home"] = asdict(real)

    # Final summary.
    report["summary"] = {
        "empty_home_expected_not_logged_in": (
            not empty.ok and empty.reason in {"not_logged_in", "unknown_error"}
        ),
        "real_home_logged_in": real.ok,
    }
    out_path = Path(__file__).with_name("spike_gh_home_probe_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
