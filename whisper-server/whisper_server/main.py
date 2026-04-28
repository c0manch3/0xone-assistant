"""Phase 6c: Mac mini Whisper sidecar — FastAPI service.

Endpoints (per research RQ4 + RQ6):

- ``POST /transcribe`` — multipart audio upload + bearer auth.
- ``POST /extract`` — JSON ``{url}`` body + bearer auth (yt-dlp).
- ``GET /health`` — no auth; returns ``{status, model_loaded, yt_dlp_version}``.

Transport: the FastAPI process binds to ``127.0.0.1:9000`` (loopback
only). Cross-host reachability is handled by an SSH reverse tunnel
(``autossh -N -R 9000:localhost:9000`` from the Mac to the VPS); the
VPS sshd's ``GatewayPorts yes`` directive then re-publishes that
listener on the docker bridge so the bot container reaches it via
``host.docker.internal``. Bearer token + SSH key are layered defence-
in-depth — both must be valid for a request to land.

Architecture invariants:

- mlx-whisper is pre-warmed in the FastAPI lifespan (0.5 s silence sample)
  so the first real request doesn't pay 4-7 s of model-load latency.
- ``ffmpeg`` and ``yt-dlp`` invoked via ``asyncio.create_subprocess_exec``
  with explicit args lists (no shell metacharacter expansion ever).
  NEVER ``shell=True``. NEVER yt-dlp ``--exec`` (CVE-2023-40581).
- Bearer token compared via ``secrets.compare_digest`` (constant time).
- ``mlx_whisper.transcribe`` is sync; wrapped in ``asyncio.to_thread``
  so the event loop stays free for the next request.
- 3-hour duration cap enforced server-side via ``ffprobe`` AFTER
  yt-dlp finishes (yt-dlp does not pre-validate duration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl

from whisper_server.config import WhisperSettings  # type: ignore[import-not-found]

log = structlog.get_logger("whisper_server")

settings = WhisperSettings()  # type: ignore[call-arg]

# Module-level mutable state populated at startup.
_MODEL_LOADED = False
_YT_DLP_VERSION: str | None = None
_YT_DLP_BIN = shutil.which("yt-dlp") or "yt-dlp"
_FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"


# ---------------------------------------------------------------------------
# Auth dependency (constant-time bearer)
# ---------------------------------------------------------------------------
async def verify_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer auth. Returns silently on success; raises 401 otherwise."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[len("Bearer "):].strip()
    if not secrets.compare_digest(presented, settings.whisper_api_token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class YtDlpDiskFullError(RuntimeError):
    pass


class YtDlpTimeoutError(RuntimeError):
    pass


class YtDlpExtractError(RuntimeError):
    pass


async def _run_subprocess(
    *args: str, timeout: float | None = None
) -> tuple[int, bytes, bytes]:
    """Spawn an argv (no shell) and return (rc, stdout, stderr).

    Raises :class:`TimeoutError` on timeout. NEVER uses ``shell=True``;
    the args list is passed straight to ``execvp`` by the asyncio
    subprocess implementation.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if timeout is None:
            stdout, stderr = await proc.communicate()
        else:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (proc.returncode or 0, stdout, stderr)


async def _check_yt_dlp_version() -> str | None:
    try:
        rc, out, _ = await _run_subprocess(_YT_DLP_BIN, "--version", timeout=10.0)
    except (FileNotFoundError, TimeoutError, OSError) as exc:
        log.warning("yt_dlp_version_probe_failed", error=repr(exc))
        return None
    if rc != 0:
        return None
    return out.decode().strip() or None


async def _convert_to_wav(src: Path, dst: Path) -> None:
    """ffmpeg subprocess: any → 16 kHz mono WAV PCM s16le."""
    rc, _, stderr = await _run_subprocess(
        _FFMPEG_BIN,
        "-y",
        "-i", str(src),
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(dst),
    )
    if rc != 0:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"ffmpeg: {stderr.decode(errors='replace')[:500]}",
        )


