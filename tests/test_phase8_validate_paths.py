"""Phase 8 W2-H4 — secret denylist regex semantics.

AC#19 / AC#10: the daemon-side ``_validate_staged_paths`` and the
bootstrap script ``deploy/scripts/vault-bootstrap.sh`` MUST agree on
exactly which paths are rejected. This file tests the daemon-side
helper directly; the script's regex is mirrored verbatim from the
single source of truth in ``VaultSyncSettings.secret_denylist_regex``.

Regex anchors (post-fix-pack F12 default set — non-anchored for parity
with the .gitignore RECURSIVE patterns):
  - ``(?:^|/)secrets/``  — ``secrets/`` directory anywhere on path.
  - ``(?:^|/)\\.aws/``    — ``.aws/`` directory anywhere on path.
  - ``(?:^|/)\\.config/0xone-assistant/`` — owner config dir anywhere.
  - ``\\.env$``           — file ending in ``.env``.
  - ``\\.key$``           — file ending in ``.key``.
  - ``\\.pem$``           — file ending in ``.pem``.

Fix-pack F12 (devops + 4-reviewer convergent): the daemon regex is
non-anchored so ``notes/.aws/credentials`` (recursively excluded by
``.gitignore``) ALSO trips the daemon if the file were force-added.
This restores defense-in-depth parity with the .gitignore — no
forced-staged path can sneak past the daemon while passing the
gitignore check.
"""

from __future__ import annotations

import pytest

from assistant.config import VaultSyncSettings
from assistant.vault_sync._validate_paths import validate_no_secrets

DEFAULT_REGEX = VaultSyncSettings().secret_denylist_regex


@pytest.mark.parametrize(
    "path",
    [
        "secrets/foo.md",
        "secrets/api.env",
        ".aws/credentials",
        ".config/0xone-assistant/abc.md",
        "notes/foo.env",
        "any/path/down/x.key",
        "deep/dir/cert.pem",
        ".env",
        "config.env",
    ],
)
def test_denylist_blocks_secret_pattern(path: str) -> None:
    """All representative denylist hits must be rejected."""
    matches = validate_no_secrets([path], DEFAULT_REGEX)
    assert matches == [path]


def test_nested_config_path_matched_by_recursive_regex() -> None:
    """F12 — post-fix-pack ``(?:^|/)\\.config/0xone-assistant/``
    matches recursively, so ``notes/.config/0xone-assistant/setup.md``
    IS rejected. This restores defense-in-depth parity with the
    ``.gitignore`` ``recursive`` pattern set; the gitignore would
    exclude this path so a force-added path matching gitignore now
    also trips the daemon. Closes a force-add bypass (W3-CRIT-3
    residual)."""
    matches = validate_no_secrets(
        ["notes/.config/0xone-assistant/setup.md"], DEFAULT_REGEX
    )
    assert matches == ["notes/.config/0xone-assistant/setup.md"]


def test_nested_secrets_dir_matched_by_recursive_regex() -> None:
    """F12 — ``notes/secrets/api.env`` (nested ``secrets/``) IS
    matched by ``(?:^|/)secrets/`` so a hostile vault writer can't
    bypass the daemon by burying secrets in a subdirectory."""
    matches = validate_no_secrets(
        ["notes/secrets/api.env"], DEFAULT_REGEX
    )
    assert matches == ["notes/secrets/api.env"]


def test_nested_aws_dir_matched_by_recursive_regex() -> None:
    """F12 — ``project/x/.aws/credentials`` IS matched by
    ``(?:^|/)\\.aws/`` so AWS creds buried in any subdir are
    caught."""
    matches = validate_no_secrets(
        ["project/x/.aws/credentials"], DEFAULT_REGEX
    )
    assert matches == ["project/x/.aws/credentials"]


@pytest.mark.parametrize(
    "path",
    [
        "notes/2026-04-28-meeting.md",
        "projects/alpha/design.md",
        "people/spouse.md",
        "inbox/random.md",
    ],
)
def test_clean_paths_pass(path: str) -> None:
    """Vanilla vault notes do not trip the denylist."""
    matches = validate_no_secrets([path], DEFAULT_REGEX)
    assert matches == []


def test_empty_input_returns_empty() -> None:
    """No staged paths → no matches (no false positives, no errors)."""
    matches = validate_no_secrets([], DEFAULT_REGEX)
    assert matches == []


def test_multiple_secret_paths_all_returned() -> None:
    """When several paths match, they are all returned in order."""
    inputs = [
        "secrets/api.env",
        "notes/foo.md",  # clean
        ".aws/credentials",
        "deep/x.pem",
    ]
    matches = validate_no_secrets(inputs, DEFAULT_REGEX)
    assert matches == [
        "secrets/api.env",
        ".aws/credentials",
        "deep/x.pem",
    ]


def test_first_match_wins_no_double_counting() -> None:
    """A path matching multiple regexes is returned exactly once."""
    # ``secrets/api.env`` matches both ``^secrets/`` AND ``\.env$``.
    inputs = ["secrets/api.env"]
    matches = validate_no_secrets(inputs, DEFAULT_REGEX)
    assert matches == ["secrets/api.env"]


def test_custom_pattern_set() -> None:
    """The helper accepts an arbitrary tuple — tests can parametrise
    without touching the settings global."""
    matches = validate_no_secrets(
        ["foo.bar", "baz.bar", "ok.txt"],
        (r"\.bar$",),
    )
    assert matches == ["foo.bar", "baz.bar"]
