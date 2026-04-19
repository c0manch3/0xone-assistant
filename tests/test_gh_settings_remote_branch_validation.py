"""T2.4 + S-6 validator matrix for GitHubSettings.

Covers argv-injection / control-char rejection at the config boundary
for the following fields:

- ``vault_remote_name`` (T2.4): ``git push <remote> <branch>`` would
  otherwise parse values like ``"--receive-pack=/bin/sh"`` or
  ``"-evil"`` as option flags.
- ``vault_branch`` (T2.4): similar argv injection, plus git ref rules
  (no ``..``, no leading ``-``, no ``.lock`` suffix).
- ``commit_message_template`` (S-6): reject templates that reference
  unknown ``{format}`` placeholders or otherwise fail at runtime.
- ``commit_author_email`` (S-6): reject control chars / newlines that
  would let an attacker inject extra fields into the commit object
  header.

All tests construct :class:`GitHubSettings` directly (sub-model
isolation — no TELEGRAM_BOT_TOKEN requirement) and scrub GH_* env
vars so a developer's ``.env`` cannot leak into the assertions.
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
# T2.4 — vault_remote_name

_VALID_URL = "git@github.com:owner/repo.git"


@pytest.mark.parametrize(
    "name",
    ["origin", "vault-backup", "backup_2024", "a", "A" * 64],
)
def test_vault_remote_name_accepts_sensible_values(name: str) -> None:
    gh = GitHubSettings(vault_remote_url=_VALID_URL, vault_remote_name=name)
    assert gh.vault_remote_name == name


@pytest.mark.parametrize(
    "name",
    [
        "--receive-pack=/bin/sh",
        "-evil",
        "bad name",          # space
        "bad/name",          # slash — disallowed for remote names
        "bad.name",          # dot — disallowed
        "a" * 65,            # exceeds 64-char cap
        "",                  # empty
        "name\n",            # newline
        "name;rm",           # shell metachar
        "name$var",
    ],
)
def test_vault_remote_name_rejects_bad_values(name: str) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url=_VALID_URL, vault_remote_name=name)


# ---------------------------------------------------------------------------
# T2.4 — vault_branch


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "master",
        "release/1.0",
        "feature/foo-bar",
        "user.vault",
        "v1.2.3",
        "a",
    ],
)
def test_vault_branch_accepts_sensible_values(branch: str) -> None:
    gh = GitHubSettings(vault_remote_url=_VALID_URL, vault_branch=branch)
    assert gh.vault_branch == branch


@pytest.mark.parametrize(
    "branch",
    [
        "-evil",                 # leading hyphen → argv injection
        "--evil",
        "a..b",                  # `..` forbidden by git ref rules
        "/main",                 # leading slash
        "main/",                 # trailing slash
        "main.lock",             # .lock suffix reserved
        "bad name",              # space
        "bad\nname",             # newline
        "",                      # empty
        "a" * 201,               # over 200 chars
        "name;rm",               # shell metachar
        "name with space",
    ],
)
def test_vault_branch_rejects_bad_values(branch: str) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(vault_remote_url=_VALID_URL, vault_branch=branch)


# ---------------------------------------------------------------------------
# S-6 — commit_message_template


def test_commit_message_template_accepts_default() -> None:
    gh = GitHubSettings(
        vault_remote_url=_VALID_URL,
        commit_message_template="vault sync {date}",
    )
    assert gh.commit_message_template == "vault sync {date}"


def test_commit_message_template_accepts_without_placeholders() -> None:
    """A template that never references ``{date}`` is still valid — it
    just produces a constant commit message."""
    gh = GitHubSettings(
        vault_remote_url=_VALID_URL,
        commit_message_template="static message",
    )
    assert gh.commit_message_template == "static message"


@pytest.mark.parametrize(
    "template",
    [
        "vault {unknown}",       # unknown placeholder
        "{user}",
        "{date} {nope}",
        "{",                     # malformed
        "bad {0} index",         # positional not supported with our kwarg
    ],
)
def test_commit_message_template_rejects_bad_values(template: str) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(
            vault_remote_url=_VALID_URL,
            commit_message_template=template,
        )


# ---------------------------------------------------------------------------
# S-6 — commit_author_email


@pytest.mark.parametrize(
    "email",
    [
        "vaultbot@localhost",
        "vaultbot+tag@example.com",
        "user@sub.domain.org",
    ],
)
def test_commit_author_email_accepts_sensible_values(email: str) -> None:
    gh = GitHubSettings(vault_remote_url=_VALID_URL, commit_author_email=email)
    assert gh.commit_author_email == email


@pytest.mark.parametrize(
    "email",
    [
        "foo\nevil@example.com",   # newline injection
        "foo\r@example.com",       # CR
        "foo\tbar@example.com",    # tab (control char)
        "foo\x00@example.com",     # NUL
        "foo\x01@example.com",     # SOH
    ],
)
def test_commit_author_email_rejects_control_chars(email: str) -> None:
    with pytest.raises(ValidationError):
        GitHubSettings(
            vault_remote_url=_VALID_URL, commit_author_email=email
        )
