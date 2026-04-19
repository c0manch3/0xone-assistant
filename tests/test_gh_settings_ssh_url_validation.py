"""Phase-8 §C1: SSH URL + ssh key path + allowed_repos validation surface.

Tests here construct :class:`GitHubSettings` DIRECTLY (not through
:class:`Settings`) so they do not require TELEGRAM_BOT_TOKEN /
OWNER_CHAT_ID in the environment (CRITICAL B-A2 fundament — sub-model
isolation). Each test must also scrub the real ``GH_*`` env vars via
``monkeypatch.delenv`` + ``monkeypatch.setenv`` so a developer's user
``.env`` cannot leak values into the asserted defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from assistant.config import GitHubSettings

# The set of env vars GitHubSettings consumes via `env_prefix="GH_"`.
# Tests that touch any of these must first clear them so a real user
# `.env` cannot leak into the assertion.
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
    """Ensure no ambient GH_* env vars bleed into tests."""
    for key in _GH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# SSH URL regex — accept / reject matrix


def test_accepts_valid_ssh_url() -> None:
    gh = GitHubSettings(vault_remote_url="git@github.com:owner/repo.git")
    assert gh.vault_remote_url == "git@github.com:owner/repo.git"


def test_rejects_https_url() -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url="https://github.com/owner/repo.git")


def test_rejects_cyrillic_owner() -> None:
    # Contains U+043E (CYRILLIC SMALL LETTER O) in the owner segment.
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url="git@github.com:\u043ewner/repo.git")


def test_rejects_double_dot_in_owner() -> None:
    # SF-C1 owner regex excludes `.`, so this is caught by the regex match
    # step rather than the dangerous-dots branch — either way it raises.
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url="git@github.com:foo..bar/repo.git")


def test_empty_url_auto_disables_auto_commit(caplog: pytest.LogCaptureFixture) -> None:
    """Q4: empty vault_remote_url + auto_commit_enabled=True flips the flag
    to False WITHOUT raising ValidationError."""
    with caplog.at_level("WARNING", logger="assistant.config"):
        gh = GitHubSettings(vault_remote_url="", auto_commit_enabled=True)
    assert gh.vault_remote_url == ""
    assert gh.auto_commit_enabled is False
    # Warning logged so operators can see why a cron never fires.
    assert any("auto_commit_enabled" in rec.message for rec in caplog.records)


def test_allowed_repos_parsed_from_env_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-A1 regression: NoDecode annotation keeps pydantic-settings 2.13
    from JSON-decoding the comma-separated env var before our validator
    has a chance to split it."""
    monkeypatch.setenv("GH_ALLOWED_REPOS", "foo/bar,baz/qux")
    gh = GitHubSettings()
    assert gh.allowed_repos == ("foo/bar", "baz/qux")


# ---------------------------------------------------------------------------
# ssh key path — metacharacter + ` -o ` substring defence


def test_ssh_key_path_rejects_space() -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(
            vault_remote_url="git@github.com:owner/repo.git",
            vault_ssh_key_path=Path("/tmp/id vault"),
        )


def test_ssh_key_path_rejects_option_injection() -> None:
    # ` -o ` substring that tries to sneak an OpenSSH option through.
    with pytest.raises(ValidationError):
        GitHubSettings(
            vault_remote_url="git@github.com:owner/repo.git",
            vault_ssh_key_path=Path("/tmp/bad -o ProxyCommand=evil"),
        )


def test_ssh_key_path_accepts_unicode_without_metachars(tmp_path: Path) -> None:
    # SF-F3: unicode letters are fine as long as there is no shell
    # metacharacter. Use tmp_path so we have an ASCII-safe parent, then
    # point at a Cyrillic-named filename.
    key = tmp_path / "\u043a\u043b\u044e\u0447"  # "ключ"
    gh = GitHubSettings(
        vault_remote_url="git@github.com:owner/repo.git",
        vault_ssh_key_path=key,
    )
    assert gh.vault_ssh_key_path == key


# ---------------------------------------------------------------------------
# SF-C1 tightened owner rules


def test_rejects_owner_with_leading_hyphen() -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url="git@github.com:-bad/repo.git")


def test_rejects_owner_with_trailing_hyphen() -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url="git@github.com:bad-/repo.git")


def test_rejects_owner_exceeding_39_chars() -> None:
    # 40 `a` characters — longer than the 39-char GitHub owner cap.
    too_long_owner = "a" * 40
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url=f"git@github.com:{too_long_owner}/repo.git")


# ---------------------------------------------------------------------------
# SF-F3 tilde expansion via env


def test_env_tilde_expansion_in_ssh_key_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_VAULT_SSH_KEY_PATH", "~/.ssh/id_vault")
    gh = GitHubSettings()
    # After expansion the stored path must start with $HOME — never the
    # literal `~` character.
    assert str(gh.vault_ssh_key_path).startswith(str(Path.home()))
    assert "~" not in str(gh.vault_ssh_key_path)


# ---------------------------------------------------------------------------
# B-A1 empty-string regression


def test_allowed_repos_empty_env_is_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_ALLOWED_REPOS", "")
    gh = GitHubSettings()
    assert gh.allowed_repos == ()
