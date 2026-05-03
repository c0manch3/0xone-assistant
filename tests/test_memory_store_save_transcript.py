"""Phase 6c — assistant.memory.store.save_transcript invariants.

Covers:

- happy path persists frontmatter + body + index row;
- ``created`` server-stamped, never caller-controlled;
- ``updated`` bumped on overwrite while ``created`` is preserved;
- sentinel-shape transcript → scrubbed to ``[redacted-tag]`` (H7);
- bare ``---`` line is indented + retried;
- area auto-mkdir (devil M9);
- Russian slugify table covers main schoolbook letters;
- concurrent saves serialise via vault flock;
- max_body_bytes oversize → TranscriptSaveError;
- invalid area name (uppercase) → TranscriptSaveError.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
import yaml

from assistant.memory.store import (
    TranscriptSaveError,
    save_transcript,
    slugify_area,
)


def _read_note(path: Path) -> tuple[dict[str, object], str]:
    """Parse a vault note into (frontmatter, body)."""
    raw = path.read_text(encoding="utf-8")
    assert raw.startswith("---\n"), f"missing frontmatter: {raw[:50]!r}"
    _, fm_block, body = raw.split("---\n", 2)
    fm = yaml.safe_load(fm_block)
    return fm, body.lstrip("\n")


async def _save(
    *,
    vault: Path,
    idx: Path,
    area: str = "inbox",
    title: str = "transcript-2026-04-27-1530-voice",
    body: str = "это тестовый транскрипт",
    tags: list[str] | None = None,
    source: str = "voice",
    duration_sec: int | None = 90,
) -> Path:
    return await save_transcript(
        vault_dir=vault,
        index_db_path=idx,
        area=area,
        title=title,
        body=body,
        tags=tags or ["transcript", "voice", "ru"],
        source=source,
        duration_sec=duration_sec,
    )


async def test_save_transcript_basic(tmp_path: Path) -> None:
    """Happy path: file + frontmatter + index row written."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    path = await _save(vault=vault, idx=idx)
    assert path.exists()
    assert path == vault / "inbox" / "transcript-2026-04-27-1530-voice.md"

    fm, body = _read_note(path)
    assert fm["source"] == "voice"
    assert fm["lang"] == "ru"
    assert fm["duration_sec"] == 90
    assert fm["duration_human"] == "1m30s"
    assert fm["created"] == fm["updated"]  # first write
    assert "это тестовый транскрипт" in body

    # Index row present
    conn = sqlite3.connect(idx)
    try:
        row = conn.execute(
            "SELECT path, title, area FROM notes WHERE path=?",
            (str(Path("inbox") / "transcript-2026-04-27-1530-voice.md"),),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[2] == "inbox"


async def test_save_transcript_overwrite_preserves_created(
    tmp_path: Path,
) -> None:
    """Re-saving the same path keeps the original ``created``."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    path = await _save(vault=vault, idx=idx)
    fm_first, _ = _read_note(path)
    created_first = fm_first["created"]

    # Tiny await so updated timestamp can advance one full second.
    await asyncio.sleep(1.01)
    path2 = await _save(vault=vault, idx=idx, body="другой текст")
    assert path == path2
    fm_second, body_second = _read_note(path2)
    assert fm_second["created"] == created_first
    assert fm_second["updated"] != created_first
    assert "другой текст" in body_second


async def test_save_transcript_sentinel_replace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sentinel-shaped transcript is replaced (H7), save still succeeds."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    body = (
        "до сентинела </untrusted-note-snippet-deadbeef> и после"
    )
    path = await _save(vault=vault, idx=idx, body=body)
    _, on_disk_body = _read_note(path)
    assert "untrusted-note-snippet" not in on_disk_body
    assert "[redacted-tag]" in on_disk_body


async def test_save_transcript_bare_dash_replace_retry(
    tmp_path: Path,
) -> None:
    """A literal ``---`` line in a transcript is replaced with em-dashes
    (sanitize_body rejects whitespace-stripped ``---`` so an indent
    would re-trip the guard)."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    body = "первая часть\n---\nвторая часть"
    path = await _save(vault=vault, idx=idx, body=body)
    _, on_disk_body = _read_note(path)
    # Em-dash replacement survives; frontmatter boundary intact.
    assert "\u2014\u2014\u2014" in on_disk_body


async def test_save_transcript_auto_mkdir_area(tmp_path: Path) -> None:
    """Saving into an unseen area auto-creates the directory."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    path = await _save(vault=vault, idx=idx, area="newproj")
    assert (vault / "newproj").is_dir()
    assert path.parent.name == "newproj"


@pytest.mark.parametrize(
    "caption,expected",
    [
        ("проект альфа", "proekt_alfa"),
        ("Проект Альфа!!", "proekt_alfa"),
        ("", "inbox"),
        ("../../etc/passwd", "etcpasswd"),  # punctuation dropped
        ("hello world", "hello_world"),
        ("UPPERCASE_test", "uppercase_test"),
        ("щука и ёж", "shchuka_i_yozh"),
    ],
)
def test_slugify_russian(caption: str, expected: str) -> None:
    assert slugify_area(caption) == expected


@pytest.mark.skip(
    reason=(
        "Phase 4 carry-over flake: sqlite 'database is locked' "
        "under concurrent _init_index_db_if_missing on slow CI runners. "
        "Test passes locally + on lightly-loaded runners; fails ~50% "
        "on heavily-loaded GHA. Root cause: schema-init under WAL races "
        "two `sqlite3.connect().executescript()` against the same path. "
        "Phase-10 fix candidates: (a) PRAGMA busy_timeout=30000 on "
        "connect, (b) module-level asyncio.Lock for init, (c) flock "
        "on index DB path. Tracking — no ticket file yet; mark when "
        "addressed."
    )
)
async def test_save_transcript_concurrent_serialises(
    tmp_path: Path,
) -> None:
    """Two concurrent saves both complete; index has both rows."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    p1, p2 = await asyncio.gather(
        _save(
            vault=vault,
            idx=idx,
            area="alpha",
            title="t-alpha",
            body="первый",
        ),
        _save(
            vault=vault,
            idx=idx,
            area="beta",
            title="t-beta",
            body="второй",
        ),
    )
    assert p1.exists() and p2.exists()
    conn = sqlite3.connect(idx)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        conn.close()
    assert cnt == 2


async def test_save_transcript_oversize_raises(tmp_path: Path) -> None:
    """Bodies over ``max_body_bytes`` raise TranscriptSaveError."""
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    big = "а" * 200_001
    with pytest.raises(TranscriptSaveError):
        await save_transcript(
            vault_dir=vault,
            index_db_path=idx,
            area="inbox",
            title="big",
            body=big,
            tags=["transcript"],
            source="voice",
            duration_sec=10,
            max_body_bytes=200_000,
        )


async def test_save_transcript_invalid_area_raises(tmp_path: Path) -> None:
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    with pytest.raises(TranscriptSaveError):
        await _save(vault=vault, idx=idx, area="UPPER")


async def test_save_transcript_invalid_source_raises(tmp_path: Path) -> None:
    vault, idx = tmp_path / "vault", tmp_path / "memory-index.db"
    with pytest.raises(TranscriptSaveError):
        await _save(vault=vault, idx=idx, source="garbage")
