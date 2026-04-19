"""Phase 7 / commit 18e — dispatch_reply partial-send failure isolation.

Sibling `tests/test_dispatch_reply_integration.py` already covers the
two-artefact partial-failure happy path. This file pins the **multi-artefact
isolation guarantee** (3+ outbox paths in one reply) under the three failure
shapes that matter operationally:

* Mid-list — a middle artefact's `send_*` raises; the loop MUST keep going
  (the next artefact is still attempted) and the cleaned-text send still
  fires.
* All-fail — every artefact's `send_*` raises; `dispatch_reply` MUST NOT
  propagate; per L-20 the artefact tokens stay in `cleaned` (not stripped),
  so `send_text` is invoked with the original paths intact (the user keeps
  the references and can re-prompt).
* First-fail — the very first artefact raises; the remaining ones MUST still
  be attempted (no fail-fast / early-return regression).

Per `dispatch_reply` invariants:
  * L-20 — failed `send_*` is logged via `log.warning("artefact_send_failed",
    ...)` and swallowed; the raw path is NOT stripped from `cleaned` (so the
    user sees the path they were promised) and the loop continues.
  * `cleaned` strip-on-success only — successful sends remove the raw path
    token; failed sends leave it (verified by the all-fail variant where
    `send_text` carries every original path).
  * Final `send_text` fires iff `cleaned.strip()` is non-empty (the
    surrounding prose makes that always true here).

Logging is observed via `structlog.testing.capture_logs`, mirroring
`tests/test_handler_multimodal_all_photos_fail.py`.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger, dispatch_reply


class _RecordingAdapter(MessengerAdapter):
    """`MessengerAdapter` test double that records every send and may raise.

    `raise_on_*` is a set of resolved `Path`s that — when passed to the
    matching `send_*` — raise `RuntimeError`. The recorded list captures the
    path REGARDLESS of whether the send succeeded or raised, so tests can
    assert the loop attempted the call (vs short-circuiting) even when the
    network leg blew up.
    """

    def __init__(
        self,
        *,
        raise_on_photo: set[Path] | None = None,
        raise_on_document: set[Path] | None = None,
        raise_on_audio: set[Path] | None = None,
    ) -> None:
        self._raise_on_photo = raise_on_photo or set()
        self._raise_on_document = raise_on_document or set()
        self._raise_on_audio = raise_on_audio or set()
        # Attempted = called (success OR raise). Sent = success only.
        self.photo_attempts: list[Path] = []
        self.photos_sent: list[Path] = []
        self.document_attempts: list[Path] = []
        self.documents_sent: list[Path] = []
        self.audio_attempts: list[Path] = []
        self.audios_sent: list[Path] = []
        self.texts: list[str] = []

    async def start(self) -> None:  # pragma: no cover - unused in unit tests
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - unused in unit tests
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        del chat_id
        self.texts.append(text)

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        self.photo_attempts.append(path)
        if path in self._raise_on_photo:
            raise RuntimeError(f"simulated send_photo failure for {path}")
        self.photos_sent.append(path)

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        self.document_attempts.append(path)
        if path in self._raise_on_document:
            raise RuntimeError(f"simulated send_document failure for {path}")
        self.documents_sent.append(path)

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        self.audio_attempts.append(path)
        if path in self._raise_on_audio:
            raise RuntimeError(f"simulated send_audio failure for {path}")
        self.audios_sent.append(path)


@pytest.fixture
def outbox(tmp_path: Path) -> Path:
    root = tmp_path / "outbox"
    root.mkdir()
    return root


def _make_artefact(outbox: Path, name: str) -> Path:
    """Create an outbox artefact and return the unresolved path.

    `dispatch_reply` resolves internally; tests should compare adapter
    arguments against `path.resolve()`.
    """
    p = outbox / name
    p.write_bytes(b"BYTES")
    return p


def _failed_warnings(
    cap: list[MutableMapping[str, Any]],
) -> list[MutableMapping[str, Any]]:
    return [
        e
        for e in cap
        if e.get("log_level") == "warning"
        and e.get("event") == "artefact_send_failed"
    ]


async def test_mid_list_photo_failure_does_not_skip_subsequent_document(
    outbox: Path,
) -> None:
    """Three artefacts: photo, photo (fails), document.

    The middle photo raises; the trailing document MUST still be attempted
    (and succeed). Cleaned text retains only the failed path.
    """
    p1 = _make_artefact(outbox, "first.png")
    p2_bad = _make_artefact(outbox, "second.png")
    p3 = _make_artefact(outbox, "third.pdf")

    adapter = _RecordingAdapter(raise_on_photo={p2_bad.resolve()})

    text = f"начало {p1} середина {p2_bad} конец {p3} итог"

    with capture_logs() as cap:
        await dispatch_reply(
            adapter,
            chat_id=10,
            text=text,
            outbox_root=outbox,
            dedup=_DedupLedger(),
        )

    # All three sends were ATTEMPTED in input order — the middle failure did
    # not abort the loop.
    assert adapter.photo_attempts == [p1.resolve(), p2_bad.resolve()]
    assert adapter.document_attempts == [p3.resolve()]
    # Bytes actually delivered: first photo + the document. Second photo
    # raised before recording.
    assert adapter.photos_sent == [p1.resolve()]
    assert adapter.documents_sent == [p3.resolve()]
    # Exactly one warning, naming the failed path + kind.
    warns = _failed_warnings(cap)
    assert len(warns) == 1
    assert warns[0]["resolved"] == str(p2_bad.resolve())
    assert warns[0]["kind"] == "photo"
    assert warns[0]["chat_id"] == 10
    # send_text fired exactly once with cleaned text: succeeded paths
    # stripped, the failed path retained (per L-20 strip-on-success).
    assert len(adapter.texts) == 1
    cleaned = adapter.texts[0]
    assert str(p1) not in cleaned
    assert str(p3) not in cleaned
    assert str(p2_bad) in cleaned
    assert "начало" in cleaned and "середина" in cleaned
    assert "конец" in cleaned and "итог" in cleaned


async def test_all_three_artefacts_fail_send_text_still_called(
    outbox: Path,
) -> None:
    """Every send_* raises → no exception propagates, send_text still fires.

    Per L-20, no failed-send path is stripped from `cleaned`, so the user
    receives the original paths in the text — they did not get the bytes,
    but they keep the artefact references.
    """
    photo = _make_artefact(outbox, "p.jpg")
    document = _make_artefact(outbox, "d.docx")
    audio = _make_artefact(outbox, "a.mp3")

    adapter = _RecordingAdapter(
        raise_on_photo={photo.resolve()},
        raise_on_document={document.resolve()},
        raise_on_audio={audio.resolve()},
    )

    text = f"одно {photo} два {document} три {audio} конец"

    with capture_logs() as cap:
        await dispatch_reply(
            adapter,
            chat_id=20,
            text=text,
            outbox_root=outbox,
            dedup=_DedupLedger(),
        )

    # Every send attempt fired (loop never short-circuited).
    assert adapter.photo_attempts == [photo.resolve()]
    assert adapter.document_attempts == [document.resolve()]
    assert adapter.audio_attempts == [audio.resolve()]
    # And every one raised, so no successful-send was recorded.
    assert adapter.photos_sent == []
    assert adapter.documents_sent == []
    assert adapter.audios_sent == []

    # Three artefact_send_failed warnings, one per kind.
    warns = _failed_warnings(cap)
    assert len(warns) == 3
    by_kind = {w["kind"]: w for w in warns}
    assert set(by_kind) == {"photo", "document", "audio"}
    assert by_kind["photo"]["resolved"] == str(photo.resolve())
    assert by_kind["document"]["resolved"] == str(document.resolve())
    assert by_kind["audio"]["resolved"] == str(audio.resolve())
    for w in warns:
        assert w["chat_id"] == 20

    # send_text fires exactly once. Per L-20, none of the failed paths
    # were stripped — all three remain in the cleaned text.
    assert len(adapter.texts) == 1
    cleaned = adapter.texts[0]
    assert str(photo) in cleaned
    assert str(document) in cleaned
    assert str(audio) in cleaned
    assert "одно" in cleaned and "два" in cleaned
    assert "три" in cleaned and "конец" in cleaned


async def test_first_artefact_fails_remaining_still_attempted(
    outbox: Path,
) -> None:
    """First-of-three fails → loop continues to the rest.

    Guards against an early-return / break regression that would short-circuit
    on the first exception.
    """
    p1_bad = _make_artefact(outbox, "alpha.png")
    p2 = _make_artefact(outbox, "beta.pdf")
    p3 = _make_artefact(outbox, "gamma.mp3")

    adapter = _RecordingAdapter(raise_on_photo={p1_bad.resolve()})

    text = f"альфа {p1_bad} бета {p2} гамма {p3} fin"

    with capture_logs() as cap:
        await dispatch_reply(
            adapter,
            chat_id=30,
            text=text,
            outbox_root=outbox,
            dedup=_DedupLedger(),
        )

    # The doomed first photo was attempted; the document & audio that
    # follow it MUST also have been attempted (and succeeded).
    assert adapter.photo_attempts == [p1_bad.resolve()]
    assert adapter.document_attempts == [p2.resolve()]
    assert adapter.audio_attempts == [p3.resolve()]
    assert adapter.photos_sent == []
    assert adapter.documents_sent == [p2.resolve()]
    assert adapter.audios_sent == [p3.resolve()]

    # Exactly one failure log, for the first photo.
    warns = _failed_warnings(cap)
    assert len(warns) == 1
    assert warns[0]["resolved"] == str(p1_bad.resolve())
    assert warns[0]["kind"] == "photo"

    # send_text fires once. Failed path retained; succeeded paths stripped.
    assert len(adapter.texts) == 1
    cleaned = adapter.texts[0]
    assert str(p1_bad) in cleaned
    assert str(p2) not in cleaned
    assert str(p3) not in cleaned
    assert "альфа" in cleaned and "бета" in cleaned
    assert "гамма" in cleaned and "fin" in cleaned


async def test_partial_failure_logs_carry_log_ctx(outbox: Path) -> None:
    """`log_ctx` extras must be merged into every `artefact_send_failed`
    entry — scheduler/subagent call-sites rely on this for trace correlation.
    """
    p1 = _make_artefact(outbox, "ok.png")
    p2_bad = _make_artefact(outbox, "boom.pdf")
    p3_bad = _make_artefact(outbox, "kaboom.mp3")

    adapter = _RecordingAdapter(
        raise_on_document={p2_bad.resolve()},
        raise_on_audio={p3_bad.resolve()},
    )

    text = f"x {p1} y {p2_bad} z {p3_bad} done"

    with capture_logs() as cap:
        await dispatch_reply(
            adapter,
            chat_id=99,
            text=text,
            outbox_root=outbox,
            dedup=_DedupLedger(),
            log_ctx={"trigger_id": 7, "schedule_id": 13},
        )

    warns = _failed_warnings(cap)
    assert len(warns) == 2
    for w in warns:
        assert w["trigger_id"] == 7
        assert w["schedule_id"] == 13
        assert w["chat_id"] == 99

    # Sanity: the good photo still went through, text still flowed.
    assert adapter.photos_sent == [p1.resolve()]
    assert len(adapter.texts) == 1
