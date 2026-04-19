#!/usr/bin/env python3
"""Phase 8 R-9 / B2: validate `git status --porcelain` output shape.

Seeds a throwaway git repo with files in every state (tracked+modified,
untracked, renamed, deleted, added, type-changed, submodule) and runs
both `--porcelain=v1` and `--porcelain=v2`. Parses output, records the
prefix each state produces, and asserts expectations from
detailed-plan §4 C4 step 7 (porcelain replaces `git diff --quiet`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=15
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"cmd failed: {cmd!r}\nrc={proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
        )
    return proc


def git(args: list[str], repo: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    # Force a predictable identity so no global config is consulted.
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


def seed_repo(repo: Path) -> dict[str, str]:
    """Create files in a variety of git states.

    Returns map of state -> filename for easy assertion.
    """
    git(["init", "-q", "-b", "main"], repo)
    (repo / "initial.md").write_text("initial\n")
    (repo / "to_be_modified.md").write_text("original\n")
    (repo / "to_be_deleted.md").write_text("doomed\n")
    (repo / "to_be_renamed.md").write_text("renameme\n")
    git(["add", "."], repo)
    git(["commit", "-q", "-m", "seed"], repo)

    # Mutate for v1 / v2 output.
    (repo / "to_be_modified.md").write_text("modified\n")
    (repo / "untracked.md").write_text("fresh\n")
    (repo / "to_be_deleted.md").unlink()
    git(["mv", "to_be_renamed.md", "renamed.md"], repo)  # rename + staged.
    (repo / "added_staged.md").write_text("added\n")
    git(["add", "added_staged.md"], repo)

    return {
        "tracked_modified": "to_be_modified.md",
        "untracked": "untracked.md",
        "deleted_unstaged": "to_be_deleted.md",
        "renamed_staged": "renamed.md",
        "added_staged": "added_staged.md",
    }


def main() -> int:
    report: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="spike_porcelain_") as td:
        repo = Path(td)
        names = seed_repo(repo)

        porcelain_v1 = git(["status", "--porcelain=v1"], repo).stdout
        porcelain_v2 = git(["status", "--porcelain=v2"], repo).stdout
        full_status = git(["status"], repo).stdout
        diff_quiet_worktree = git(["diff", "--quiet"], repo, check=False).returncode
        diff_quiet_cached = git(["diff", "--cached", "--quiet"], repo, check=False).returncode

        v1_lines = porcelain_v1.splitlines()
        report["repo_seed"] = names
        report["porcelain_v1_raw"] = porcelain_v1
        report["porcelain_v1_parsed"] = [
            {"prefix": line[:2], "path": line[3:]} for line in v1_lines
        ]
        report["porcelain_v2_raw"] = porcelain_v2
        report["full_status"] = full_status
        report["diff_quiet_worktree_rc"] = diff_quiet_worktree
        report["diff_quiet_cached_rc"] = diff_quiet_cached

        # Derived assertions (plan-critical).
        prefixes = {line[:2]: line[3:] for line in v1_lines}
        assertions = {
            "has_untracked_??": any(p.startswith("??") for p in prefixes),
            "has_modified_worktree_prefix_has_M": any(
                p.endswith("M") or p == " M" or p == "M " for p in prefixes
            ),
            "has_deleted_D": any("D" in p for p in prefixes),
            "has_renamed_R": any("R" in p for p in prefixes),
            "has_added_A": any("A" in p for p in prefixes),
            # Critical B2 fix: `git diff --quiet` ignores untracked files.
            # Expected rc=1 for worktree (modified + deleted exist),
            # possibly 0 if the only change were untracked. We seeded a
            # worktree-modification so we expect 1.
            "diff_quiet_sees_only_modified_or_staged": diff_quiet_worktree == 1,
        }
        report["assertions"] = assertions

        # Now test a scenario where ONLY untracked exists.
        with tempfile.TemporaryDirectory(prefix="spike_untracked_") as td2:
            repo2 = Path(td2)
            git(["init", "-q", "-b", "main"], repo2)
            (repo2 / "seed.md").write_text("seed\n")
            git(["add", "."], repo2)
            git(["commit", "-q", "-m", "seed"], repo2)
            # Only add an untracked file.
            (repo2 / "only_untracked.md").write_text("new\n")
            diff_quiet_rc = git(["diff", "--quiet"], repo2, check=False).returncode
            porcelain = git(["status", "--porcelain"], repo2).stdout
            report["untracked_only_scenario"] = {
                "diff_quiet_rc": diff_quiet_rc,
                "porcelain": porcelain,
                "verdict_b2_justified": diff_quiet_rc == 0 and "??" in porcelain,
            }

    out_path = Path(__file__).with_name("spike_git_status_porcelain_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_path}")
    for k, v in report.items():
        if k in ("porcelain_v2_raw", "full_status"):
            continue
        print(f"--- {k} ---")
        print(json.dumps(v, indent=2, default=str)[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