async def _ffprobe_duration(path: Path) -> float:
    """Return audio duration in seconds. 0.0 when probe fails."""
    rc, out, _ = await _run_subprocess(
        _FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    if rc != 0:
        return 0.0
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def _yt_dlp_extract(
    url: str, work_dir: Path, *, timeout: int
) -> tuple[Path, dict[str, Any]]:
    """Download URL audio + metadata; return (audio_path, meta_dict)."""
    free = shutil.disk_usage(work_dir).free
    if free < settings.yt_dlp_min_disk_free_bytes:
        raise YtDlpDiskFullError(
            f"only {free / 1e9:.1f} GB free; refusing extract"
        )
    out_template = str(work_dir / "audio.%(ext)s")
    # F12 (fix-pack): use ``--print "%(info)j"`` instead of
    # ``--print-json``. The legacy line-prefix sniffing parser broke on
    # multi-line JSON or yt-dlp stdout chatter (e.g. download-progress
    # lines printed to stdout in some yt-dlp versions). ``%(info)j`` is
    # documented to emit a single JSON line per-video AFTER all download
    # work finishes — robust to whatever yt-dlp prints in between.
    args = [
        _YT_DLP_BIN,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--restrict-filenames",
        "--no-live-from-start",
        "--max-filesize", f"{settings.yt_dlp_max_filesize_mb}M",
        "-f",
        f"bestaudio[filesize<{settings.yt_dlp_max_filesize_mb}M]/"
        "bestaudio[abr<=128]/bestaudio/best",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "5",
        "-o", out_template,
        "--socket-timeout", "30",
        "--retries", "2",
        "--print", "%(info)j",
        url,
    ]
    try:
        rc, stdout, stderr = await _run_subprocess(*args, timeout=timeout)
    except TimeoutError as exc:
        raise YtDlpTimeoutError(f"yt-dlp timed out after {timeout}s") from exc
    if rc != 0:
        raise YtDlpExtractError(
            stderr.decode(errors="replace")[:1000] or "yt-dlp non-zero exit"
        )
    candidates = list(work_dir.glob("audio.*"))
    if not candidates:
        raise YtDlpExtractError("no audio file produced")
    meta: dict[str, Any] = {}
    # Try the cheap path first: the entire stdout decoded as one JSON
    # object (``%(info)j`` emits a single self-contained line). If that
    # fails, fall back to scanning each line — keeps backwards compat
    # with any yt-dlp version that emits info-json with embedded
    # newlines (rare, but observed historically).
    decoded = stdout.decode(errors="replace").strip()
    if decoded:
        try:
            obj = json.loads(decoded)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            meta = obj
        else:
            for raw_line in decoded.splitlines():
                line = raw_line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    line_obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(line_obj, dict):
                    meta = line_obj
    return candidates[0], meta


async def _transcribe_wav(wav_path: Path, language: str) -> dict[str, Any]:
    """Run mlx-whisper on a 16 kHz mono WAV. Sync → ``to_thread``."""
    import mlx_whisper  # type: ignore[import-not-found]

    def _run() -> Any:
        return mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=settings.model_repo,
            language=language,
            word_timestamps=False,
            fp16=True,
        )

    return await asyncio.to_thread(_run)


