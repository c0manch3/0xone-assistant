#!/usr/bin/env python3
"""Phase 8 R-7: verify `git commit --only -- <path>` semantics.

Setup:
  1. Fresh repo, two subdirs `dir_a/` and `dir_b/`.
  2. Each has a modified file staged via `git add .`.
  3. Run `git commit --only -- dir_a/`.

Assertions:
  * Commit contains ONLY dir_a changes (verified via git show --stat).
  * dir_b changes remain STAGED after the commit.
  * `git diff --cached --quiet -- dir_b/` returns 1 (still-staged).
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
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {args!r} failed: rc={proc.returncode}\nstderr={proc.stderr}"
        )
    return proc


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_commit_only_") as td:
        repo = Path(td)
        git(["init", "-q", "-b", "main"], repo)

        (repo / "dir_a").mkdir()
        (repo / "dir_b").mkdir()
        (repo / "dir_a" / "a.md").write_text("a1\n")
        (repo / "dir_b" / "b.md").write_text("b1\n")
        git(["add", "."], repo)
        git(["commit", "-q", "-m", "seed"], repo)

        # Modify both.
        (repo / "dir_a" / "a.md").write_text("a2\n")
        (repo / "dir_b" / "b.md").write_text("b2\n")
        git(["add", "."], repo)

        # Snapshot pre-commit.
        report["pre_commit_cached_all"] = git(
            ["diff", "--cached", "--name-only"], repo
        ).stdout

        # Commit only dir_a.
        res = git(
            ["commit", "-q", "-m", "only dir_a", "--only", "--", "dir_a/"], repo
        )
        report["commit_rc"] = res.returncode

        # Inspect the commit.
        report["show_stat"] = git(["show", "--stat", "HEAD"], repo).stdout
        report["show_name_only"] = git(
            ["show", "--name-only", "--pretty=format:"], repo
        ).stdout.strip()

        # Is dir_b still staged?
        rc_cached_b = git(
            ["diff", "--cached", "--quiet", "--", "dir_b/"], repo, check=False
        ).returncode
        report["diff_cached_dir_b_rc"] = rc_cached_b
        report["dir_b_still_staged"] = rc_cached_b == 1

        # What does `git status --porcelain` say?
        report["post_commit_porcelain"] = git(
            ["status", "--porcelain"], repo
        ).stdout

        # Final verdict.
        report["verdict"] = {
            "commit_contains_only_dir_a": (
                "dir_a/a.md" in report["show_name_only"]
                and "dir_b/b.md" not in report["show_name_only"]
            ),
            "dir_b_still_staged": report["dir_b_still_staged"],
        }

    out_path = Path(__file__).with_name("spike_git_commit_only_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
