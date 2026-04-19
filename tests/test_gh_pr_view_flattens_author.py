"""SF-A5 regression: ``gh pr view`` flattens nested ``author`` JSON.

``gh`` natively returns the author field as ``{"login": "X", ...}`` but
downstream consumers (assistant, tests, model prompts) expect a flat
string. ``tools/gh/_lib/gh_ops._flatten_gh_json`` collapses the shape in
``run_gh_json`` so every ``tools/gh/main.py pr view`` response presents
``"author": "X"`` consistently.

This test pins the behaviour so a future refactor can't silently drop
the flatten step. We also verify the secondary shape
``{"defaultBranchRef": {"name": X}}`` → ``{"default_branch": X}`` on
``repo view``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from tools.gh import main as gh_main
from tools.gh._lib import gh_ops


def test_pr_view_flattens_author(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")

    fake_stdout = json.dumps(
        {
            "number": 15,
            "title": "feat: x",
            "body": "desc",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "author": {
                "login": "octocat",
                "url": "https://github.com/octocat",
                # Extra nested fields must be dropped; we only keep login.
                "type": "User",
            },
        }
    )

    def _fake_run(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=fake_stdout, stderr=""
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run)
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")

    rc = gh_main.main(["pr", "view", "15", "--repo", "owner/repo"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    # Core SF-A5 assertion: author is a flat string, not a dict.
    assert payload["author"] == "octocat", (
        f"expected flat author string, got {payload['author']!r}"
    )
    assert not isinstance(payload["author"], dict)
    # Other fields pass through verbatim.
    assert payload["number"] == 15
    assert payload["state"] == "OPEN"
    assert payload["mergeable"] == "MERGEABLE"


def test_pr_list_flattens_author_per_item(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """``gh pr list`` returns a JSON array; flatten must descend into items."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")

    fake_stdout = json.dumps(
        [
            {
                "number": 1,
                "title": "a",
                "state": "OPEN",
                "author": {"login": "alice"},
            },
            {
                "number": 2,
                "title": "b",
                "state": "CLOSED",
                "author": {"login": "bob"},
            },
        ]
    )

    def _fake_run(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=fake_stdout, stderr=""
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run)
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")

    rc = gh_main.main(["pr", "list", "--repo", "owner/repo"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    prs = payload["prs"]
    assert len(prs) == 2
    assert prs[0]["author"] == "alice"
    assert prs[1]["author"] == "bob"


def test_repo_view_flattens_default_branch(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Secondary flatten: ``{"defaultBranchRef": {"name": X}}`` → ``{"default_branch": X}``."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")

    fake_stdout = json.dumps(
        {
            "name": "repo",
            "description": "a test repo",
            "defaultBranchRef": {"name": "main"},
            "visibility": "PUBLIC",
        }
    )

    def _fake_run(
        cmd: list[str], *_args: Any, **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=fake_stdout, stderr=""
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _fake_run)
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")

    rc = gh_main.main(["repo", "view", "--repo", "owner/repo"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["default_branch"] == "main"
    # Original key is dropped after rename to snake_case.
    assert "defaultBranchRef" not in payload
    assert payload["name"] == "repo"
    assert payload["visibility"] == "PUBLIC"


def test_flatten_gh_json_preserves_primitive_payloads() -> None:
    """Pure-unit safety: non-dict / non-list inputs pass through verbatim."""
    assert gh_ops._flatten_gh_json("x") == "x"
    assert gh_ops._flatten_gh_json(42) == 42
    assert gh_ops._flatten_gh_json(None) is None
    assert gh_ops._flatten_gh_json([]) == []
    assert gh_ops._flatten_gh_json({}) == {}
    # author-looking key without nested login falls through unchanged.
    assert gh_ops._flatten_gh_json({"author": "already-flat"}) == {
        "author": "already-flat"
    }
    # Dict-shaped author without ``login`` key is preserved as-is.
    assert gh_ops._flatten_gh_json({"author": {"nickname": "foo"}}) == {
        "author": {"nickname": "foo"}
    }
