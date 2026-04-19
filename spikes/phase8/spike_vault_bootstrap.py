#!/usr/bin/env python3
"""Phase 8 R-8 / R-14 / Q9: emulate vault-dir bootstrap + push to local bare repo.

Steps:
  1. Create `<tmp>/vault_dir/` (empty, no .git).
  2. Create `<tmp>/vault-bare.git/` (`git init --bare`).
  3. Bootstrap vault_dir:
     * git init
     * git checkout -b main
     * git remote add vault-backup file://.../vault-bare.git
     * empty commit ("bootstrap")
  4. Seed a markdown file, git add, commit.
  5. `git push vault-backup main` — file URL, no ssh.
  6. Verify `/tmp/vault-bare.git/refs/heads/main` exists on remote.
  7. Second push after changing file → expected to succeed (ff).
  8. Divergence test: second local clone commits + pushes; first clone
     commits + pushes — expect non-fast-forward rejection (exit 1 with
     'rejected' in stderr). That maps to phase-8 exit 7.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def git(args: list[str], repo: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "spike",
        "GIT_AUTHOR_EMAIL": "spike@localhost",
        "GIT_COMMITTER_NAME": "spike",
        "GIT_COMMITTER_EMAIL": "spike@localhost",
        "GIT_TERMINAL_PROMPT": "0",
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed: rc={proc.returncode}\nstderr={proc.stderr}"
        )
    return proc


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_vault_bootstrap_") as td:
        base = Path(td)
        vault_dir = base / "vault_dir"
        vault_dir.mkdir()
        bare = base / "vault-bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)

        # Bootstrap.
        git(["init", "-q", "-b", "main"], vault_dir)
        git(["remote", "add", "vault-backup", f"file://{bare}"], vault_dir)
        git(["commit", "--allow-empty", "-q", "-m", "bootstrap"], vault_dir)
        push1 = git(["push", "vault-backup", "main"], vault_dir)
        report["bootstrap_push_rc"] = push1.returncode
        report["bare_refs_after_bootstrap"] = sorted(
            p.name for p in (bare / "refs" / "heads").iterdir()
        )

        # Seed a real file.
        (vault_dir / "note.md").write_text("hello from vault\n", encoding="utf-8")
        git(["add", "-A"], vault_dir)
        git(["commit", "-q", "-m", "add note"], vault_dir)
        push2 = git(["push", "vault-backup", "main"], vault_dir)
        report["note_push_rc"] = push2.returncode

        remote_log = subprocess.run(
            ["git", "-C", str(bare), "log", "--oneline", "main"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        report["remote_log"] = remote_log.stdout

        # Divergence scenario.
        laptop_a = base / "laptop_a"
        laptop_b = base / "laptop_b"
        subprocess.run(
            ["git", "clone", "-q", f"file://{bare}", str(laptop_a)],
            check=True,
            timeout=15,
        )
        subprocess.run(
            ["git", "clone", "-q", f"file://{bare}", str(laptop_b)],
            check=True,
            timeout=15,
        )
        (laptop_a / "from_a.md").write_text("a\n", encoding="utf-8")
        git(["add", "-A"], laptop_a)
        git(["commit", "-q", "-m", "A"], laptop_a)
        push_a = git(["push", "origin", "main"], laptop_a)
        report["push_a_rc"] = push_a.returncode

        (laptop_b / "from_b.md").write_text("b\n", encoding="utf-8")
        git(["add", "-A"], laptop_b)
        git(["commit", "-q", "-m", "B"], laptop_b)
        push_b = git(["push", "origin", "main"], laptop_b, check=False)
        report["push_b_rc"] = push_b.returncode
        report["push_b_stderr"] = push_b.stderr
        report["push_b_rejected_markers"] = {
            "rejected": "rejected" in push_b.stderr.lower(),
            "non_fast_forward": "non-fast-forward" in push_b.stderr.lower()
            or "fetch first" in push_b.stderr.lower(),
        }

    out_path = Path(__file__).with_name("spike_vault_bootstrap_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
