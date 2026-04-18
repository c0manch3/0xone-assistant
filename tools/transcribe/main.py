"""transcribe CLI — HTTP thin client for mlx-whisper (phase 7).

Invoked by the model via the Bash allowlist:

    python tools/transcribe/main.py <path> [--language ru|en|auto]
                                           [--timeout-s N]
                                           [--format text|segments]
                                           [--endpoint URL]

The heavy work (mlx-whisper on Apple Silicon) lives on a host Mac and is
reached via SSH reverse tunnel on `127.0.0.1:9100`. This CLI is a
stdlib-only POST client that streams the audio file as `multipart/form-data`
and prints the upstream JSON response on stdout.

Exit codes (tight, mirror plan §2.7 / §3.1):
    0  OK
    2  argv invalid (bad flag, missing path, bad language/format/timeout)
    3  path-guard failed (not absolute, bad extension, too large, missing,
                           outside inbox/project-root, symlink escape)
    4  network (endpoint unreachable, HTTP >=400, upstream timeout)
    5  unknown (unexpected exception class — should never fire)

Design rationale:
    * stdlib-only. urllib + mimetypes + email.message + ipaddress cover
      the full surface. No httpx/requests import so the CLI can be
      invoked under the narrowest possible venv (matches the phase-7
      pattern used by `tools/ping/`, `tools/memory/`, `tools/schedule/`).
    * Endpoint guard delegates to `is_loopback_only`, NOT to
      `classify_url`. S-1 spike proved `classify_url` permits `10.x`
      and `192.168.x` which are unreachable in the phase-7 architecture
      (host accepts only `127.0.0.1:<port>` via reverse tunnel).
    * `--endpoint` is accepted from env (`MEDIA_TRANSCRIBE_ENDPOINT`)
      OR argv; argv wins. Default points at `http://localhost:9100/
      transcribe` matching `MediaSettings.transcribe_endpoint`.
    * Multipart is built with `email.message.EmailMessage` to get a
      correct boundary + headers without pulling `requests`. `urlopen`
      accepts any `bytes`-bodied `Request` — we feed it the serialized
      form directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Phase-7 (Q9a tech-debt close): tools are a real Python sub-package.
# Short sys.path pragma so launching via `python tools/transcribe/main.py`
# (without `-m`) still imports `tools.transcribe._net_mirror`.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.transcribe._net_mirror import is_loopback_only  # noqa: E402

# --- Exit codes --------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PATH = 3
EXIT_NETWORK = 4
EXIT_UNKNOWN = 5

# --- Constants (mirror MediaSettings defaults; CLI does NOT import Settings
# to stay stdlib-only and avoid TELEGRAM_BOT_TOKEN env requirement) --------

_ALLOWED_EXT: frozenset[str] = frozenset({".oga", ".ogg", ".mp3", ".wav", ".m4a", ".flac"})
_ALLOWED_LANG: frozenset[str] = frozenset({"ru", "en", "auto"})
_ALLOWED_FORMAT: frozenset[str] = frozenset({"text", "segments"})
_DEFAULT_ENDPOINT = "http://localhost:9100/transcribe"
_DEFAULT_TIMEOUT_S = 60
_MIN_TIMEOUT_S = 10
_MAX_TIMEOUT_S = 300
_MAX_INPUT_BYTES_DEFAULT = 25_000_000  # 25 MB — MEDIA_TRANSCRIBE_MAX_INPUT_BYTES


# --- Output helpers ---------------------------------------------------------


def _emit_error(code: int, error: str, **extra: Any) -> int:
    """Print a one-line JSON error on stderr and return the exit code.

    Matches the house convention from `tools/memory/main.py::_fail`. Keeps
    stdout clean for the happy-path payload so callers can pipe to `jq`.
    """
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


# --- argv parsing -----------------------------------------------------------


def _parse_argv(argv: list[str]) -> argparse.Namespace:
    """Parse CLI argv. Raises `SystemExit(EXIT_USAGE)` on bad flags."""
    parser = argparse.ArgumentParser(
        prog="transcribe",
        description="HTTP thin client for mlx-whisper (phase-7 media CLI).",
    )
    parser.add_argument("path", help="absolute path to audio file")
    parser.add_argument(
        "--language",
        default=os.environ.get("MEDIA_TRANSCRIBE_LANGUAGE_DEFAULT", "auto"),
        choices=sorted(_ALLOWED_LANG),
        help="Transcription language hint (default: auto).",
    )
    parser.add_argument(
        "--timeout-s",
        type=int,
        default=int(os.environ.get("MEDIA_TRANSCRIBE_TIMEOUT_S", _DEFAULT_TIMEOUT_S)),
        help=f"HTTP timeout, {_MIN_TIMEOUT_S}..{_MAX_TIMEOUT_S} s (default: {_DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--format",
        default="text",
        choices=sorted(_ALLOWED_FORMAT),
        help="Upstream output format (default: text).",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("MEDIA_TRANSCRIBE_ENDPOINT", _DEFAULT_ENDPOINT),
        help="HTTP endpoint (loopback-only; default: %(default)s).",
    )
    # `argparse` raises SystemExit(2) internally on bad argv — that IS
    # the convention phase-7 expects (EXIT_USAGE == 2), so we don't
    # intercept.
    args = parser.parse_args(argv)
    if not (_MIN_TIMEOUT_S <= args.timeout_s <= _MAX_TIMEOUT_S):
        parser.error(
            f"--timeout-s must be in [{_MIN_TIMEOUT_S}, {_MAX_TIMEOUT_S}], got {args.timeout_s}"
        )
    return args


# --- Validation -------------------------------------------------------------


def _validate_path(raw: str) -> tuple[Path | None, str | None]:
    """Absolute-path + extension + size check. Returns `(path, None)` on OK.

    Phase-2 PreToolUse hooks also vet bash argv; this CLI-level check is
    defence-in-depth (per plan §3.1 "Path-guard" bullet). Requiring
    absolute paths dodges cwd-drift ambiguity between worker and main
    turn.
    """
    if not raw:
        return None, "path is empty"
    candidate = Path(raw)
    if not candidate.is_absolute():
        return None, f"path must be absolute, got {raw!r}"
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return None, f"path does not exist: {raw}"
    except (OSError, RuntimeError) as exc:
        return None, f"cannot resolve path: {exc}"
    if not resolved.is_file():
        return None, f"path is not a regular file: {resolved}"
    if resolved.suffix.lower() not in _ALLOWED_EXT:
        return None, (f"unsupported extension {resolved.suffix!r}; allowed: {sorted(_ALLOWED_EXT)}")
    max_bytes = _resolve_max_input_bytes()
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, f"cannot stat file: {exc}"
    if size > max_bytes:
        return None, f"file size {size} exceeds cap {max_bytes}"
    return resolved, None


def _resolve_max_input_bytes() -> int:
    raw = os.environ.get("MEDIA_TRANSCRIBE_MAX_INPUT_BYTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _MAX_INPUT_BYTES_DEFAULT


def _validate_endpoint(url: str) -> str | None:
    """Return `None` iff the endpoint is loopback-only; otherwise a reason.

    S-1 spike: DO NOT use `classify_url` — it permits `10.x` / `192.168.x`
    which are unreachable via the phase-7 reverse tunnel. `is_loopback_only`
    is the strictly stricter rule and is what we want here.
    """
    try:
        ok, reason = asyncio.run(is_loopback_only(url))
    except RuntimeError as exc:
        # Defensive: asyncio.run is called from a sync context so this
        # shouldn't fire in production — but if the CLI is ever wrapped
        # in an async harness, surface the failure cleanly instead of
        # trapping as EXIT_UNKNOWN.
        return f"loopback probe failed: {exc}"
    if not ok:
        return reason
    return None


# --- Multipart encoding -----------------------------------------------------


def _encode_multipart(
    audio_path: Path,
    language: str,
    fmt: str,
) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body from three fields.

    Returns `(body_bytes, content_type_with_boundary)`. Uses
    `email.message.EmailMessage` so the boundary is generated correctly
    and the field-name escaping is the stdlib's responsibility (not
    hand-rolled). The audio payload rides as an `application/octet-stream`
    part when `mimetypes.guess_type` can't infer the mime (e.g. `.oga`
    on some hosts).

    Field names align with the documented mlx-whisper HTTP contract
    (plan §3.1: `file`, `language`, `format`).
    """
    msg = EmailMessage()
    msg["MIME-Version"] = "1.0"
    # Use a fresh boundary per call — urllib doesn't care, but a stable
    # uuid avoids any collision with payload bytes (vanishingly unlikely
    # yet cheap to prevent).
    boundary = f"----transcribe-{uuid.uuid4().hex}"
    msg.set_type("multipart/form-data")
    msg.set_boundary(boundary)

    # language field
    lang_part = EmailMessage()
    lang_part.add_header("Content-Disposition", "form-data", name="language")
    lang_part.set_payload(language)
    msg.attach(lang_part)

    # format field
    fmt_part = EmailMessage()
    fmt_part.add_header("Content-Disposition", "form-data", name="format")
    fmt_part.set_payload(fmt)
    msg.attach(fmt_part)

    # file field
    guessed_type, _enc = mimetypes.guess_type(audio_path.name)
    main_type, sub_type = (
        guessed_type.split("/", 1) if guessed_type else ("application", "octet-stream")
    )
    try:
        audio_bytes = audio_path.read_bytes()
    except OSError as exc:
        # Surface up to the caller — main() converts to EXIT_PATH.
        raise _AudioReadError(str(exc)) from exc
    file_part = EmailMessage()
    file_part.add_header(
        "Content-Disposition",
        "form-data",
        name="file",
        filename=audio_path.name,
    )
    file_part.set_payload(audio_bytes)
    file_part.set_type(f"{main_type}/{sub_type}")
    # `set_payload` with binary on an `EmailMessage` needs explicit CTE
    # `binary` so the generator doesn't base64-encode it. urllib sends
    # raw bytes — encoding would corrupt the audio.
    file_part["Content-Transfer-Encoding"] = "binary"
    msg.attach(file_part)

    # Serialize. `as_bytes(policy=...)` would give us CRLF line endings
    # which is what HTTP expects; the default policy emits `\n` and
    # breaks picky servers. `email.policy.HTTP` is the drop-in for this
    # case.
    from email import policy

    body = msg.as_bytes(policy=policy.HTTP)
    # Strip the leading envelope headers — multipart body starts at the
    # first `--boundary`. `email` embeds the `MIME-Version` and
    # `Content-Type` headers that we don't want on the wire (urllib adds
    # its own Content-Type header).
    split_marker = b"\r\n\r\n"
    sep = body.find(split_marker)
    if sep == -1:
        raise _MultipartEncodeError("could not locate header/body split")
    payload = body[sep + len(split_marker) :]
    content_type = f"multipart/form-data; boundary={boundary}"
    return payload, content_type


