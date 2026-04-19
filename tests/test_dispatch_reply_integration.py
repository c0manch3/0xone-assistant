"""Phase 7 / commit 6 — end-to-end `dispatch_reply` integration.

These tests wire a fake `MessengerAdapter` to exercise the full
extract → classify → path-guard → dedup → send → cleaned-text flow.
Individual units are covered in sibling files (`_regex`, `_classify`,
`_path_guard`, `_dedup_ledger`); this file is the glue test, asserting
that the surface behaviour of `dispatch_reply` matches plan §2.6 +
§7 (acceptance checklist).

Scenarios:
  1. Single photo → send_photo + cleaned text.
  2. One photo succeeds, one artefact send raises → failed path left
     in text, other photo + cleaned text still delivered (L-20).
  3. No artefacts + non-empty text → only send_text.
  4. Only artefacts, text reduces to whitespace → NO send_text call
     (prevents blank Telegram bubble).
  5. Multiple kinds (photo + doc + audio) → three distinct send calls
     in the order the paths appeared in the text.
  6. Dedup prevents second send for the same (path, chat_id) in one
     call (duplicated path in text) — text still cleaned both times.
  7. `log_ctx` is accepted and does not crash (smoke).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply


class _FakeAdapter(MessengerAdapter):
    """Records every send and (optionally) raises on nominated paths."""

    def __init__(self, *, raise_on: set[Path] | None = None) -> None:
        self._raise_on = raise_on or set()
        self.photos: list[Path] = []
        self.documents: list[Path] = []
        self.audios: list[Path] = []
        self.texts: list[str] = []

    async def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append(text)

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        if path in self._raise_on:
            raise RuntimeError(f"simulated send_photo failure for {path}")
        self.photos.append(path)

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        if path in self._raise_on:
            raise RuntimeError(f"simulated send_document failure for {path}")
        self.documents.append(path)

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        if path in self._raise_on:
            raise RuntimeError(f"simulated send_audio failure for {path}")
        self.audios.append(path)


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    root.mkdir()
    return root


async def test_single_photo_plus_cleaned_text(outbox: Path) -> None:
    p = outbox / "a.png"
    p.write_bytes(b"PNG")
    adapter = _FakeAdapter()

    await dispatch_reply(
        adapter,
        chat_id=42,
        text=f"готово: {p}",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    assert adapter.photos == [p.resolve()]
    assert adapter.texts == ["готово:"]
    assert adapter.documents == []
    assert adapter.audios == []


async def test_partial_send_failure_preserves_rest(outbox: Path) -> None:
    """L-20: one send raises; the OTHER artefact + cleaned text still
    flow. The failed raw path stays in the cleaned text so the user
    can see it (we did NOT deliver the bytes)."""
    good = outbox / "ok.png"
    good.write_bytes(b"PNG")
    bad = outbox / "boom.png"
    bad.write_bytes(b"PNG")

    adapter = _FakeAdapter(raise_on={bad.resolve()})

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"хорошо: {good}\nплохо: {bad}\nготово",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    # Good one sent, bad one swallowed by the warning branch.
    assert adapter.photos == [good.resolve()]
    # Cleaned text: good path stripped; bad path survives for user to
    # see; trailing "готово" preserved.
    text = adapter.texts[0]
    assert str(good) not in text
    assert str(bad) in text
    assert "готово" in text


async def test_no_artefacts_text_only(outbox: Path) -> None:
    adapter = _FakeAdapter()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text="плоский ответ без путей",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    assert adapter.photos == []
    assert adapter.documents == []
    assert adapter.audios == []
    assert adapter.texts == ["плоский ответ без путей"]


async def test_only_artefact_text_empty_no_text_send(outbox: Path) -> None:
    p = outbox / "a.png"
    p.write_bytes(b"PNG")
    adapter = _FakeAdapter()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"{p}",
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    assert adapter.photos == [p.resolve()]
    # Cleaned text reduces to empty — no blank bubble.
    assert adapter.texts == []


async def test_multiple_kinds_delivered_in_order(outbox: Path) -> None:
    photo = outbox / "a.png"
    photo.write_bytes(b"PNG")
    doc = outbox / "b.pdf"
    doc.write_bytes(b"%PDF")
    audio = outbox / "c.mp3"
    audio.write_bytes(b"MP3")
    adapter = _FakeAdapter()

    text = f"photo {photo} doc {doc} audio {audio} done"
    await dispatch_reply(
        adapter,
        chat_id=1,
        text=text,
        outbox_root=outbox,
        dedup=_DedupLedger(),
    )

    assert adapter.photos == [photo.resolve()]
    assert adapter.documents == [doc.resolve()]
    assert adapter.audios == [audio.resolve()]
    cleaned = adapter.texts[0]
    # All three paths stripped.
    assert str(photo) not in cleaned
    assert str(doc) not in cleaned
    assert str(audio) not in cleaned
    assert "photo" in cleaned and "doc" in cleaned and "audio" in cleaned


async def test_duplicate_path_in_single_text_deduped(outbox: Path) -> None:
    """Same `(path, chat_id)` twice in one message → ledger sees the
    second one, skips the network send, still strips the text."""
    p = outbox / "x.png"
    p.write_bytes(b"PNG")
    adapter = _FakeAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"раз {p} два {p}",
        outbox_root=outbox,
        dedup=ledger,
    )

    # Network send exactly once.
    assert adapter.photos == [p.resolve()]
    cleaned = adapter.texts[0]
    assert str(p) not in cleaned
    assert "раз" in cleaned and "два" in cleaned


async def test_log_ctx_accepted_and_threaded(outbox: Path) -> None:
    """Smoke: passing a `log_ctx` dict must not raise; structlog
    handles arbitrary kwargs. We don't assert the log record here
    (the logger is globally configured) — just that the call path
    is type-clean."""
    p = outbox / "a.png"
    p.write_bytes(b"PNG")
    adapter = _FakeAdapter()

    await dispatch_reply(
        adapter,
        chat_id=1,
        text=f"ok {p}",
        outbox_root=outbox,
        dedup=_DedupLedger(),
        log_ctx={"trigger_id": 12, "schedule_id": 34},
    )

    assert adapter.photos == [p.resolve()]


async def test_dedup_across_two_calls_same_ledger(outbox: Path) -> None:
    """Mirrors the 3-call-site integration: two dispatch_reply calls
    share ONE _DedupLedger. Second call for the same path → skip."""
    p = outbox / "x.png"
    p.write_bytes(b"PNG")
    ledger = _DedupLedger()

    adapter_a = _FakeAdapter()
    await dispatch_reply(
        adapter_a,
        chat_id=5,
        text=f"first {p}",
        outbox_root=outbox,
        dedup=ledger,
    )
    adapter_b = _FakeAdapter()
    await dispatch_reply(
        adapter_b,
        chat_id=5,
        text=f"second {p}",
        outbox_root=outbox,
        dedup=ledger,
    )

    # First call: one photo delivered.
    assert adapter_a.photos == [p.resolve()]
    # Second call: ledger says SEEN, nothing sent — but text still
    # cleaned.
    assert adapter_b.photos == []
    assert str(p) not in adapter_b.texts[0]


async def test_dedup_different_chat_ids_independent(outbox: Path) -> None:
    """Ledger key is `(path, chat_id)`. Same path, different chat →
    two independent sends."""
    p = outbox / "x.png"
    p.write_bytes(b"PNG")
    ledger = _DedupLedger()

    adapter_a = _FakeAdapter()
    await dispatch_reply(
        adapter_a, chat_id=1, text=f"first {p}",
        outbox_root=outbox, dedup=ledger,
    )
    adapter_b = _FakeAdapter()
    await dispatch_reply(
        adapter_b, chat_id=2, text=f"second {p}",
        outbox_root=outbox, dedup=ledger,
    )

    assert adapter_a.photos == [p.resolve()]
    assert adapter_b.photos == [p.resolve()]
