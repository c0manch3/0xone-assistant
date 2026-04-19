"""Phase-8 §C1: auto_commit_tz (zoneinfo) + auto_commit_cron validators.

Tests here construct :class:`GitHubSettings` directly so they do not
require TELEGRAM_BOT_TOKEN in the env (sub-model isolation).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.config import GitHubSettings

_GH_ENV_KEYS = (
    "GH_VAULT_REMOTE_URL",
    "GH_VAULT_SSH_KEY_PATH",
    "GH_VAULT_REMOTE_NAME",
    "GH_VAULT_BRANCH",
    "GH_AUTO_COMMIT_ENABLED",
    "GH_AUTO_COMMIT_CRON",
    "GH_AUTO_COMMIT_TZ",
    "GH_COMMIT_MESSAGE_TEMPLATE",
    "GH_COMMIT_AUTHOR_EMAIL",
    "GH_ALLOWED_REPOS",
)


@pytest.fixture(autouse=True)
def _scrub_gh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _GH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# auto_commit_tz (zoneinfo)


@pytest.mark.parametrize("tz", ["Europe/Moscow", "UTC", "Etc/GMT+3"])
def test_accepts_known_timezone(tz: str) -> None:
    gh = GitHubSettings(
        vault_remote_url="git@github.com:owner/repo.git",
        auto_commit_tz=tz,
    )
    assert gh.auto_commit_tz == tz


def test_rejects_unknown_timezone() -> None:
    with pytest.raises(ValidationError) as excinfo:
        GitHubSettings(
            vault_remote_url="git@github.com:owner/repo.git",
            auto_commit_tz="Mars/Phobos",
        )
    # Ensure the error cause chains back to zoneinfo (not just a generic
    # validator message).
    assert "Mars/Phobos" in str(excinfo.value)


# ---------------------------------------------------------------------------
# auto_commit_cron


def test_accepts_default_cron() -> None:
    gh = GitHubSettings(
        vault_remote_url="git@github.com:owner/repo.git",
        auto_commit_cron="0 3 * * *",
    )
    assert gh.auto_commit_cron == "0 3 * * *"


def test_rejects_invalid_cron() -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(
            vault_remote_url="git@github.com:owner/repo.git",
            auto_commit_cron="invalid cron",
        )


# ---------------------------------------------------------------------------
# allowed_repos parse — CSV ingress.
#
# The B-A1 regression lives primarily in the SSH URL test module, but we
# repeat the positive case here because the wave-plan §"Tests" line item
# explicitly asks for it in this file too.


def test_allowed_repos_csv_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_ALLOWED_REPOS", "a/b,c/d")
    gh = GitHubSettings()
    assert gh.allowed_repos == ("a/b", "c/d")


def test_allowed_repos_csv_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_ALLOWED_REPOS", " a/b , c/d ,, ")
    gh = GitHubSettings()
    assert gh.allowed_repos == ("a/b", "c/d")
