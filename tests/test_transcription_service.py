"""Phase 6c — TranscriptionService unit tests.

Mocks httpx with ``httpx.MockTransport`` so we exercise the real
client + serialisation logic without real network I/O.

Covers:

- happy path /transcribe → TranscriptionResult parsed;
- happy path /extract → metadata fields parsed;
- bearer token in Authorization header;
- timeout → TranscriptionError with sanitised Russian message;
- ConnectError → TranscriptionError;
- 401 (auth fail) → TranscriptionError;
- 413 (too large) → TranscriptionError;
- 422 (yt-dlp can't extract) → TranscriptionError;
- /health success and failure shapes;
- enabled=False when WHISPER_API_URL/TOKEN unset.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from assistant.config import ClaudeSettings, Settings
from assistant.services.transcription import (
    TranscriptionError,
    TranscriptionService,
)


def _settings(
    tmp_path: Path,
    *,
    url: str | None = "http://mac-mini.test:9000",
    token: str | None = "x" * 32,
) -> Settings:
    """Build a Settings instance with optional whisper config."""
    kwargs: dict[str, object] = {
        "telegram_bot_token": "123456:" + "x" * 30,
        "owner_chat_id": 42,
        "project_root": tmp_path,
        "data_dir": tmp_path / "data",
        "claude": ClaudeSettings(timeout=30, max_concurrent=1),
    }
    if url is not None:
        kwargs["whisper_api_url"] = url
    if token is not None:
        kwargs["whisper_api_token"] = token
    return Settings(**kwargs)  # type: ignore[arg-type]


def _service_with_handler(
    settings: Settings,
    handler: httpx.MockTransport | None = None,
    *,
    transport_factory: object | None = None,
) -> tuple[TranscriptionService, list[httpx.Request]]:
    """Build TranscriptionService whose AsyncClient uses a mock transport.

    Returns the service plus a ``captured`` list that the test asserts
    against (each entry is the actual httpx.Request the client sent).
    """
    captured: list[httpx.Request] = []

    if transport_factory is not None:
        # Test wants to swap in a custom transport (e.g. ConnectError).
        def _factory(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            raise transport_factory(req)  # type: ignore[misc, operator]
        transport: httpx.MockTransport = httpx.MockTransport(_factory)
    elif handler is not None:
        # Wrap the test handler with our capture sidecar.
        original_handler = handler.handler  # type: ignore[attr-defined]

        def _wrapper(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return original_handler(req)
        transport = httpx.MockTransport(_wrapper)
    else:
        raise ValueError("either handler or transport_factory required")

    service = TranscriptionService(settings)

    # Monkeypatch the AsyncClient constructor used inside service so it
    # picks up our mock transport. We do this by subclassing.
    import assistant.services.transcription as t_mod

    real_client = t_mod.httpx.AsyncClient

    class _PatchedClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    t_mod.httpx.AsyncClient = _PatchedClient  # type: ignore[misc]
    # Caller is responsible for resetting via fixture teardown.
    return service, captured


@pytest.fixture
def restore_httpx() -> object:
    import assistant.services.transcription as t_mod
    original = t_mod.httpx.AsyncClient
    yield
    t_mod.httpx.AsyncClient = original  # type: ignore[misc]


# ----------------------------------------------------------------------
# enabled flag
# ----------------------------------------------------------------------


def test_enabled_false_without_url(tmp_path: Path) -> None:
    s = _settings(tmp_path, url=None, token=None)
    svc = TranscriptionService(s)
    assert svc.enabled is False


def test_enabled_true_when_pair_set(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    svc = TranscriptionService(s)
    assert svc.enabled is True


# ----------------------------------------------------------------------
# Happy path /transcribe
# ----------------------------------------------------------------------


async def test_transcribe_happy_path(
    tmp_path: Path, restore_httpx: object
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "  Привет, это тест.  ",
                "language": "ru",
                "duration": 4.2,
                "segments": [],
            },
        )

    svc, captured = _service_with_handler(
        _settings(tmp_path),
        handler=httpx.MockTransport(handler),
    )
    result = await svc.transcribe(b"fake-audio", "audio/ogg", "voice.ogg")
    assert result.text == "Привет, это тест."
    assert result.language == "ru"
    assert result.duration == pytest.approx(4.2)
    # Bearer header present.
    assert captured[0].headers["Authorization"].startswith("Bearer ")
    assert captured[0].headers["Authorization"].endswith("x" * 32)


# ----------------------------------------------------------------------
# Happy path /extract
# ----------------------------------------------------------------------


async def test_extract_url_happy_path(
    tmp_path: Path, restore_httpx: object
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "Лекция про FastAPI",
                "language": "ru",
                "duration": 1800.0,
                "title": "FastAPI talk",
                "channel": "PyCon",
                "upload_date": "20260415",
                "segments": [],
            },
        )

    svc, captured = _service_with_handler(
        _settings(tmp_path),
        handler=httpx.MockTransport(handler),
    )
    result = await svc.extract_url("https://example.com/video")
    assert result.title == "FastAPI talk"
    assert result.channel == "PyCon"
    assert result.upload_date == "20260415"
    assert captured[0].headers["Authorization"].startswith("Bearer ")
    # extract uses POST with JSON body — verify shape
    assert captured[0].method == "POST"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


async def test_transcribe_timeout_raises_sanitised_russian(
    tmp_path: Path, restore_httpx: object
) -> None:
    svc, _ = _service_with_handler(
        _settings(tmp_path),
        transport_factory=httpx.TimeoutException,
    )
    with pytest.raises(TranscriptionError) as excinfo:
        await svc.transcribe(b"x", "audio/ogg", "v.ogg")
    msg = str(excinfo.value)
    # Sanitised Russian, no repr leak.
    assert "таймаут" in msg
    assert "TimeoutException" not in msg


async def test_transcribe_connect_error(
    tmp_path: Path, restore_httpx: object
) -> None:
    svc, _ = _service_with_handler(
        _settings(tmp_path),
        transport_factory=httpx.ConnectError,
    )
    with pytest.raises(TranscriptionError) as excinfo:
        await svc.transcribe(b"x", "audio/ogg", "v.ogg")
    assert "offline" in str(excinfo.value).lower()


@pytest.mark.parametrize(
    "status,expected_substr",
    [
        (401, "отказ"),  # auth
        (413, "большая"),  # too large
        (422, "yt-dlp"),
    ],
)
async def test_transcribe_http_error_codes(
    tmp_path: Path,
    restore_httpx: object,
    status: int,
    expected_substr: str,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server-side error")

    svc, _ = _service_with_handler(
        _settings(tmp_path),
        handler=httpx.MockTransport(handler),
    )
    with pytest.raises(TranscriptionError) as excinfo:
        # Use extract_url so the 422 yt-dlp mapping is exercised.
        await svc.extract_url("https://example.com/x")
    assert expected_substr in str(excinfo.value).lower()


# ----------------------------------------------------------------------
# /health
# ----------------------------------------------------------------------


async def test_health_check_ok(tmp_path: Path, restore_httpx: object) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "model_loaded": True,
                "yt_dlp_version": "2026.04.15",
            },
        )

    svc, _ = _service_with_handler(
        _settings(tmp_path),
        handler=httpx.MockTransport(handler),
    )
    assert await svc.health_check() is True


async def test_health_check_model_not_loaded(
    tmp_path: Path, restore_httpx: object
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "ok", "model_loaded": False},
        )

    svc, _ = _service_with_handler(
        _settings(tmp_path),
        handler=httpx.MockTransport(handler),
    )
    assert await svc.health_check() is False


async def test_health_check_offline(
    tmp_path: Path, restore_httpx: object
) -> None:
    svc, _ = _service_with_handler(
        _settings(tmp_path),
        transport_factory=httpx.ConnectError,
    )
    assert await svc.health_check() is False


# ----------------------------------------------------------------------
# Disabled service
# ----------------------------------------------------------------------


async def test_disabled_transcribe_raises(tmp_path: Path) -> None:
    s = _settings(tmp_path, url=None, token=None)
    svc = TranscriptionService(s)
    with pytest.raises(TranscriptionError):
        await svc.transcribe(b"x", "audio/ogg", "v.ogg")


async def test_disabled_extract_raises(tmp_path: Path) -> None:
    s = _settings(tmp_path, url=None, token=None)
    svc = TranscriptionService(s)
    with pytest.raises(TranscriptionError):
        await svc.extract_url("https://example.com/x")
