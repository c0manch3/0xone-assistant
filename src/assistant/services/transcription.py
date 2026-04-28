"""Phase 6c: HTTP client for the Mac mini Whisper sidecar.

The sidecar exposes three endpoints over Tailscale (private mesh):

- ``POST /transcribe`` — multipart audio upload + bearer auth → JSON.
- ``POST /extract`` — JSON ``{url}`` body + bearer auth → JSON.
- ``GET /health`` — liveness probe (no auth).

Trust model: requests originate from the bot container (sharing the
Tailscale sidecar's network namespace). The bearer token is a
defence-in-depth layer on top of the Tailscale ACL — even if the ACL
ever leaks, requests without the token are rejected at the Mac sidecar.

Error policy: every transport / HTTP / non-200 outcome is normalised to
:class:`TranscriptionError` with a sanitised Russian message that the
adapter / handler can ``await message.reply(...)`` directly. ``repr(exc)``
is NEVER surfaced to the user (might leak file IDs, paths, tokens).
The structured log line carries the full exception type for owner
post-mortem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog

from assistant.config import Settings

log = structlog.get_logger(__name__)

# Health probe must be fast — Mac mini awake should answer in <50 ms over
# Tailscale; a 5-second cap is generous and keeps the audio handler from
# stalling for tens of seconds when the Mac is offline.
_HEALTH_TIMEOUT_S = 5.0

# Server-side max audio bytes per upload. Keep loosely aligned with the
# adapter's 20 MB Telegram cap; tighter caps live in the sidecar's own
# request-size enforcement.
_MAX_AUDIO_BYTES = 100 * 1024 * 1024


# Russian replies, mapped from sidecar HTTP status codes. The sidecar's
# response body is intentionally NOT echoed (might contain yt-dlp
# stderr fragments with paths or URL parameters that leak metadata).
_HTTP_ERR_MAP: dict[int, str] = {
    400: "не получилось разобрать ссылку",
    401: "транскрипция: отказ авторизации (sidecar bearer mismatch)",
    413: "слишком длинная или большая запись (>3 часа или >100 МБ)",
    415: "не смог обработать аудио, попробуй другой формат",
    422: "не смог извлечь аудио из ссылки (yt-dlp не справился)",
    504: "yt-dlp таймаут (>10 мин на скачивание)",
    507: "Mac sidecar — закончилось место",
}


class TranscriptionError(RuntimeError):
    """Raised on any failure of the Whisper sidecar round-trip.

    The string form is already a sanitised Russian message safe for
    ``message.reply`` — callers should NOT prefix or wrap it with
    additional text the user might find confusing.
    """


@dataclass(frozen=True)
class TranscriptionResult:
    """Normalised payload returned by both ``/transcribe`` and ``/extract``.

    ``segments`` carries timestamped chunks if the caller wants to
    surface a clip extract later; current handler ignores them.
    Optional ``title`` / ``channel`` / ``upload_date`` are populated only
    by ``/extract`` (yt-dlp metadata).
    """

    text: str
    language: str
    duration: float
    title: str | None = None
    channel: str | None = None
    upload_date: str | None = None
    segments: list[dict[str, Any]] = field(default_factory=list)


def _map_http_status(status: int) -> str:
    """Return the Russian reply string for a sidecar HTTP error."""
    return _HTTP_ERR_MAP.get(
        status,
        f"транскрипция временно недоступна (sidecar status={status})",
    )


class TranscriptionService:
    """Bot-side httpx client for the Mac mini Whisper sidecar.

    Stateless across calls; each public method opens a fresh
    ``httpx.AsyncClient`` (we do not keep a long-lived client because
    Tailscale's NAT path occasionally rotates and a stale connection
    pool produces opaque ConnectError spikes).

    When :attr:`enabled` is False (no URL/token configured), every
    public method raises :class:`TranscriptionError` immediately so the
    handler reaches the spec'd "Mac sidecar offline" reply path.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = (settings.whisper_api_url or "").rstrip("/")
        self._token = settings.whisper_api_token or ""
        self._timeout = float(settings.whisper_timeout)

    @property
    def enabled(self) -> bool:
        """True when both URL and token are configured."""
        return bool(self._url and self._token)

    def _headers(self) -> dict[str, str]:
        # Bearer token on every request; ``compare_digest`` runs
        # server-side. ``User-Agent`` helps the sidecar log distinguish
        # legitimate bot traffic from arbitrary tailnet probes.
        return {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "0xone-assistant/6c",
        }

    async def health_check(self) -> bool:
        """Probe ``GET /health`` (no auth) with a 5-second cap.

        Returns ``True`` only on 200 + ``model_loaded == True``. Any
        other outcome (timeout, ConnectError, non-200, malformed body,
        ``model_loaded`` False) returns ``False`` — the adapter then
        emits the offline-reject reply WITHOUT raising.
        """
        if not self._url:
            return False
        try:
            async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
                resp = await client.get(f"{self._url}/health")
        except httpx.TimeoutException:
            log.info("whisper_health_timeout")
            return False
        except httpx.ConnectError as exc:
            log.info("whisper_health_connect_error", error=type(exc).__name__)
            return False
        except httpx.HTTPError as exc:
            log.warning(
                "whisper_health_http_error",
                error_type=type(exc).__name__,
            )
            return False
        if resp.status_code != 200:
            log.info("whisper_health_non_200", status=resp.status_code)
            return False
        try:
            payload = resp.json()
        except ValueError:
            log.warning("whisper_health_malformed_json")
            return False
        return bool(payload.get("model_loaded", False))

    async def transcribe(
        self,
        audio_bytes: bytes,
        mime_type: str,
        filename: str,
    ) -> TranscriptionResult:
        """POST ``/transcribe`` with the audio bytes + bearer auth.

        Raises :class:`TranscriptionError` on any failure; the exception
        message is already a sanitised Russian string for
        ``message.reply``.

        F9 (fix-pack) note: callers SHOULD prefer
        :meth:`transcribe_file` when they have the file on disk —
        ``audio_bytes`` materialises the entire file in RAM, which can
        OOM the 1.5 GB-capped bot container on a 100 MB upload.
        """
        if not self.enabled:
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar не настроен)"
            )
        if len(audio_bytes) > _MAX_AUDIO_BYTES:
            raise TranscriptionError(
                "слишком длинная или большая запись (>3 часа или >100 МБ)"
            )
        files = {
            "file": (
                filename or "audio.bin",
                audio_bytes,
                mime_type or "application/octet-stream",
            )
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._url}/transcribe",
                    files=files,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            log.warning("whisper_transcribe_timeout", error=type(exc).__name__)
            raise TranscriptionError(
                "транскрипция таймаут (>1 часа), попробуй короче"
            ) from exc
        except httpx.ConnectError as exc:
            log.warning(
                "whisper_transcribe_connect_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar offline)"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning(
                "whisper_transcribe_http_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция: внутренняя ошибка сети"
            ) from exc
        return self._parse_response(resp)

    async def transcribe_file(
        self,
        audio_path: Path,
        mime_type: str,
        filename: str,
    ) -> TranscriptionResult:
        """POST ``/transcribe`` streaming the file from disk.

        F9 (fix-pack): preferred over :meth:`transcribe` when the
        attachment lives on disk. ``httpx`` reads the file in chunks
        through the multipart encoder so the bot container does NOT
        materialise the whole audio in RAM. Tested for 100 MB uploads
        on a 1.5 GB container.

        Raises :class:`TranscriptionError` on any failure; the exception
        message is already a sanitised Russian string for
        ``message.reply``.
        """
        if not self.enabled:
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar не настроен)"
            )
        # ASYNC240: ``Path.stat`` is a sync filesystem call; offload to
        # a thread so the event loop stays free for other turns. Same
        # rationale as the file-read inside ``open("rb")`` below — we
        # accept a brief blocking window per upload (~10ms for stat).
        import asyncio as _asyncio

        try:
            size = await _asyncio.to_thread(
                lambda: audio_path.stat().st_size
            )
        except OSError as exc:
            raise TranscriptionError(
                "не смог прочитать аудио файл"
            ) from exc
        if size > _MAX_AUDIO_BYTES:
            raise TranscriptionError(
                "слишком длинная или большая запись (>3 часа или >100 МБ)"
            )

        try:
            with audio_path.open("rb") as fh:
                files = {
                    "file": (
                        filename or audio_path.name or "audio.bin",
                        fh,
                        mime_type or "application/octet-stream",
                    )
                }
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        f"{self._url}/transcribe",
                        files=files,
                        headers=self._headers(),
                    )
        except httpx.TimeoutException as exc:
            log.warning("whisper_transcribe_timeout", error=type(exc).__name__)
            raise TranscriptionError(
                "транскрипция таймаут (>1 часа), попробуй короче"
            ) from exc
        except httpx.ConnectError as exc:
            log.warning(
                "whisper_transcribe_connect_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar offline)"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning(
                "whisper_transcribe_http_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция: внутренняя ошибка сети"
            ) from exc
        except OSError as exc:
            raise TranscriptionError(
                "не смог прочитать аудио файл"
            ) from exc
        return self._parse_response(resp)

    async def extract_url(self, url: str) -> TranscriptionResult:
        """POST ``/extract`` with the URL + bearer auth.

        The sidecar runs ``yt-dlp`` to fetch the audio, ffmpeg to
        normalise, and Whisper to transcribe — all in one round-trip.
        Same error policy as :meth:`transcribe`.
        """
        if not self.enabled:
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar не настроен)"
            )
        if not isinstance(url, str) or not url.startswith(
            ("http://", "https://")
        ):
            raise TranscriptionError("не похоже на ссылку для транскрипции")
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._url}/extract",
                    json={"url": url, "language": "ru"},
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            log.warning("whisper_extract_timeout", error=type(exc).__name__)
            raise TranscriptionError(
                "yt-dlp таймаут (>10 мин на скачивание)"
            ) from exc
        except httpx.ConnectError as exc:
            log.warning(
                "whisper_extract_connect_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция временно недоступна (sidecar offline)"
            ) from exc
        except httpx.HTTPError as exc:
            log.warning(
                "whisper_extract_http_error",
                error_type=type(exc).__name__,
            )
            raise TranscriptionError(
                "транскрипция: внутренняя ошибка сети"
            ) from exc
        return self._parse_response(resp)

    @staticmethod
    def _parse_response(resp: httpx.Response) -> TranscriptionResult:
        """Decode a successful sidecar response into a result struct.

        Non-200 → :class:`TranscriptionError` with the Russian map.
        Malformed JSON / missing fields → :class:`TranscriptionError`
        (the sidecar's own contract is broken — owner needs to know).
        """
        if resp.status_code != 200:
            log.warning(
                "whisper_non_200",
                status=resp.status_code,
                # NEVER log the full body: yt-dlp stderr can include
                # URL parameters / paths / cookies. A bounded preview
                # of the first 80 bytes is enough for owner debugging.
                body_preview=resp.text[:80] if resp.text else "",
            )
            raise TranscriptionError(_map_http_status(resp.status_code))
        try:
            payload = resp.json()
        except ValueError as exc:
            log.warning("whisper_malformed_json")
            raise TranscriptionError(
                "транскрипция: некорректный ответ от sidecar"
            ) from exc
        text = payload.get("text")
        if not isinstance(text, str):
            raise TranscriptionError(
                "транскрипция: пустой или некорректный текст"
            )
        return TranscriptionResult(
            text=text.strip(),
            language=str(payload.get("language", "ru")),
            duration=float(payload.get("duration", 0.0)),
            title=payload.get("title"),
            channel=payload.get("channel"),
            upload_date=payload.get("upload_date"),
            segments=list(payload.get("segments") or []),
        )
