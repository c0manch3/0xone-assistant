"""Phase 7: MediaSettings defaults + env override surface.

Canonical reference: `plan/phase7/implementation.md` §2.11 (verbatim table).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.config import MediaSettings, Settings


def test_defaults_match_spec() -> None:
    """All defaults must match implementation.md §2.11 verbatim."""
    m = MediaSettings()

    # Photo path
    assert m.photo_mode == "inline_base64"
    assert m.photo_max_inline_bytes == 5_242_880
    assert m.photo_download_max_bytes == 10_485_760

    # Voice / audio
    assert m.voice_max_sec == 1800
    assert m.voice_inline_threshold_sec == 30
    assert m.voice_max_bytes == 15_000_000
    assert m.audio_max_bytes == 50_000_000

    # Document
    assert m.document_max_bytes == 20_971_520

    # Transcribe
    assert m.transcribe_endpoint == "http://localhost:9100/transcribe"
    assert m.transcribe_language_default == "auto"
    assert m.transcribe_timeout_s == 60
    assert m.transcribe_max_input_bytes == 25_000_000

    # Genimage
    assert m.genimage_endpoint == "http://localhost:9101/generate"
    assert m.genimage_daily_cap == 1
    assert m.genimage_steps_default == 8
    assert m.genimage_timeout_s == 120

    # Extract / Render
    assert m.extract_max_input_bytes == 20_000_000
    assert m.render_max_body_bytes == 512_000
    assert m.render_max_output_bytes == 10_485_760

    # Retention
    assert m.retention_inbox_days == 14
    assert m.retention_outbox_days == 7
    assert m.retention_total_cap_bytes == 2_147_483_648
    assert m.sweep_interval_s == 3600


def test_env_override_photo_mode_path_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """MEDIA_PHOTO_MODE=path_tool parses via Literal."""
    monkeypatch.setenv("MEDIA_PHOTO_MODE", "path_tool")
    m = MediaSettings()
    assert m.photo_mode == "path_tool"


def test_env_override_invalid_photo_mode_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Literal rejects unknown photo_mode values."""
    monkeypatch.setenv("MEDIA_PHOTO_MODE", "not_a_mode")
    with pytest.raises(ValidationError):
        MediaSettings()


def test_extra_env_var_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra='ignore' → unknown MEDIA_* env vars don't raise."""
    monkeypatch.setenv("MEDIA_UNKNOWN_KNOB", "42")
    m = MediaSettings()  # must not raise
    assert not hasattr(m, "unknown_knob")


def test_settings_has_media_default() -> None:
    """`Settings.media` is a MediaSettings instance by default."""
    s = Settings(telegram_bot_token="test", owner_chat_id=1)  # type: ignore[call-arg]
    assert isinstance(s.media, MediaSettings)
    assert s.media.photo_mode == "inline_base64"
