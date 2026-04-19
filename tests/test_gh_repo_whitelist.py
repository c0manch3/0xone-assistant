"""Repo allow-list enforcement for ``tools/gh/main.py`` (phase-8 C3).

Covers I-8.5: any ``issue`` / ``pr`` / ``repo`` invocation with a
``--repo`` value NOT in ``GitHubSettings.allowed_repos`` must:

1. Exit with code ``6`` (``REPO_NOT_ALLOWED``).
2. Emit a JSON payload containing ``repo_not_allowed``.
3. NEVER invoke ``subprocess.run`` (defence-in-depth — we install a
   tripwire that fails the test if the handler shells out before the
   allow-list check).

B-A2 invariant: no TG tokens in env.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from tools.gh import main as gh_main
from tools.gh._lib import gh_ops
from tools.gh._lib import repo_allowlist


def _install_subprocess_tripwire(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Fail the test if ``subprocess.run`` fires anywhere in the handler path.

    The allow-list check must happen BEFORE any shell invocation; this
    tripwire turns a regression (e.g. someone reordering the handler
    to query gh first) into an immediate AssertionError.
    """

    def _boom(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(
            "subprocess.run must NOT be invoked when --repo is outside "
            "the allow-list; allow-list check must precede any shell call."
        )

    monkeypatch.setattr(gh_ops.subprocess, "run", _boom)
    # Also guard the ``which`` pre-check in case a future refactor
    # routes through ``gh_auth_status`` before the allow-list.
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _n: "/usr/bin/gh")


def test_issue_list_rejects_non_whitelisted_repo(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "allowed/repo")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")
    _install_subprocess_tripwire(monkeypatch)

    rc = gh_main.main(["issue", "list", "--repo", "evil/exfil"])

    assert rc == 6
    out = capsys.readouterr().out
    assert "repo_not_allowed" in out
    payload = json.loads(out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "repo_not_allowed"
    assert payload["repo"] == "evil/exfil"
    assert payload["allowed"] == ["allowed/repo"]


def test_pr_view_rejects_non_whitelisted_repo(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "a/b,c/d")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")
    _install_subprocess_tripwire(monkeypatch)

    rc = gh_main.main(["pr", "view", "1", "--repo", "e/f"])
    assert rc == 6
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] == "repo_not_allowed"
    assert set(payload["allowed"]) == {"a/b", "c/d"}


def test_repo_view_rejects_non_whitelisted_repo(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/trusted")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "")
    _install_subprocess_tripwire(monkeypatch)

    rc = gh_main.main(["repo", "view", "--repo", "owner/untrusted"])
    assert rc == 6
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] == "repo_not_allowed"
    assert payload["repo"] == "owner/untrusted"


def test_extract_owner_repo_from_ssh_url_valid() -> None:
    """``extract_owner_repo_from_ssh_url`` rountrips the canonical shape."""
    assert (
        repo_allowlist.extract_owner_repo_from_ssh_url(
            "git@github.com:octocat/hello-world.git"
        )
        == "octocat/hello-world"
    )


def test_extract_owner_repo_from_ssh_url_rejects_https() -> None:
    """HTTPS URLs, bare slugs, and typo-squatted hosts all raise."""
    for bad in (
        "https://github.com/o/r.git",
        "git@gitlab.com:o/r.git",
        "git@github.com:o/r",  # missing .git
        "o/r",
        "",
    ):
        try:
            repo_allowlist.extract_owner_repo_from_ssh_url(bad)
        except ValueError:
            continue
        else:  # pragma: no cover — reached only on regression
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_is_repo_allowed_exact_match_only() -> None:
    allowed = ("a/b", "c/d")
    assert repo_allowlist.is_repo_allowed("a/b", allowed) is True
    assert repo_allowlist.is_repo_allowed("c/d", allowed) is True
    # Case-sensitive — GitHub slugs are case-sensitive.
    assert repo_allowlist.is_repo_allowed("A/B", allowed) is False
    # Substring / prefix shouldn't count.
    assert repo_allowlist.is_repo_allowed("a/bb", allowed) is False
    # Malformed slug → False regardless of membership.
    assert repo_allowlist.is_repo_allowed("not a slug", allowed) is False
    assert repo_allowlist.is_repo_allowed("", allowed) is False
