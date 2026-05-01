"""Phase 8 §3 — VaultSyncSettings pydantic v2 model_validator tests.

Covers the six AC#22-class scenarios:

- ``manual_tool_enabled=True`` + ``enabled=False`` → reject.
- ``drain_timeout_s < push_timeout_s`` → reject.
- ``enabled=True`` + ``repo_url=None`` → reject.
- malformed ``repo_url`` regex → reject.
- valid happy-path config passes.
- defaults (``enabled=False``) construct cleanly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.config import VaultSyncSettings


def test_default_construction_is_valid() -> None:
    """Defaults must be self-consistent — fresh checkout with no env
    overrides must NOT trip the validator (otherwise the daemon would
    refuse to boot on every clean clone).
    """
    s = VaultSyncSettings()
    assert s.enabled is False
    assert s.manual_tool_enabled is False
    assert s.cron_interval_s == 3600.0
    assert s.drain_timeout_s == 60.0
    assert s.push_timeout_s == 60


def test_manual_tool_without_enabled_rejected() -> None:
    """W2-C3 — ``manual_tool_enabled=True`` without ``enabled=True``
    is logically inconsistent: the @tool would register but the
    subsystem itself would not be constructed.
    """
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(enabled=False, manual_tool_enabled=True)
    assert "manual_tool_enabled requires enabled=True" in str(ei.value)


def test_drain_lt_push_rejected() -> None:
    """W2-M3 — ``drain_timeout_s`` must be ``>= push_timeout_s`` so a
    slow-but-healthy push always finishes within the drain budget.
    """
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(
            enabled=True,
            repo_url="git@github.com:c0manch3/0xone-vault.git",
            push_timeout_s=60,
            drain_timeout_s=30,
        )
    assert "drain_timeout_s must be >= push_timeout_s" in str(ei.value)


def test_enabled_without_repo_url_rejected() -> None:
    """``enabled=True`` requires ``repo_url`` — otherwise the loop
    would crash on first push attempt.
    """
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(enabled=True, repo_url=None)
    assert "repo_url required when enabled=True" in str(ei.value)


def test_malformed_repo_url_rejected() -> None:
    """L-2 — repo_url must match the SSH form
    ``git@<host>:<owner>/<repo>.git``. https URLs and bare strings
    are rejected at load.
    """
    bad_urls = [
        "https://github.com/c0manch3/0xone-vault.git",
        "github.com/c0manch3/0xone-vault.git",
        "git@github.com/c0manch3/0xone-vault.git",  # missing colon
        "not-a-url",
        "git@github.com:c0manch3/0xone-vault",  # missing .git
    ]
    for bad in bad_urls:
        with pytest.raises(ValidationError) as ei:
            VaultSyncSettings(enabled=True, repo_url=bad)
        assert "repo_url must match SSH form" in str(ei.value), bad


def test_happy_path_valid_config() -> None:
    """All four fields aligned → construction succeeds."""
    s = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
    )
    assert s.enabled is True
    assert s.repo_url == "git@github.com:c0manch3/0xone-vault.git"
    assert s.manual_tool_enabled is True
