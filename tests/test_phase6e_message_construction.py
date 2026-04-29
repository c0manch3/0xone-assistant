"""Phase 6e — IncomingMessage construction-time rejection tests.

CRIT-3 close: non-telegram audio / URL turns are rejected at
``IncomingMessage.__post_init__`` so the bg dispatch model never sees a
"caller waiting on the bg result" mismatch with the scheduler
dispatcher's revert/dead-letter machinery or the picker's ledger
(neither has anyone waiting on the bg task).

Tests cover:

- Fix-pack F9: audio kind without a concrete source (no attachment,
  no URL) refuses to construct (caught at boundary instead of crashing
  deep inside ``_run_audio_job``).
- Fix-pack F10: ``origin='scheduler'`` AND ``origin='picker'`` are
  both rejected — only ``origin='telegram'`` is meaningful for audio.
- Sanity: telegram-origin audio constructs cleanly.

The three rejection checks replace the now-deleted F7 fixture
(``test_scheduler_origin_audio_turn_passes_scheduler_note`` from
phase 6c fix-pack) which exercised an envelope-assembly path that no
longer exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.adapters.base import IncomingMessage


def _make_audio_tmp(tmp_path: Path) -> Path:
    """Drop a tiny fake voice file under tmp_path/uploads."""
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    p = uploads / "voice.ogg"
    p.write_bytes(b"OggS payload")
    return p


def test_scheduler_origin_audio_rejected_at_construction(
    tmp_path: Path,
) -> None:
    """A scheduler-origin trigger carrying an ogg attachment must
    refuse to construct.

    Pre-6e the audio handler would honour this turn and inject an F7
    scheduler-note; phase 6e drops scheduler-note injection because
    the bg dispatch has no caller for ``revert_to_pending`` to talk
    to. Failing fast here means the scheduler dispatcher's outer
    try/except picks up the AssertionError, marks the trigger
    interrupted, and the dead-letter machinery runs as designed.
    """
    p = _make_audio_tmp(tmp_path)
    with pytest.raises(AssertionError, match="non-telegram audio"):
        IncomingMessage(
            chat_id=42,
            message_id=1,
            text="",
            origin="scheduler",
            meta={"trigger_id": "tr-123"},
            attachment=p,
            attachment_kind="ogg",
            attachment_filename=p.name,
            audio_duration=15,
            audio_mime_type="audio/ogg",
        )


def test_scheduler_origin_url_extraction_rejected() -> None:
    """A scheduler-origin URL extraction trigger must also refuse —
    same rationale as the file-based path."""
    with pytest.raises(AssertionError, match="non-telegram audio"):
        IncomingMessage(
            chat_id=42,
            message_id=1,
            text="",
            origin="scheduler",
            meta={"trigger_id": "tr-456"},
            url_for_extraction="https://example.com/podcast",
        )


def test_picker_origin_audio_rejected(tmp_path: Path) -> None:
    """Fix-pack F10: ``origin='picker'`` is rejected just like
    ``origin='scheduler'``.

    The picker's ledger expects synchronous handler completion; the
    bg-dispatch audio path returns the lock immediately and finishes
    minutes later out-of-band. Constructing an audio turn with
    ``origin='picker'`` would orphan the picker's ``record_finished``
    SQL update; reject at the boundary.
    """
    p = _make_audio_tmp(tmp_path)
    with pytest.raises(AssertionError, match="non-telegram audio"):
        IncomingMessage(
            chat_id=42,
            message_id=1,
            text="",
            origin="picker",
            attachment=p,
            attachment_kind="ogg",
            attachment_filename=p.name,
            audio_duration=15,
            audio_mime_type="audio/ogg",
        )


def test_audio_kind_without_source_rejected() -> None:
    """Fix-pack F9: audio ``attachment_kind`` without ``attachment`` AND
    without ``url_for_extraction`` is a contract violation.

    Pre-fix, this would surface as an ``AssertionError`` deep inside
    ``_run_audio_job`` (transcribe-time assert). Post-fix, the
    boundary catches it at construction so the offending caller's
    stack trace is unambiguous.
    """
    with pytest.raises(
        AssertionError, match="audio attachment_kind requires"
    ):
        IncomingMessage(
            chat_id=42,
            message_id=1,
            text="",
            origin="telegram",
            attachment_kind="ogg",
            attachment_filename="phantom.ogg",
            audio_duration=10,
        )


def test_telegram_origin_audio_passes(tmp_path: Path) -> None:
    """Sanity: the rejection only fires for ``origin != 'telegram'``;
    the default ``'telegram'`` origin constructs cleanly with both
    attachment-source and url-source variants.
    """
    p = _make_audio_tmp(tmp_path)
    msg = IncomingMessage(
        chat_id=42,
        message_id=1,
        text="",
        origin="telegram",
        attachment=p,
        attachment_kind="ogg",
        attachment_filename=p.name,
        audio_duration=15,
        audio_mime_type="audio/ogg",
    )
    assert msg.origin == "telegram"
    assert msg.attachment is p

    msg_url = IncomingMessage(
        chat_id=42,
        message_id=2,
        text="",
        origin="telegram",
        url_for_extraction="https://example.com/podcast",
    )
    assert msg_url.url_for_extraction == "https://example.com/podcast"
