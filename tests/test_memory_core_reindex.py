"""Reindex + auto-reindex tests — Policy B (count + max_mtime_ns)."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from assistant.tools_sdk import _memory_core as core


def _mk_note(vault: Path, rel: str, title: str, body: str) -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\ntitle: {title}\ntags: []\n---\n\n{body}\n"
    )
    p.write_text(content, encoding="utf-8")
    return p


def test_reindex_empty_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    n = core.reindex_vault(vault, idx)
    assert n == 0
    conn = sqlite3.connect(idx)
    try:
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    finally:
        conn.close()


def test_reindex_seed_12_notes(seed_vault_copy: Path, tmp_path: Path) -> None:
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    n = core.reindex_vault(seed_vault_copy, idx)
    # Seed vault ships 12 .md files total; _*.md (4) are excluded.
    assert n == 8, f"expected 8 indexable seed notes (12 - 4 MOC), got {n}"


def test_reindex_excludes_obsidian_any_depth(tmp_path: Path) -> None:
    """M2.1: ``.obsidian`` anywhere in the rel path excludes the file."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _mk_note(vault, "real.md", "Real", "kept")
    _mk_note(vault, ".obsidian/config.md", "Config", "skipped")
    _mk_note(vault, "projects/.obsidian/nested.md", "Nested", "skipped")
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    n = core.reindex_vault(vault, idx)
    assert n == 1


def test_reindex_excludes_moc_underscore(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _mk_note(vault, "real.md", "Real", "kept")
    _mk_note(vault, "_index.md", "MOC", "skipped")
    _mk_note(vault, "projects/_overview.md", "Overview", "skipped")
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    n = core.reindex_vault(vault, idx)
    assert n == 1


def test_auto_reindex_count_mismatch_triggers(tmp_path: Path) -> None:
    """Disk has more notes than the index → reindex runs."""
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    # No notes indexed yet; add a note to disk.
    _mk_note(vault, "inbox/a.md", "A", "body a")
    core._maybe_auto_reindex(vault, idx)
    conn = sqlite3.connect(idx)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert cnt == 1


def test_memory_auto_reindex_obsidian_edit_detected(tmp_path: Path) -> None:
    """C2.4: count unchanged but max_mtime_ns bumps → reindex fires."""
    vault = tmp_path / "vault"
    vault.mkdir()
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    note = _mk_note(vault, "inbox/a.md", "A", "old body")
    # Initial reindex seeds max_mtime_ns.
    core._maybe_auto_reindex(vault, idx)
    # Verify it was indexed.
    conn = sqlite3.connect(idx)
    try:
        body = conn.execute(
            "SELECT body FROM notes WHERE path='inbox/a.md'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "old body" in body
    # Wait long enough for mtime to change on all filesystems.
    time.sleep(0.02)
    note.write_text(
        "---\ntitle: A\ntags: []\n---\n\nbrand new body text\n",
        encoding="utf-8",
    )
    # Bump mtime explicitly so coarse-resolution filesystems still
    # register a change.
    new_time = time.time() + 5
    os.utime(note, (new_time, new_time))
    core._maybe_auto_reindex(vault, idx)
    conn = sqlite3.connect(idx)
    try:
        body2 = conn.execute(
            "SELECT body FROM notes WHERE path='inbox/a.md'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "brand new body text" in body2


def test_auto_reindex_large_vault_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy: >_MAX_AUTO_REINDEX disk notes → skip + log warn."""
    import structlog

    vault = tmp_path / "vault"
    vault.mkdir()
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    monkeypatch.setattr(core, "_MAX_AUTO_REINDEX", 2)
    # Create 3 notes.
    for i in range(3):
        _mk_note(vault, f"inbox/note-{i}.md", f"N{i}", f"body {i}")
    monkeypatch.delenv("MEMORY_ALLOW_LARGE_REINDEX", raising=False)
    # Fix 11: ``_memory_core`` migrated to structlog. ``capture_logs``
    # is structlog's test-side recorder — stdlib ``caplog`` no longer
    # sees these events.
    with structlog.testing.capture_logs() as records:
        core._maybe_auto_reindex(vault, idx)
    # Count in index must still be 0 — skip fired.
    conn = sqlite3.connect(idx)
    try:
        assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0
    finally:
        conn.close()
    assert any(
        rec.get("event") == "memory_vault_too_large_for_auto_reindex"
        for rec in records
    )


def test_auto_reindex_lock_contention_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2.3: non-blocking lock contention on boot → warn + skip."""
    import structlog

    vault = tmp_path / "vault"
    vault.mkdir()
    idx = tmp_path / "idx.db"
    core._ensure_index(idx)
    _mk_note(vault, "inbox/a.md", "A", "body")

    # Monkeypatch vault_lock to always raise BlockingIOError when
    # blocking=False.
    from contextlib import contextmanager

    @contextmanager
    def always_blocked(lock_path, *, blocking=True, timeout=5.0):
        if not blocking:
            raise BlockingIOError("contended")
        yield

    monkeypatch.setattr(core, "vault_lock", always_blocked)
    with structlog.testing.capture_logs() as records:
        # Should not raise.
        core._maybe_auto_reindex(vault, idx)
    assert any(
        rec.get("event") == "memory_auto_reindex_skipped_lock_contention"
        for rec in records
    )