class _AudioReadError(Exception):
    """Raised when the CLI can't read the input audio file."""


class _MultipartEncodeError(Exception):
    """Raised on a multipart encoding invariant break."""


# --- HTTP call --------------------------------------------------------------


def _http_post(
    endpoint: str,
    body: bytes,
    content_type: str,
    timeout_s: int,
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """POST `body` to `endpoint`. Returns `(status, data, error_reason)`.

    Only one of (data, error_reason) is non-None at a time. `status` is
    `None` if we never got a reply (URLError / timeout).
    """
    req = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": "0xone-assistant-transcribe/1",
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            status = resp.status
    except HTTPError as exc:
        return exc.code, None, f"upstream returned HTTP {exc.code}"
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return None, None, f"endpoint unreachable: {reason}"
    except TimeoutError:
        return None, None, f"timeout after {timeout_s}s"

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return status, None, f"upstream returned non-JSON: {exc}"
    if not isinstance(data, dict):
        return status, None, f"upstream returned non-object JSON: {type(data).__name__}"
    return status, data, None


# --- main() -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. See module docstring for exit-code contract."""
    try:
        args = _parse_argv(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        # argparse's SystemExit. Normalise non-2 codes too.
        return int(exc.code) if exc.code is not None else EXIT_USAGE

    path, path_err = _validate_path(args.path)
    if path_err is not None or path is None:
        return _emit_error(EXIT_PATH, path_err or "path validation failed")

    endpoint_err = _validate_endpoint(args.endpoint)
    if endpoint_err is not None:
        # Endpoint mis-config is argv-level (operator-provided), hence
        # EXIT_USAGE rather than EXIT_PATH. Matches the convention that
        # argparse's own errors exit 2.
        return _emit_error(EXIT_USAGE, f"endpoint rejected: {endpoint_err}")

    try:
        body, content_type = _encode_multipart(path, args.language, args.format)
    except _AudioReadError as exc:
        return _emit_error(EXIT_PATH, f"cannot read audio: {exc}")
    except _MultipartEncodeError as exc:  # pragma: no cover — invariant break
        return _emit_error(EXIT_UNKNOWN, f"multipart encode failed: {exc}")

    try:
        status, data, net_err = _http_post(args.endpoint, body, content_type, args.timeout_s)
    except Exception as exc:
        return _emit_error(
            EXIT_UNKNOWN,
            f"unexpected error during POST: {type(exc).__name__}: {exc}",
        )

    if net_err is not None:
        return _emit_error(EXIT_NETWORK, net_err, status=status)
    assert data is not None  # mypy: invariant from _http_post

    # Pass the upstream payload through verbatim. Keeping it intact means
    # the model doesn't have to re-guess what the server returned.
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
