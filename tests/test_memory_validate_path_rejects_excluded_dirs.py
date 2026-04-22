"""Fix 5 / QA M2 — ``validate_path`` rejects writes into the reindex's
excluded directories BEFORE they hit disk.

Writes to ``.tmp/…``, ``.obsidian/…``, ``.git/…`` etc. would succeed
on disk but never index — the reindex scan filters them out. Result:
``memory_search`` / ``memory_list`` never surface them, and the model
thinks its save went missing. Up-front rejection prevents the
"phantom note" class of bugs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.tools_sdk._memory_core import (
    _VAULT_SCAN_EXCLUDES,
    validate_path,
)


def test_validate_path_rejects_dot_tmp_first_segment(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="reserved/excluded"):
        validate_path(".tmp/evil.md", vault)


def test_validate_path_rejects_dot_obsidian_first_segment(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="reserved/excluded"):
        validate_path(".obsidian/cfg.md", vault)


def test_validate_path_rejects_every_scan_exclude_first_segment(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for excluded in _VAULT_SCAN_EXCLUDES:
        with pytest.raises(ValueError, match="reserved/excluded"):
            validate_path(f"{excluded}/foo.md", vault)


def test_validate_path_rejects_any_dotprefixed_first_segment(
    tmp_path: Path,
) -> None:
    """Even a first segment not in ``_VAULT_SCAN_EXCLUDES`` is rejected
    if it starts with ``.`` — dot-prefix is the POSIX "hidden" convention
    and indexers frequently skip it.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ValueError, match="reserved/excluded"):
        validate_path(".hidden/foo.md", vault)


def test_validate_path_allows_dot_in_later_segments(tmp_path: Path) -> None:
    """Only the FIRST component is checked. ``inbox/._hidden.md`` is
    allowed — the per-area MOC / dot-rejection logic is scoped to the
    path head, not file stems.
    """
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    out = validate_path("inbox/._hidden.md", vault)
    assert out == (vault / "inbox" / "._hidden.md").resolve()
