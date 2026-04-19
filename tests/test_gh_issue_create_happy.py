"""Happy path for ``tools/gh/main.py issue create`` (phase-8 C3).

Covers:

- Allow-list accepts a whitelisted repo (``GH_ALLOWED_REPOS=owner/repo``)
  and the handler shells out to ``gh`` (mocked).
- ``run_gh_json`` parses the canned ``{"url": ..., "number": 42}`` stdout
  into the handler's JSON passthrough.
- Exit code ``0`` + stdout contains ``"ok": true`` + ``"number": 42``.

B-A2 invariant: no ``TELEGRAM_BOT_TOKEN`` / ``OWNER_CHAT_ID`` are set —
the CLI must construct ``GitHubSettings`` directly and succeed.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from tools.gh import main as gh_main
from tools.gh._lib import gh_ops


def test_issue_create_happy(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    # B-A2: scrub TG tokens so we exercise the direct-instantiation path.
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    # ``vault_remote_url`` is optional for C3 (read-only commands), but
    # the Q4 auto-disable model validator still fires — setting it here
    # keeps the test explicit about which envs are material.
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "git@github.com:owner/vault.git")

    fake_stdout = json.dumps(
        {"url": "https://github.com/owner/repo/issues/42", "number": 42}
    )

    captured_argv: list[list[str]] = []

    def _fake_run(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        # Capture the argv so we can assert allow-list fired BEFORE
        # shelling out and that ``gh issue create`` is the intent.
        captured_argv.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=fake_stdout, stderr=""
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run)
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")

    rc = gh_main.main(
        [
            "issue", "create",
            "--repo", "owner/repo",
            "--title", "bug",
            "--body", "test",
        ]
    )

    assert rc == 0, f"handler returned non-zero rc: {rc}"

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["number"] == 42
    assert payload["url"] == "https://github.com/owner/repo/issues/42"

    # Exactly one subprocess call, and it was ``gh issue create`` — sanity
    # that we didn't accidentally hit ``gh auth status`` first or
    # double-dispatch.
    assert len(captured_argv) == 1, (
        f"expected exactly 1 subprocess call, got {len(captured_argv)}: "
        f"{captured_argv!r}"
    )
    argv = captured_argv[0]
    assert argv[0] == "gh"
    assert argv[1:3] == ["issue", "create"]
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "owner/repo"
    assert "--title" in argv and argv[argv.index("--title") + 1] == "bug"
    assert "--body" in argv and argv[argv.index("--body") + 1] == "test"


def test_issue_create_with_labels(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Labels are forwarded as repeated ``--label`` flags (argparse append)."""

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "git@github.com:owner/vault.git")

    captured_argv: list[list[str]] = []

    def _fake_run(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_argv.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"url": "x", "number": 7}),
            stderr="",
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run)
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")

    rc = gh_main.main(
        [
            "issue", "create",
            "--repo", "owner/repo",
            "--title", "x",
            "--body", "y",
            "--label", "bug",
            "--label", "p0",
        ]
    )
    assert rc == 0
    capsys.readouterr()  # drain

    argv = captured_argv[0]
    # argparse append means both labels come through in the handler's gh_args.
    label_positions = [i for i, a in enumerate(argv) if a == "--label"]
    assert len(label_positions) == 2
    labels = {argv[i + 1] for i in label_positions}
    assert labels == {"bug", "p0"}
