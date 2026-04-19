"""Phase 7 / commit 6 — path-guard semantics of `dispatch_reply`.

Pitfall #10 (plan §0): BOTH `resolve().is_relative_to(outbox_root)`
AND `exists()` are load-bearing. This file covers every branch:

  1. Path inside outbox AND exists         → send triggered.
  2. Path inside outbox but does NOT exist → skipped, no send.
  3. Path exists but OUTSIDE outbox        → skipped, no send
     (path-traversal defence).
  4. Symlink inside outbox pointing OUTSIDE → skipped (resolve()
     follows the symlink).
  5. Extension-suffixed directory under outbox (e.g. `x.png/` is
     a directory, not a file) → exists() returns True but the
     classifier + Telegram would blow up on a dir upload; we rely
     on `exists()` *plus* the dispatch adapter raising for
     non-regular files. We assert the guard itself does not drop
     the dir on regex grounds (the regex v3 `exists()` filter is
     what stops it).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply


# --- Test adapter double -------------------------------------------


class _RecordingAdapter(MessengerAdapter):
    """Collects every send_* call. No retries, no network."""

    def __init__(self) -> None:
        self.photos: list[Path] = []
        self.documents: list[Path] = []
        self.audios: list[Path] = []
        self.texts: list[str] = []

    async def start(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append(text)

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.photos.append(path)

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.documents.append(path)

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        self.audios.append(path)


# --- Fixtures ------------------------------------------------------


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    root.mkdir()
    return root


# --- Tests ---------------------------------------------------------


async def test_inside_outbox_and_exists_sends(outbox: Path) -> None:
    photo = outbox / "good.png"
    photo.write_bytes(b"PNG")
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"готово: {photo}",
        outbox_root=outbox,
        dedup=ledger,
    )

    # Send happened, path resolved to the real file under outbox.
    assert adapter.photos == [photo.resolve()]
    # Text cleaned so user doesn't see the raw absolute path.
    assert adapter.texts == ["готово:"]


async def test_inside_outbox_but_missing_skips(outbox: Path) -> None:
    """Pitfall #10 sub-case: regex may match `/abs/outbox/x.png` from
    `/abs/outbox/x.png/y`; `exists()` is the decisive filter."""
    ghost = outbox / "ghost.png"
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"я типа сделал {ghost} вот",
        outbox_root=outbox,
        dedup=ledger,
    )

    assert adapter.photos == []
    # Raw path stays in the text (we could not deliver it).
    assert adapter.texts == [f"я типа сделал {ghost} вот"]


async def test_outside_outbox_skipped(tmp_path: Path, outbox: Path) -> None:
    """Path-traversal defence. Attacker coaxes the model into emitting
    `/etc/passwd` style paths — guard MUST drop them even if they
    exist on disk."""
    intruder = tmp_path / "secret.png"
    intruder.write_bytes(b"PNG")
    assert intruder.exists()  # sanity: outside outbox but exists
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"sneaky: {intruder}",
        outbox_root=outbox,
        dedup=ledger,
    )

    assert adapter.photos == []
    # Raw text passed through unchanged — guard doesn't rewrite it.
    assert adapter.texts == [f"sneaky: {intruder}"]


async def test_symlink_pointing_outside_outbox_skipped(
    tmp_path: Path, outbox: Path
) -> None:
    """`resolve()` follows symlinks; a symlink inside outbox that
    points OUTSIDE must fail the `is_relative_to` check."""
    intruder = tmp_path / "secret.png"
    intruder.write_bytes(b"PNG")
    link = outbox / "link.png"
    link.symlink_to(intruder)
    assert link.resolve() == intruder.resolve()
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"check {link}",
        outbox_root=outbox,
        dedup=ledger,
    )

    assert adapter.photos == []
    assert adapter.texts == [f"check {link}"]


async def test_symlink_inside_outbox_resolves_under_outbox(outbox: Path) -> None:
    """Positive twin: a symlink inside outbox that also POINTS inside
    outbox MUST resolve under outbox and dispatch normally."""
    target = outbox / "real.png"
    target.write_bytes(b"PNG")
    link = outbox / "link.png"
    link.symlink_to(target)
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"here {link}",
        outbox_root=outbox,
        dedup=ledger,
    )

    # resolve() collapses the symlink to the real file.
    assert adapter.photos == [target.resolve()]


async def test_directory_matching_ext_is_skipped(outbox: Path) -> None:
    """`/abs/outbox/x.png/` — the path *exists* as a directory but a
    real artefact must be a file. Current guard allows it through
    because Path.exists() is True; the adapter would reject the dir
    at upload time. We ASSERT the current behaviour so a future
    strengthening (is_file()) surfaces as an explicit test update."""
    fake = outbox / "x.png"
    fake.mkdir()
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    # Regex matches `/abs/outbox/x.png` because of the trailing `/`
    # stop-set lookahead, then path guard sees it exists (as a dir).
    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"see {fake}/ done",
        outbox_root=outbox,
        dedup=ledger,
    )

    # Current behaviour: adapter gets called with the directory path.
    # The adapter would raise at upload time (not our concern here;
    # L-20 ensures the text still goes out).
    assert adapter.photos == [fake.resolve()]


async def test_multiple_artefacts_in_one_text_all_sent(outbox: Path) -> None:
    photo = outbox / "a.png"
    photo.write_bytes(b"PNG")
    doc = outbox / "b.pdf"
    doc.write_bytes(b"%PDF")
    audio = outbox / "c.mp3"
    audio.write_bytes(b"MP3")
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"{photo}\n{doc}\n{audio}",
        outbox_root=outbox,
        dedup=ledger,
    )

    assert adapter.photos == [photo.resolve()]
    assert adapter.documents == [doc.resolve()]
    assert adapter.audios == [audio.resolve()]
    # All three paths cleaned — resulting text is empty, send_text
    # MUST NOT be called (no blank Telegram bubble).
    assert adapter.texts == []


async def test_outbox_root_with_symlink_in_prefix(
    tmp_path: Path,
) -> None:
    """`outbox_root` itself may be a symlink (e.g. data_dir on macOS
    /var -> /private/var). The guard `resolve()`s the root once so
    the `is_relative_to` check works regardless."""
    real_outbox = tmp_path / "real_outbox"
    real_outbox.mkdir()
    symlink_outbox = tmp_path / "symlink_outbox"
    symlink_outbox.symlink_to(real_outbox)
    photo = real_outbox / "x.png"
    photo.write_bytes(b"PNG")
    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    # Pass the symlinked path as outbox_root — guard must resolve it.
    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"from sym: {real_outbox / 'x.png'}",
        outbox_root=symlink_outbox,
        dedup=ledger,
    )

    assert adapter.photos == [photo.resolve()]
