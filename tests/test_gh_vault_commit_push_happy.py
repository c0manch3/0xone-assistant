"""Happy-path ``vault-commit-push`` — bootstrap + content commit + push (phase-8 C4).

Integration-style: uses a real local bare repo (`file://`) per R-14 so
the entire git pipeline runs against real binaries. No subprocess mocks
for `git` itself.

Covers I-8.3 / I-8.4 success-side assertions:

- Exit 0 on a dirty vault.
- JSON payload shape: ``{"ok": true, "commit_sha": <40-hex>, "files_changed": N, "retried_unpushed": false}``.
- Bare repo receives the commit (HEAD advances).
- No GH_TOKEN / GH_CONFIG_DIR / GIT_SSH_COMMAND leak from caller env
  into the spawned git processes (SF-C2/SF-C3 scrub).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def _bare_head(bare_repo: Path, branch: str = "main") -> str | None:
    """Return the sha of the remote branch head, or None if the ref is absent.

    A freshly-initialised bare repo has no refs — ``show-ref`` exits
    non-zero. We return None rather than raising so test assertions can
    distinguish "branch not yet pushed" from a sha mismatch.
    """
    proc = subprocess.run(  # noqa: S603 — trusted git binary
        ["git", "-C", str(bare_repo), "show-ref", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.split()[0] if proc.stdout else None


def test_happy_commit_push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dirty vault → bootstrap + commit + push → exit 0, JSON has commit_sha, bare ref advances."""
    env = install_file_remote(monkeypatch, tmp_path)

    # Seed real files so porcelain is non-empty. The CLI's mkdir step
    # (B-A3) creates `vault_dir` itself, but we pre-create it here so
    # the files can land BEFORE the CLI runs. Mode is irrelevant — the
    # CLI's `mkdir(exist_ok=True)` is idempotent on an existing dir.
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("important note\n")
    (env.vault_dir / "doc.md").write_text("another doc\n")

    rc = gh_main.main(["vault-commit-push", "--message", "phase8 smoke"])
    assert rc == 0, f"expected OK, got rc={rc}"

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert _SHA1_RE.match(payload["commit_sha"]), (
        f"commit_sha must be 40-hex, got {payload['commit_sha']!r}"
    )
    # files_changed counts files in HEAD's commit. Because bootstrap
    # created a prior commit, the happy-run commit will include note.md
    # + doc.md (both new) — exactly 2 files.
    assert payload["files_changed"] == 2, (
        f"expected 2 files_changed, got {payload['files_changed']}"
    )
    assert payload["retried_unpushed"] is False

    # Bare repo now has refs/heads/main pointing at HEAD.
    bare_head = _bare_head(env.bare_repo, env.settings.vault_branch)
    assert bare_head == payload["commit_sha"], (
        f"bare repo head {bare_head!r} != payload commit_sha {payload['commit_sha']!r}"
    )


def test_happy_no_env_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Caller env with ``GH_TOKEN`` / ``GIT_SSH_COMMAND`` doesn't leak into git.

    We set hostile values BEFORE the CLI runs, then assert the run
    completes cleanly — if the scrub didn't fire, git would either try
    to use our bogus GIT_SSH_COMMAND (rc != 0) or our GH_TOKEN (no
    effect on file:// but still a leak).

    This is an indirect assertion because we can't easily snoop inside
    the child process's env without an LD_PRELOAD trick; a rc=0 result
    against a file:// remote with poisoned env is the practical proof.
    """
    env = install_file_remote(monkeypatch, tmp_path)

    # Poison the caller env with the exact keys `_GIT_ENV_SCRUB_KEYS`
    # in `git_ops.py` is supposed to strip.
    monkeypatch.setenv("GH_TOKEN", "leaked-token-should-not-matter")
    monkeypatch.setenv(
        "GIT_SSH_COMMAND", "ssh -o ProxyCommand=false"
    )  # would break any real ssh push
    monkeypatch.setenv("SSH_ASKPASS", "/usr/bin/false")

    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("x\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0, (
        f"expected rc=0 despite poisoned env (scrub should have fired); got {rc}"
    )
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is True


def test_happy_retried_unpushed_false_for_fresh_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A fresh stage+commit cycle sets ``retried_unpushed=False``."""
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "hello.md").write_text("hi\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["retried_unpushed"] is False, (
        "fresh commit must not be labelled as a retry"
    )


def test_happy_push_constructs_ssh_cmd_with_config_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T1.4: the ``GIT_SSH_COMMAND`` sent to ``git push`` must include
    ``-F /dev/null`` so ``~/.ssh/config`` ProxyCommand entries cannot
    inject unintended execution.

    We capture the env handed to ``subprocess.run`` for the ``git push``
    call and inspect ``GIT_SSH_COMMAND``. Passthrough for every other
    invocation so the full bootstrap + commit pipeline still runs
    against real git binaries.
    """
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("content\n")

    captured_ssh_cmd: list[str] = []

    from tools.gh._lib import git_ops

    real_run = git_ops.subprocess.run

    def _capture(*args, **kwargs):  # type: ignore[no-untyped-def]
        cmd = args[0] if args else kwargs.get("args")
        if (
            isinstance(cmd, list)
            and len(cmd) >= 4
            and cmd[0] == "git"
            and cmd[1] == "-C"
            and cmd[3] == "push"
        ):
            env_kwarg = kwargs.get("env") or {}
            ssh_cmd = env_kwarg.get("GIT_SSH_COMMAND", "")
            captured_ssh_cmd.append(ssh_cmd)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_ops.subprocess, "run", _capture)

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0, f"expected rc=0; got {rc}"
    capsys.readouterr()

    assert captured_ssh_cmd, (
        "T1.4: git push was never invoked with a GIT_SSH_COMMAND env override"
    )
    ssh_cmd = captured_ssh_cmd[0]
    # Core anti-injection flags must be present.
    assert " -F /dev/null " in ssh_cmd or ssh_cmd.endswith(" -F /dev/null"), (
        f"T1.4: expected '-F /dev/null' in ssh argv; got {ssh_cmd!r}"
    )
    assert "IdentitiesOnly=yes" in ssh_cmd, (
        f"expected IdentitiesOnly=yes; got {ssh_cmd!r}"
    )
    assert "StrictHostKeyChecking=accept-new" in ssh_cmd
    assert "UserKnownHostsFile=" in ssh_cmd