async def _prewarm() -> None:
    """Run a 0.5 s silence sample through the model so weights land in
    unified memory before the first real request."""
    global _MODEL_LOADED
    try:
        import mlx_whisper  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError as exc:
        log.error("prewarm_skipped_missing_deps", error=repr(exc))
        return
    silence = np.zeros(8000, dtype=np.float32)  # 0.5 s @ 16 kHz

    def _run() -> Any:
        return mlx_whisper.transcribe(
            silence,
            path_or_hf_repo=settings.model_repo,
            language=settings.default_language,
        )

    try:
        await asyncio.to_thread(_run)
        _MODEL_LOADED = True
        log.info("prewarm_ok", model=settings.model_repo)
    except Exception as exc:  # noqa: BLE001
        log.error("prewarm_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    url: HttpUrl
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    max_duration_sec: int = Field(default=10800, ge=1, le=10800)


class ExtractSegment(BaseModel):
    start: float
    end: float
    text: str


class ExtractResponse(BaseModel):
    text: str
    language: str
    duration: float
    title: str | None = None
    channel: str | None = None
    upload_date: str | None = None
    segments: list[ExtractSegment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# FastAPI lifespan + app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    global _YT_DLP_VERSION
    _YT_DLP_VERSION = await _check_yt_dlp_version()
    if _YT_DLP_VERSION is None:
        log.warning("yt_dlp_unavailable")
    await _prewarm()
    yield
    # No teardown — MLX releases on process exit.


app = FastAPI(lifespan=lifespan)
logging.basicConfig(level=settings.log_level)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_loaded": _MODEL_LOADED,
        "yt_dlp_version": _YT_DLP_VERSION,
    }


_SAFE_FILENAME_RE = re.compile(r"[^\w.-]")


def _safe_upload_filename(raw: str | None) -> str:
    """F1 (fix-pack): sanitise an attacker-controlled multipart filename.

    ``UploadFile.filename`` is supplied by the client; without sanitising
    we'd write to ``td / "../../etc/whisper-server/.env"`` which would
    escape the per-request temp dir on path resolution. The bot side
    already sanitises, but defence-in-depth here protects against any
    future caller / direct curl test / misconfigured ACL.
    """
    base = Path(raw or "audio.bin").name
    cleaned = _SAFE_FILENAME_RE.sub("_", base)[:128]
    return cleaned or "audio.bin"


@app.post("/transcribe")
async def transcribe(
    file: UploadFile,
    language: str = "ru",
    _: None = Depends(verify_token),
) -> dict[str, Any]:
    """Multipart upload → ffmpeg → mlx-whisper."""
    if file.size is not None and file.size > settings.max_audio_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="file too large",
        )
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        td_resolved = td.resolve()
        safe_name = _safe_upload_filename(file.filename)
        in_path = td / safe_name
        # Belt-and-suspenders: even after sanitisation the resolved path
        # MUST live inside the temp dir. A non-relative path here is a
        # critical sanitiser regression — refuse the upload loudly.
        if not in_path.resolve().is_relative_to(td_resolved):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="invalid filename",
            )
        # F9 (fix-pack): counter-based body size enforcement during the
        # upload. ``file.size`` is the Content-Length advertised by the
        # client; we still cap the actual on-disk write so a forged
        # Content-Length cannot bypass the limit.
        bytes_written = 0
        with in_path.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > settings.max_audio_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="file too large",
                    )
                fh.write(chunk)

        dur = await _ffprobe_duration(in_path)
        if dur and dur > settings.max_duration_sec:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"duration {dur:.0f}s exceeds cap "
                    f"{settings.max_duration_sec}s"
                ),
            )

        wav_path = td / "audio.wav"
        await _convert_to_wav(in_path, wav_path)
        try:
            result = await _transcribe_wav(wav_path, language)
        except Exception as exc:  # noqa: BLE001
            log.exception("transcribe_failed")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="transcription failed",
            ) from exc

    return {
        "text": (result.get("text") or "").strip(),
        "language": result.get("language", language),
        "duration": result.get("duration", dur),
        "segments": list(result.get("segments") or []),
    }


@app.post("/extract", response_model=ExtractResponse)
async def extract(
    req: ExtractRequest,
    _: None = Depends(verify_token),
) -> ExtractResponse:
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        try:
            audio_path, meta = await _yt_dlp_extract(
                str(req.url),
                td,
                timeout=settings.yt_dlp_timeout_sec,
            )
        except YtDlpDiskFullError as exc:
            raise HTTPException(
                status.HTTP_507_INSUFFICIENT_STORAGE, detail=str(exc)
            ) from exc
        except YtDlpTimeoutError as exc:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc)
            ) from exc
        except YtDlpExtractError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"extract: {str(exc)[:500]}",
            ) from exc

        duration = await _ffprobe_duration(audio_path)
        if duration and duration > req.max_duration_sec:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"duration {duration:.0f}s exceeds cap "
                    f"{req.max_duration_sec}s"
                ),
            )

        wav_path = td / "audio.wav"
        await _convert_to_wav(audio_path, wav_path)
        try:
            result = await _transcribe_wav(wav_path, req.language)
        except Exception as exc:  # noqa: BLE001
            log.exception("extract_transcribe_failed")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="transcription failed",
            ) from exc

    title = meta.get("title")
    channel = meta.get("channel") or meta.get("uploader")
    upload_date = meta.get("upload_date")
    return ExtractResponse(
        text=(result.get("text") or "").strip(),
        language=result.get("language", req.language),
        duration=float(result.get("duration", duration)),
        title=title if isinstance(title, str) else None,
        channel=channel if isinstance(channel, str) else None,
        upload_date=upload_date if isinstance(upload_date, str) else None,
        segments=[
            ExtractSegment(
                start=float(s.get("start", 0)),
                end=float(s.get("end", 0)),
                text=str(s.get("text", "")),
            )
            for s in (result.get("segments") or [])
        ],
    )
