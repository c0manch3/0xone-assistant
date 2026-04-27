"""Phase 6b — MediaGroupAggregator unit tests.

Covers research.md RQ3 invariants:

- Single photo flushes after debounce (positive baseline).
- Burst arrival resets debounce — total flush count = 1.
- Bucket size cap (5) → overflow callback fires once + 6th tmp file
  unlinked.
- ``flush_for_chat`` drains pending bucket without waiting debounce.
- Multi-chat isolation: two chats interleaved, each flush gets only
  its own paths.
- Caption "first non-empty wins" (Telegram album order is not
  guaranteed).
- ``cancel_all`` drops pending buckets without firing callback.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from assistant.adapters.media_group import MediaGroupAggregator


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


# ---------------------------------------------------------------------------
# Test scaffolding: a recorder that captures both flush + overflow events
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        # F13: tuple now carries (chat_id, group_id, paths, caption,
        # first_message_id) — first_message_id was added in fix-pack
        # so handler IncomingMessage.message_id reflects the bucket's
        # first photo rather than the placeholder ``0``.
        self.flushes: list[tuple[int, str, list[Path], str, int]] = []
        self.overflows: list[int] = []
        # F4: error-reply callback recorder.
        self.error_replies: list[tuple[int, str]] = []

    async def flush(
        self,
        chat_id: int,
        group_id: str,
        paths: list[Path],
        caption: str,
        first_message_id: int,
    ) -> None:
        self.flushes.append(
            (chat_id, group_id, list(paths), caption, first_message_id)
        )

    async def overflow(self, chat_id: int) -> None:
        self.overflows.append(chat_id)

    async def error_reply(self, chat_id: int, text: str) -> None:
        self.error_replies.append((chat_id, text))


def _make(
    rec: _Recorder,
    *,
    debounce_sec: float = 0.05,
    flush_override: Any = None,
    error_reply: bool = False,
) -> MediaGroupAggregator:
    return MediaGroupAggregator(
        flush_cb=flush_override or rec.flush,
        overflow_cb=rec.overflow,
        error_reply_cb=rec.error_reply if error_reply else None,
        debounce_sec=debounce_sec,
        max_photos=5,
    )


# ---------------------------------------------------------------------------
# Single photo + basic debounce
# ---------------------------------------------------------------------------


async def test_single_photo_flushes_after_debounce(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)
    p = _touch(tmp_path / "a.jpg")

    await agg.add(1, "g1", p, "caption")
    # Wait past the 50 ms debounce window.
    await asyncio.sleep(0.2)

    assert len(rec.flushes) == 1
    chat_id, group_id, paths, caption, first_id = rec.flushes[0]
    assert chat_id == 1
    assert group_id == "g1"
    assert paths == [p]
    assert caption == "caption"
    assert first_id == 0  # default in tests that omit message_id


async def test_burst_resets_debounce(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)

    paths = [_touch(tmp_path / f"{i}.jpg") for i in range(3)]
    # Three rapid arrivals — each resets the timer.
    for p in paths:
        await agg.add(1, "g1", p, "")
        await asyncio.sleep(0.02)
    # Now wait past debounce.
    await asyncio.sleep(0.2)

    assert len(rec.flushes) == 1
    _, _, flushed_paths, _, _ = rec.flushes[0]
    assert flushed_paths == paths


# ---------------------------------------------------------------------------
# Bucket size cap / overflow
# ---------------------------------------------------------------------------


async def test_size_cap_overflow_drops_sixth_photo(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)

    # Fire 7 photos rapidly. First 5 land in bucket, 6th + 7th drop.
    paths = [_touch(tmp_path / f"{i}.jpg") for i in range(7)]
    for p in paths:
        await agg.add(1, "g1", p, "")
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.2)

    assert len(rec.flushes) == 1
    _, _, flushed, _, _ = rec.flushes[0]
    assert len(flushed) == 5
    # Overflow notify fires ONCE per group (devil cost-amplification).
    assert rec.overflows == [1]
    # Dropped tmp files (6th + 7th) cleaned from disk.
    assert not paths[5].exists()
    assert not paths[6].exists()
    # First 5 still on disk (the flush callback owns cleanup downstream).
    for p in paths[:5]:
        assert p.exists()


async def test_overflow_fires_once_per_group(tmp_path: Path) -> None:
    """5+1+1+1 sequence → overflow_cb called exactly ONE time per group."""
    rec = _Recorder()
    agg = _make(rec)

    for i in range(8):
        p = _touch(tmp_path / f"{i}.jpg")
        await agg.add(1, "g1", p, "")
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.2)

    assert rec.overflows == [1]


# ---------------------------------------------------------------------------
# flush_for_chat external trigger
# ---------------------------------------------------------------------------


async def test_flush_for_chat_drains_immediately(tmp_path: Path) -> None:
    rec = _Recorder()
    # Long debounce so nothing fires automatically.
    agg = _make(rec, debounce_sec=10.0)

    p1 = _touch(tmp_path / "a.jpg")
    p2 = _touch(tmp_path / "b.jpg")
    await agg.add(1, "g1", p1, "first")
    await agg.add(1, "g1", p2, "")
    await agg.flush_for_chat(1)

    assert len(rec.flushes) == 1
    _, _, paths, caption, _ = rec.flushes[0]
    assert paths == [p1, p2]
    assert caption == "first"


async def test_flush_for_chat_preempts_other_chat_unaffected(
    tmp_path: Path,
) -> None:
    """flush_for_chat(1) does NOT drain chat 2's bucket."""
    rec = _Recorder()
    agg = _make(rec, debounce_sec=10.0)

    pa = _touch(tmp_path / "a.jpg")
    pb = _touch(tmp_path / "b.jpg")
    await agg.add(1, "g1", pa, "")
    await agg.add(2, "g2", pb, "")

    await agg.flush_for_chat(1)
    assert len(rec.flushes) == 1
    assert rec.flushes[0][0] == 1


