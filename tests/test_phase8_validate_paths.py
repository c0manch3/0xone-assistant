"""Phase 8 W2-H4 — secret denylist regex semantics.

AC#19 / AC#10: the daemon-side ``_validate_staged_paths`` and the
bootstrap script ``deploy/scripts/vault-bootstrap.sh`` MUST agree on
exactly which paths are rejected. This file tests the daemon-side
helper directly; the script's regex is mirrored verbatim from the
single source of truth in ``VaultSyncSettings.secret_denylist_regex``.

Regex anchors (default set):
  - ``^secrets/``        — top-level ``secrets/`` directory.
  - ``^\\.aws/``          — top-level ``.aws/`` directory.
  - ``^\\.config/0xone-assistant/`` — owner config dir at root.
  - ``\\.env$``           — file ending in ``.env``.
  - ``\\.key$``           — file ending in ``.key``.
  - ``\\.pem$``           — file ending in ``.pem``.

Note: the leading-anchor patterns are explicitly anchored at the
start of the path so a path like ``notes/.config/0xone-assistant/x``
is NOT matched (the secret detector targets repo-root layout, not
arbitrary nested paths). Anti-test below verifies this.
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


def test_nested_config_path_not_matched_by_anchored_regex() -> None:
    """``notes/.config/0xone-assistant/setup.md`` does NOT match
    ``^\\.config/0xone-assistant/`` because the regex is anchored at
    the start of the path. This is intentional: the detector targets
    repo-root paths only.
    """
    matches = validate_no_secrets(
        ["notes/.config/0xone-assistant/setup.md"], DEFAULT_REGEX
    )
    # Note: regex anchors block ``^\.config/...`` from matching but
    # the path also doesn't match any ``\.env$`` / ``\.key$`` /
    # ``\.pem$`` pattern, so it should NOT be rejected.
    assert matches == []


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
