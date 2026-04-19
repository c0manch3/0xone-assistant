"""Test helpers for phase-8 ``vault-commit-push`` integration tests.

These tests need a real ``file://`` git remote (local bare repo) to avoid
pulling ssh into the test environment. But ``GitHubSettings`` validators
reject non-``git@github.com:OWNER/REPO.git`` URLs at load time, and the
``repo_allowlist`` module's ``extract_owner_repo_from_ssh_url`` raises
on the same shape. Rather than loosening production code for test
convenience, we inject a stub settings object via :func:`monkeypatch`
and neutralise the allow-list call path.

Exposed helper: :func:`install_file_remote` — configures env vars, creates
the bare repo, writes a dummy ssh key, and patches the CLI module so
``_cmd_vault_commit_push`` sees a pydantic-like object carrying the
``file://`` URL directly.

Kept under ``tests/_helpers`` so the pattern is a shared contract rather
than copy-pasted into each test.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools.gh import main as gh_main
from tools.gh._lib import repo_allowlist


@dataclass
class _FakeGitHubSettings:
    """Duck-typed stand-in for :class:`assistant.config.GitHubSettings`.

    Carries the exact attribute surface consumed by
    ``_cmd_vault_commit_push`` + ``_do_push_cycle``. A real pydantic
    model would validate the ``file://`` URL away; the fake skips
    validation intentionally. All fields are required so a typo in a
    test surfaces as ``AttributeError`` rather than a silent default.
    """

    vault_remote_url: str
    vault_ssh_key_path: Path
    vault_remote_name: str
    vault_branch: str
    auto_commit_enabled: bool
    auto_commit_cron: str
    auto_commit_tz: str
    commit_message_template: str
    commit_author_email: str
    allowed_repos: tuple[str, ...]


@dataclass
class VaultTestEnv:
    """Bundle of test-only paths returned by :func:`install_file_remote`."""

    data_dir: Path
    vault_dir: Path
    bare_repo: Path
    remote_url: str
    ssh_key: Path
    settings: _FakeGitHubSettings


def _init_bare_repo(path: Path) -> None:
    """Create an empty bare repo at ``path``.

    We shell out to ``git init --bare`` rather than using a Python wrapper
    to keep the stack exactly matching what the production code sees —
    a plain file-system bare repo, no unusual layout.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 — git is trusted
        ["git", "init", "--bare", "-q", str(path)],
        check=True,
        capture_output=True,
    )


def install_file_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    allowed: bool = True,
    branch: str = "main",
) -> VaultTestEnv:
    """Set up a ``file://`` bare repo + patched settings for an integration test.

    Side effects on the monkeypatch instance:

    - Scrubs ``TELEGRAM_BOT_TOKEN`` / ``OWNER_CHAT_ID`` so
      ``GitHubSettings()`` isn't even instantiated (the production path
      still works without them, we just belt-and-suspenders this).
    - Sets ``ASSISTANT_DATA_DIR`` + ``MEMORY_VAULT_DIR`` under ``tmp_path``.
    - Replaces ``assistant.config.GitHubSettings`` (as imported by the
      CLI's lazy import path) with a factory returning
      :class:`_FakeGitHubSettings` so validators don't reject the
      ``file://`` URL.
    - Replaces ``repo_allowlist.extract_owner_repo_from_ssh_url`` and
      ``repo_allowlist.is_repo_allowed`` with passthroughs keyed on the
      bare-repo UUID slug. When ``allowed=False`` the allowlist rejects
      so tests can exercise the exit-6 path with a file remote.

    Returns :class:`VaultTestEnv` holding every path the test might want
    to assert on (post-push bare-repo state, pre-commit vault state, etc).
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)

    data_dir = tmp_path / "data"
    vault_dir = data_dir / "vault"
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(vault_dir))

    # Unique bare repo per test so parallel xdist workers never collide.
    bare = tmp_path / f"bare-{uuid.uuid4().hex[:8]}.git"
    _init_bare_repo(bare)
    remote_url = f"file://{bare}"

    # Dummy key — never actually used (file:// bypasses ssh) but the
    # readability check still runs. 0o600 so the "permissive_mode"
    # stderr warning doesn't leak into assertions.
    ssh_key = tmp_path / "id_vault"
    ssh_key.write_text("fake-key\n")
    ssh_key.chmod(0o600)

    # Slug is opaque — the fake repo_allowlist uses it as a token only.
    slug = "test-owner/test-vault"
    settings = _FakeGitHubSettings(
        vault_remote_url=remote_url,
        vault_ssh_key_path=ssh_key,
        vault_remote_name="vault-backup",
        vault_branch=branch,
        auto_commit_enabled=True,
        auto_commit_cron="0 3 * * *",
        auto_commit_tz="Europe/Moscow",
        commit_message_template="vault sync {date}",
        commit_author_email="vaultbot@localhost",
        allowed_repos=(slug,) if allowed else (),
    )

    # We patch inside `assistant.config` so the `_cmd_vault_commit_push`
    # lazy import `from assistant.config import GitHubSettings` picks up
    # our fake. The attribute is callable (the handler calls
    # `GitHubSettings()`), so the fake is a factory returning the stub.
    import assistant.config as assistant_config

    def _fake_ctor() -> _FakeGitHubSettings:
        return settings

    monkeypatch.setattr(assistant_config, "GitHubSettings", _fake_ctor)

    # repo_allowlist.extract: short-circuit to our known slug whenever
    # the URL matches the `file://` prefix we configured. Non-matching
    # URLs fall through to the production function so unrelated tests
    # keep their original behaviour.
    real_extract = repo_allowlist.extract_owner_repo_from_ssh_url

    def _fake_extract(url: str) -> str:
        if url == remote_url:
            return slug
        return real_extract(url)

    monkeypatch.setattr(
        repo_allowlist, "extract_owner_repo_from_ssh_url", _fake_extract
    )

    # is_repo_allowed: the production function rejects non-slug shapes
    # via regex; our slug IS a valid shape, so no patch needed for the
    # allowed path. But if the test wants `allowed=False`, we still let
    # the real function run (the allowed tuple is empty → returns False).

    # Cross-module monkeypatch: the CLI grabs `GitHubSettings` via
    # `from assistant.config import GitHubSettings` INSIDE the handler,
    # so patching `assistant.config.GitHubSettings` is sufficient (the
    # import runs AFTER monkeypatch). No need to also patch `gh_main`.

    _ = gh_main  # keep import referenced for import-time side effects

    return VaultTestEnv(
        data_dir=data_dir,
        vault_dir=vault_dir,
        bare_repo=bare,
        remote_url=remote_url,
        ssh_key=ssh_key,
        settings=settings,
    )


__all__ = ["VaultTestEnv", "install_file_remote"]
