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

    Fix-pack F6 (UX): ``manual_tool_enabled`` default is True per
    spec §3 table. The "is the @tool actually visible" gate is the
    computed ``effective_manual_tool_enabled`` property — it returns
    False when ``enabled=False`` regardless of ``manual_tool_enabled``,
    so the model never sees the tool on a fresh checkout.

    Fix-pack F4: ``git_op_timeout_s`` lowered 30→10s; vault_lock
    budget bumped to 60s (= 4 * git_op_timeout_s + slack).
    """
    s = VaultSyncSettings()
    assert s.enabled is False
    assert s.manual_tool_enabled is True
    assert s.effective_manual_tool_enabled is False  # False on default
    assert s.cron_interval_s == 3600.0
    assert s.drain_timeout_s == 60.0
    assert s.push_timeout_s == 60
    assert s.git_op_timeout_s == 10
    assert s.vault_lock_acquire_timeout_s == 60.0
    assert s.first_tick_delay_s == 60.0


def test_manual_tool_without_enabled_owner_set_rejected() -> None:
    """F6 — owner-set ``manual_tool_enabled=True`` + ``enabled=False``
    is logically inconsistent and is REJECTED. The validator
    distinguishes owner-set vs default via ``model_fields_set``;
    silent-default-True does not trip (covered by
    ``test_default_construction_is_valid``).
    """
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(enabled=False, manual_tool_enabled=True)
    assert "manual_tool_enabled=True requires enabled=True" in str(ei.value)


def test_manual_tool_default_with_enabled_false_does_not_raise() -> None:
    """F6 (UX) — owner who keeps ``manual_tool_enabled`` at the
    default value can set ``VAULT_SYNC_ENABLED=false`` without the
    validator slamming. ``effective_manual_tool_enabled`` is False so
    the @tool stays invisible regardless."""
    # Default ``manual_tool_enabled`` is True — but it's not in
    # ``model_fields_set`` because we didn't pass it, so the validator
    # is lenient.
    s = VaultSyncSettings(enabled=False)
    assert s.manual_tool_enabled is True
    assert s.effective_manual_tool_enabled is False


def test_effective_manual_tool_property_truth_table() -> None:
    """F1 (AC#5 closure): ``effective_manual_tool_enabled`` is the
    AND of the two flags. The bridge gate uses this property
    exclusively so the @tool never leaks when the subsystem itself
    is disabled.
    """
    # enabled=False → always False (regardless of manual_tool_enabled).
    s1 = VaultSyncSettings()
    assert s1.effective_manual_tool_enabled is False
    s2 = VaultSyncSettings(enabled=False, manual_tool_enabled=False)
    assert s2.effective_manual_tool_enabled is False
    # enabled=True + manual_tool_enabled=True → True.
    s3 = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
    )
    assert s3.effective_manual_tool_enabled is True
    # enabled=True + manual_tool_enabled=False → False.
    s4 = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=False,
    )
    assert s4.effective_manual_tool_enabled is False


def test_vault_lock_budget_validator() -> None:
    """F4 — ``vault_lock_acquire_timeout_s`` must cover at least one
    full 4-step git pipeline at the worst-case ``git_op_timeout_s``
    ceiling, otherwise a parallel ``memory_write`` racing the cron
    tick can blow the vault_lock timeout while the pipeline is still
    legitimately running.
    """
    # Default: 60s vault_lock vs 4 * 10s git_op = 40s budget — passes.
    VaultSyncSettings()
    # 30s vault_lock + 10s git_op_timeout_s = 40s required → reject.
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(
            enabled=True,
            repo_url="git@github.com:c0manch3/0xone-vault.git",
            vault_lock_acquire_timeout_s=30.0,
            git_op_timeout_s=10,
        )
    assert "vault_lock_acquire_timeout_s" in str(ei.value)
    # 40s vault_lock + 10s git_op_timeout_s = 40s required → boundary
    # passes (the validator uses ``>=``).
    VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        vault_lock_acquire_timeout_s=40.0,
        git_op_timeout_s=10,
    )


def test_drain_lt_push_rejected() -> None:
    """W2-M3 — ``drain_timeout_s`` must be ``>= push_timeout_s`` so a
    slow-but-healthy push always finishes within the drain budget.

    Pre-set ``vault_lock_acquire_timeout_s`` to a value compatible
    with the F4 validator so the test exercises the
    drain-vs-push invariant in isolation.
    """
    with pytest.raises(ValidationError) as ei:
        VaultSyncSettings(
            enabled=True,
            repo_url="git@github.com:c0manch3/0xone-vault.git",
            push_timeout_s=60,
            drain_timeout_s=30,
            git_op_timeout_s=10,
            vault_lock_acquire_timeout_s=60.0,
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