# ---------------------------------------------------------------------------
# Multi-chat / multi-group isolation
# ---------------------------------------------------------------------------


async def test_two_chats_get_separate_callbacks(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)

    p1 = _touch(tmp_path / "a.jpg")
    p2 = _touch(tmp_path / "b.jpg")
    p3 = _touch(tmp_path / "c.jpg")

    await agg.add(1, "g1", p1, "first")
    await agg.add(2, "g2", p2, "second")
    await agg.add(1, "g1", p3, "")
    await asyncio.sleep(0.2)

    assert len(rec.flushes) == 2
    by_chat = {f[0]: f for f in rec.flushes}
    assert by_chat[1][2] == [p1, p3]
    assert by_chat[2][2] == [p2]


# ---------------------------------------------------------------------------
# Caption: first non-empty wins
# ---------------------------------------------------------------------------


async def test_caption_first_non_empty_wins(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)

    pa = _touch(tmp_path / "a.jpg")
    pb = _touch(tmp_path / "b.jpg")
    pc = _touch(tmp_path / "c.jpg")
    # First arrival has empty caption; second has "winner"; third has
    # a different caption that should NOT replace winner.
    await agg.add(1, "g1", pa, "")
    await agg.add(1, "g1", pb, "winner")
    await agg.add(1, "g1", pc, "loser")
    await asyncio.sleep(0.2)

    assert rec.flushes[0][3] == "winner"


# ---------------------------------------------------------------------------
# cancel_all
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F1 regression — _do_flush self-cancellation on debounce path
# ---------------------------------------------------------------------------


async def test_debounce_flush_cb_with_internal_await_completes(
    tmp_path: Path,
) -> None:
    """F1 (CRITICAL): on the debounce path, ``bucket.flush_task`` IS the
    currently-running task. Pre-fix code called ``.cancel()`` on it,
    scheduling CancelledError on self — the next ``await`` inside the
    flush callback would raise, the photo turn would die silently, and
    tmp files would leak.

    A flush callback that performs ``await asyncio.sleep(...)`` before
    recording (mirroring real ``bot.send_chat_action``) reproduces the
    bug; the post-fix guard (``pending is not current_task``) keeps the
    callback running to completion.
    """
    rec = _Recorder()
    completed: list[bool] = []

    async def flush_with_internal_await(
        chat_id: int,
        group_id: str,
        paths: list[Path],
        caption: str,
        first_message_id: int,
    ) -> None:
        # Simulates the real ``ChatActionSender.typing`` enter / first
        # ``send_chat_action`` await — any internal yield was the
        # exact spot where pre-fix CancelledError struck.
        await asyncio.sleep(0.01)
        rec.flushes.append(
            (chat_id, group_id, list(paths), caption, first_message_id)
        )
        completed.append(True)

    agg = _make(rec, flush_override=flush_with_internal_await)
    p = _touch(tmp_path / "x.jpg")
    await agg.add(1, "g1", p, "", message_id=11)
    await asyncio.sleep(0.3)

    # Callback ran to completion — the internal await did NOT raise
    # CancelledError.
    assert completed == [True]
    assert len(rec.flushes) == 1
    assert rec.flushes[0][2] == [p]


# ---------------------------------------------------------------------------
# F4 — flush callback exception → user gets Russian error reply
# ---------------------------------------------------------------------------


async def test_flush_cb_exception_replies_russian(tmp_path: Path) -> None:
    """F4: flush_cb raises → error_reply_cb invoked with Russian
    "internal error" text so the owner is not silently left without a
    reply. Tmp files cleaned up regardless.
    """
    rec = _Recorder()

    async def boom(
        chat_id: int,
        group_id: str,
        paths: list[Path],
        caption: str,
        first_message_id: int,
    ) -> None:
        raise RuntimeError("simulated bridge error")

    agg = _make(rec, flush_override=boom, error_reply=True)
    p = _touch(tmp_path / "x.jpg")
    await agg.add(7, "g7", p, "", message_id=99)
    await asyncio.sleep(0.2)

    assert len(rec.error_replies) == 1
    chat_id, text = rec.error_replies[0]
    assert chat_id == 7
    assert "ошибка" in text or "ошибк" in text  # Russian word stem
    # Tmp file cleaned up.
    assert not p.exists()


# ---------------------------------------------------------------------------
# F13 — first_message_id captured from first photo
# ---------------------------------------------------------------------------


async def test_first_message_id_from_first_photo(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec)

    p1 = _touch(tmp_path / "a.jpg")
    p2 = _touch(tmp_path / "b.jpg")
    await agg.add(1, "g1", p1, "", message_id=42)
    await agg.add(1, "g1", p2, "", message_id=43)
    await asyncio.sleep(0.2)

    assert len(rec.flushes) == 1
    _, _, _, _, first_id = rec.flushes[0]
    assert first_id == 42


async def test_cancel_all_drops_pending_bucket(tmp_path: Path) -> None:
    rec = _Recorder()
    agg = _make(rec, debounce_sec=10.0)

    pa = _touch(tmp_path / "a.jpg")
    await agg.add(1, "g1", pa, "")

    await agg.cancel_all()
    # Wait past the original debounce window — flush must NOT fire.
    await asyncio.sleep(0.2)

    assert rec.flushes == []
