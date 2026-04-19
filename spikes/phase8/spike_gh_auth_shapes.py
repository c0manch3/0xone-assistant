#!/usr/bin/env python3
"""Phase 8 R-1 + R-15: probe `gh auth status --hostname github.com` in various scenarios.

Runs `gh auth status --hostname github.com` under three envs:

  1. Fresh / not logged in — HOME pointed at an empty temp dir with no
     `.config/gh/` tree. Expected: non-zero exit, stderr text telling
     the user they need to log in.
  2. Logged in via OAuth — HOME left as the current user's home (this
     machine). Expected: exit 0 if the developer is already logged in
     against github.com, non-zero otherwise (we just capture what we
     observe).
  3. Pseudo-expired — temporarily rename ~/.config/gh/hosts.yml aside
     (NOT modified, NOT overwritten), run the probe, then restore.
     Expected: same shape as scenario 1 (gh acts as if not logged in).

Captures for each: rc, stdout, stderr, plus whether the `--json` output
flag is supported (newer gh versions) vs purely human-readable text.
Everything is dumped to spike_gh_auth_shapes_report.json.

IMPORTANT: step 3 performs a RENAME (mv) of the live `hosts.yml` to a
sibling `.spike-backup` filename, then restores it via a `finally:` —
no data is ever overwritten or deleted. A crash mid-probe leaves the
`.spike-backup` file in place; the user can `mv` it back by hand.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run_gh(env: dict[str, str], extra_args: list[str] | None = None) -> dict[str, object]:
    argv = ["gh", "auth", "status", "--hostname", "github.com"]
    if extra_args:
        argv.extend(extra_args)
    try:
        proc = subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "rc": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "error": "timeout",
        }
    except FileNotFoundError as exc:
        return {
            "argv": argv,
            "rc": None,
            "error": f"gh not found: {exc}",
        }
    return {
        "argv": argv,
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def scenario_1_fresh_home() -> dict[str, object]:
    """HOME points at empty temp dir, no $XDG_CONFIG_HOME."""
    with tempfile.TemporaryDirectory(prefix="spike_empty_home_") as td:
        env = {
            "HOME": td,
            "PATH": os.environ["PATH"],
        }
        # Scrub any token env vars that would shadow the config check.
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        return _run_gh(env)


def scenario_2_logged_in() -> dict[str, object]:
    """Use the real HOME — whatever the developer has configured."""
    env = {
        "HOME": str(Path.home()),
        "PATH": os.environ["PATH"],
    }
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return _run_gh(env)


def scenario_3_hidden_hosts_yml() -> dict[str, object]:
    """Rename hosts.yml aside (non-destructive); run probe; restore."""
    hosts_path = Path.home() / ".config" / "gh" / "hosts.yml"
    backup_path = hosts_path.with_suffix(".yml.spike-backup")
    if not hosts_path.exists():
        return {
            "skipped": True,
            "reason": f"{hosts_path} does not exist; cannot simulate expired.",
        }
    try:
        shutil.move(str(hosts_path), str(backup_path))
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ["PATH"],
        }
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        result = _run_gh(env)
        return result
    finally:
        if backup_path.exists():
            shutil.move(str(backup_path), str(hosts_path))


def probe_json_flag_support() -> dict[str, object]:
    """Does `gh auth status --json` exist on this version?"""
    env = {"HOME": str(Path.home()), "PATH": os.environ["PATH"]}
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return _run_gh(env, extra_args=["--json", "user"])


def gh_version() -> str:
    try:
        out = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip()
    except Exception as exc:
        return f"error: {exc}"


def main() -> int:
    report: dict[str, object] = {
        "gh_version": gh_version(),
        "scenario_1_fresh_home": scenario_1_fresh_home(),
        "scenario_2_current_home": scenario_2_logged_in(),
        "scenario_3_hidden_hosts_yml": scenario_3_hidden_hosts_yml(),
        "probe_json_flag_support": probe_json_flag_support(),
    }
    out_path = Path(__file__).with_name("spike_gh_auth_shapes_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_path}")
    for key, val in report.items():
        print(f"--- {key} ---")
        print(json.dumps(val, indent=2, default=str)[:600])
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
