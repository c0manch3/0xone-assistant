"""genimage CLI — stdlib-only HTTP client for the mflux-server on the Mac host.

Phase-7 thin client. Generates an image by POSTing a JSON body to the
local mflux endpoint (reached via SSH reverse tunnel on
``127.0.0.1:<port>``). The server streams back a PNG which we write
atomically to ``--out``.

The CLI enforces a per-day invocation quota at
``<data_dir>/run/genimage-quota.json`` using ``fcntl.flock(LOCK_EX)``
for race safety. Spike S-5 (R-3) verifies that 10 concurrent workers
competing for the same quota observe exactly one winner.

Exit codes:
    0 — success
    2 — argv / validation (required flags missing, bad enum, etc.)
    3 — path-guard (--out outside outbox, unsafe traversal, wrong ext)
    4 — network (endpoint unreachable, timeout, HTTP error)
    5 — unknown (unhandled exception)
    6 — quota exceeded (daily cap hit)

Usage::

    python tools/genimage/main.py \\
        --prompt "закат над морем" \\
        --out /abs/data/media/outbox/<uuid>.png \\
        [--width 1024] [--height 1024] [--steps 8] [--seed N] \\
        [--timeout-s 120] [--endpoint URL] [--daily-cap N] [--quota-file PATH]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Make the package importable whether invoked via
# `python tools/genimage/main.py` or `python -m tools.genimage.main`.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from assistant.media.path_guards import (  # noqa: E402
    PathGuardError,
    validate_future_output_path,
)
from tools.genimage._net_mirror import is_loopback_only  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PATH = 3
EXIT_NETWORK = 4
EXIT_UNKNOWN = 5
EXIT_QUOTA = 6

_ALLOWED_SIZES = frozenset({256, 512, 768, 1024})
_STEPS_MIN, _STEPS_MAX = 1, 20
_SEED_MIN, _SEED_MAX = 0, 2**31 - 1
_TIMEOUT_MIN_S, _TIMEOUT_MAX_S = 30, 600
_PROMPT_MAX_BYTES = 1024
_DEFAULT_ENDPOINT = "http://localhost:9101/generate"
_DEFAULT_DAILY_CAP = 1
_DEFAULT_TIMEOUT_S = 120
_DEFAULT_STEPS = 8
_DEFAULT_WIDTH = 1024
_DEFAULT_HEIGHT = 1024
_RESPONSE_MAX_BYTES = 25 * 1024 * 1024  # safety cap; mflux PNGs are ~2-5 MB


# ----------------------------------------------------------- exit helpers


def _print_err(msg: str) -> None:
    sys.stderr.write(msg.rstrip("\n") + "\n")


def _exit_json(code: int, payload: dict[str, Any]) -> int:
    """Emit a compact JSON result on stdout and return ``code``.

    Keeps CLI output machine-parseable for the worker subagent and
    `tools/task/main.py` which capture stdout verbatim.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


# ----------------------------------------------------------- argv helpers


def _positive_int(raw: str, *, name: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"{name}: expected integer, got {raw!r}"
        ) from exc
    if value < lo or value > hi:
        raise argparse.ArgumentTypeError(
            f"{name}: {value} out of range [{lo}, {hi}]"
        )
    return value


def _width_height(raw: str) -> int:
    value = _positive_int(raw, name="dimension", lo=256, hi=1024)
    if value not in _ALLOWED_SIZES:
        raise argparse.ArgumentTypeError(
            f"dimension must be one of {sorted(_ALLOWED_SIZES)}, got {value}"
        )
    return value


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="genimage",
        description=(
            "Generate an image via mflux over a loopback HTTP endpoint. "
            "Writes the resulting PNG to --out."
        ),
    )
    p.add_argument("--prompt", required=True, help="UTF-8 prompt, ≤1024 bytes, no newlines")
    p.add_argument("--out", required=True, help="absolute path to write the PNG")
    p.add_argument(
        "--width",
        type=_width_height,
        default=_DEFAULT_WIDTH,
        help="image width (one of 256/512/768/1024; default 1024)",
    )
    p.add_argument(
        "--height",
        type=_width_height,
        default=_DEFAULT_HEIGHT,
        help="image height (one of 256/512/768/1024; default 1024)",
    )
    p.add_argument(
        "--steps",
        type=lambda s: _positive_int(s, name="--steps", lo=_STEPS_MIN, hi=_STEPS_MAX),
        default=_DEFAULT_STEPS,
        help="mflux sampling steps (1..20; default 8)",
    )
    p.add_argument(
        "--seed",
        type=lambda s: _positive_int(s, name="--seed", lo=_SEED_MIN, hi=_SEED_MAX),
        default=None,
        help="RNG seed (0..2^31-1). Omit to let the server pick.",
    )
    p.add_argument(
        "--timeout-s",
        type=lambda s: _positive_int(
            s, name="--timeout-s", lo=_TIMEOUT_MIN_S, hi=_TIMEOUT_MAX_S
        ),
        default=_DEFAULT_TIMEOUT_S,
        help=f"request timeout in seconds (default {_DEFAULT_TIMEOUT_S})",
    )
    p.add_argument(
        "--endpoint",
        default=os.environ.get("MEDIA_GENIMAGE_ENDPOINT", _DEFAULT_ENDPOINT),
        help="mflux HTTP endpoint (must resolve to loopback only)",
    )
    p.add_argument(
        "--daily-cap",
        type=lambda s: _positive_int(s, name="--daily-cap", lo=0, hi=10_000),
        default=None,
        help="override daily invocation cap (default: env MEDIA_GENIMAGE_DAILY_CAP or 1)",
    )
    p.add_argument(
        "--quota-file",
        default=None,
        help=(
            "explicit path to the quota file "
            "(default: $ASSISTANT_DATA_DIR/run/genimage-quota.json)"
        ),
    )
    return p


# ----------------------------------------------------------- path guards


def _validate_prompt(prompt: str) -> str | None:
    """Return an error message iff the prompt is not a well-formed request.

    * Must be UTF-8 encodable.
    * Must not contain newline characters (avoids accidental multiline
      HTTP header injection on the server side).
    * Must be ≤1024 UTF-8 bytes.
    """
    if not prompt:
        return "prompt is empty"
    if "\n" in prompt or "\r" in prompt:
        return "prompt must not contain newlines"
    try:
        encoded = prompt.encode("utf-8")
    except UnicodeEncodeError:
        return "prompt is not UTF-8 encodable"
    if len(encoded) > _PROMPT_MAX_BYTES:
        return f"prompt exceeds {_PROMPT_MAX_BYTES} UTF-8 bytes ({len(encoded)} given)"
    return None


def _data_dir() -> Path:
    """Resolve the project data directory without importing src/assistant."""
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _outbox_root() -> Path:
    override = os.environ.get("MEDIA_OUTBOX_DIR")
    if override:
        return Path(override).resolve()
    return (_data_dir() / "media" / "outbox").resolve()


def _validate_out_path(raw: str) -> tuple[Path | None, str | None]:
    """Return (resolved_path, error).

    Fix-pack I3 + I7 (phase-7): delegates to the shared
    :func:`assistant.media.path_guards.validate_future_output_path`.
    The shared helper uses a strict parent-resolve + re-append +
    ``is_relative_to`` combo that correctly handles ``..`` in the
    middle of the path AND symlinks in parent components — neither
    worked with the previous ``resolve(strict=False)`` implementation
    on POSIX platforms (where ``resolve(strict=False)`` does NOT
    collapse ``..`` through non-existent directory components).

    Additional genimage-specific contract retained here:

    * Suffix is ``.png`` only (mflux produces PNG; the allow-list is
      narrower than ``validate_future_output_path`` allows for by
      its generic design).
    * The target MUST NOT already exist — genimage policy is "write
      fresh PNG, never overwrite" so a caller asking for an extant
      path is rejected. ``validate_future_output_path`` deliberately
      does NOT embed that policy (render_doc overwrites via
      ``os.replace``); genimage enforces it here.
    """
    try:
        final = validate_future_output_path(
            raw,
            root=_outbox_root(),
            allowed_suffixes={".png"},
        )
    except PathGuardError as exc:
        return None, f"--out {exc}"
    if final.exists():
        return None, f"--out already exists, refuse to overwrite: {final}"
    return final, None


# ----------------------------------------------------------- quota


def _quota_file_path(explicit: str | None) -> Path:
    """Resolve the quota file path per env / flag precedence."""
    if explicit:
        return Path(explicit)
    override = os.environ.get("MEDIA_GENIMAGE_QUOTA_FILE")
    if override:
        return Path(override)
    return _data_dir() / "run" / "genimage-quota.json"


def _effective_daily_cap(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    env = os.environ.get("MEDIA_GENIMAGE_DAILY_CAP")
    if env is not None:
        try:
            parsed = int(env)
        except ValueError:
            parsed = _DEFAULT_DAILY_CAP
        return max(parsed, 0)
    return _DEFAULT_DAILY_CAP


def _today_utc() -> str:
    # UTC is deliberate — the VPS may be in any tz and we want the quota
    # rollover to be predictable / documented. S-5 R-4 notes clock
    # rollback across midnight as a known ±1 jitter.
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _check_and_increment_quota(
    path: Path, cap: int, *, today: str | None = None
) -> tuple[bool, dict[str, Any]]:
    """Atomically reserve one slot of the daily cap.

    Schema of the quota file::

        {"date": "YYYY-MM-DD", "count": N}

    Serialization: ``fcntl.flock(fd, LOCK_EX)`` held across read →
    mutate → write, exactly mirroring the S-5 spike algorithm (R-3
    passes with 10 concurrent workers).

    Returns ``(allowed, state_after)``:
      * ``allowed=True`` — count was below cap and has been bumped;
        caller MUST proceed.
      * ``allowed=False`` — cap reached; state_after reflects the
        unchanged file contents (count == cap).

    ``today`` may be injected for deterministic tests; production
    callers leave it ``None`` (→ ``_today_utc()``).
    """
    today_str = today or _today_utc()
    path.parent.mkdir(parents=True, exist_ok=True)

    # A cap of 0 means "disabled for this invocation" — we deny without
    # touching the file, which keeps the cap=0 override visible (useful
    # for tests and for operators temporarily disabling the feature).
    state: dict[str, Any]
    if cap <= 0:
        state = _read_quota_best_effort(path)
        return False, state

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.lseek(fd, 0, os.SEEK_SET)
        # 4 KiB is generous — the JSON blob is ~40 bytes.
        raw = os.read(fd, 4096)
        try:
            state = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Corrupt quota file — reset conservatively. Losing a count
            # on corruption is preferable to refusing all requests
            # forever.
            state = {}
        # X-1 guard: a disk-fill mid-write or hand-edit can produce a
        # well-formed JSON list/scalar that parses cleanly but breaks
        # the subsequent `state.get(...)` calls. Treat any non-dict
        # payload as absent, symmetric with `_read_quota_best_effort`.
        if not isinstance(state, dict):
            state = {}

        if state.get("date") != today_str:
            state = {"date": today_str, "count": 0}

        count = int(state.get("count", 0))
        if count >= cap:
            return False, {"date": today_str, "count": count, "cap": cap}

        state["count"] = count + 1

        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(state).encode("utf-8"))
        os.fsync(fd)
        return True, {"date": today_str, "count": state["count"], "cap": cap}
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_quota_best_effort(path: Path) -> dict[str, Any]:
    """Return the quota state without locking; used only for diagnostics."""
    try:
        # X-2: read_bytes + decode(errors="replace") mirrors the locked
        # write-path's tolerance for non-UTF-8 bytes. A partial fsync
        # after a crash can leave arbitrary bytes on disk; we must never
        # let that raise UnicodeDecodeError out of a diagnostic helper.
        raw = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    # Be lenient: if the file exists but is shaped as a list / scalar
    # (shouldn't happen in practice, but corruption is a known risk),
    # treat it as an empty record so callers get a consistent type.
    return parsed if isinstance(parsed, dict) else {}


# ----------------------------------------------------------- HTTP


def _post_image(
    *,
    endpoint: str,
    payload: dict[str, Any],
    timeout_s: int,
) -> tuple[bytes, dict[str, Any] | None]:
    """POST JSON, return (image_bytes, meta_dict).

    The mflux server returns the raw PNG bytes by default (simpler than
    streaming multipart back). Optional response headers carry metadata:

      * ``X-Image-Seed``      — RNG seed actually used (int as string)
      * ``X-Image-Width``     — width  (int as string)
      * ``X-Image-Height``    — height (int as string)

    ``Content-Length`` is honoured and clamped at
    ``_RESPONSE_MAX_BYTES`` to prevent a compromised tunnel from
    exhausting the VPS disk.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "image/png",
        "User-Agent": "0xone-genimage/1.0",
    }
    req = Request(endpoint, data=body, method="POST", headers=headers)
    with urlopen(req, timeout=timeout_s) as resp:
        content_length_header = resp.headers.get("Content-Length")
        declared = int(content_length_header) if content_length_header else None
        if declared is not None and declared > _RESPONSE_MAX_BYTES:
            raise _ResponseTooLargeError(declared)
        image = resp.read(_RESPONSE_MAX_BYTES + 1)
        if len(image) > _RESPONSE_MAX_BYTES:
            raise _ResponseTooLargeError(len(image))
        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image/png" not in content_type:
            raise _UnexpectedContentTypeError(content_type or "<missing>")
        meta = {
            "seed": resp.headers.get("X-Image-Seed"),
            "width": resp.headers.get("X-Image-Width"),
            "height": resp.headers.get("X-Image-Height"),
        }
        return image, meta


class _ResponseTooLargeError(Exception):
    """Server returned more bytes than ``_RESPONSE_MAX_BYTES``."""

    def __init__(self, size: int) -> None:
        super().__init__(f"response exceeds {_RESPONSE_MAX_BYTES} bytes (got {size})")


class _UnexpectedContentTypeError(Exception):
    """Server did not return an image/png body."""

    def __init__(self, content_type: str) -> None:
        super().__init__(f"expected image/png, got {content_type!r}")


# ----------------------------------------------------------- atomic write


def _atomic_write_png(dest: Path, data: bytes) -> None:
    """Write ``data`` to ``dest`` via a same-directory tempfile + rename.

    os.replace() is atomic on POSIX when source and destination share a
    filesystem. The tempfile is created in ``dest.parent`` so we never
    cross a mount boundary. The ``.png`` suffix is preserved on the
    final name but not on the temp (mktemp guarantees uniqueness).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".genimage-", suffix=".tmp", dir=str(dest.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ----------------------------------------------------------- main flow


def _run(args: argparse.Namespace) -> int:
    # 1. Argv-level guards first — cheap, no side effects.
    prompt_err = _validate_prompt(args.prompt)
    if prompt_err:
        _print_err(prompt_err)
        return EXIT_USAGE

    out_path, out_err = _validate_out_path(args.out)
    if out_err or out_path is None:
        _print_err(out_err or "invalid --out")
        return EXIT_PATH

    # 2. Endpoint SSRF guard — loopback-only. Pitfall #5.
    loopback_ok, loopback_reason = is_loopback_only(args.endpoint)
    if not loopback_ok:
        _print_err(f"endpoint rejected (not loopback-only): {loopback_reason}")
        return EXIT_PATH

    # 3. Daily quota reservation BEFORE any network I/O. Exit 6 is
    #    cheaper than exit 4 when the cap is reached — we don't want
    #    to load the mflux server with work that we'd discard.
    cap = _effective_daily_cap(args.daily_cap)
    quota_path = _quota_file_path(args.quota_file)
    allowed, quota_state = _check_and_increment_quota(quota_path, cap)
    if not allowed:
        return _exit_json(
            EXIT_QUOTA,
            {
                "ok": False,
                "reason": "daily quota exceeded",
                "quota": quota_state,
            },
        )

    # 4. Build request payload. ``--seed`` is omitted when None so the
    #    server can pick its own. We intentionally pass unknown-to-us
    #    keys through verbatim if they're present in env overrides —
    #    but we don't support any right now.
    payload: dict[str, Any] = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
    }
    if args.seed is not None:
        payload["seed"] = args.seed

    # 5. POST. Any HTTP / network error counts as exit 4. Note: if this
    #    fails, the quota has already been spent — this is the correct
    #    behaviour for a rate-limited external service (you paid the
    #    slot; too bad). A future enhancement could roll back the
    #    counter on transient failures, but that reintroduces the race
    #    we're trying to avoid.
    try:
        image_bytes, meta = _post_image(
            endpoint=args.endpoint,
            payload=payload,
            timeout_s=args.timeout_s,
        )
    except HTTPError as exc:
        _print_err(f"server returned HTTP {exc.code}: {exc.reason}")
        return EXIT_NETWORK
    except URLError as exc:
        _print_err(f"endpoint unreachable: {exc.reason}")
        return EXIT_NETWORK
    except TimeoutError:
        _print_err(f"timeout after {args.timeout_s}s")
        return EXIT_NETWORK
    except _ResponseTooLargeError as exc:
        _print_err(str(exc))
        return EXIT_NETWORK
    except _UnexpectedContentTypeError as exc:
        _print_err(str(exc))
        return EXIT_NETWORK
    except OSError as exc:
        # Socket-level errors that don't surface as URLError (e.g.
        # connection-reset mid-response on some kernels).
        _print_err(f"network I/O error: {exc}")
        return EXIT_NETWORK

    # 6. Persist atomically. If the write fails we still have to return
    #    an error — the image is lost because the server doesn't
    #    support retrieve-by-id yet. Exit 5 because it's a local I/O
    #    problem, not a network failure.
    try:
        _atomic_write_png(out_path, image_bytes)
    except OSError as exc:
        _print_err(f"failed to write {out_path}: {exc}")
        return EXIT_UNKNOWN

    # 7. Emit machine-readable success payload.
    result: dict[str, Any] = {
        "ok": True,
        "path": str(out_path),
        "width": args.width,
        "height": args.height,
        "steps": args.steps,
        "size_bytes": len(image_bytes),
        "quota": quota_state,
    }
    if meta:
        # Pass through whatever the server reported, coerced to int
        # where we can for easier downstream consumption.
        for key in ("seed", "width", "height"):
            raw_value = meta.get(key)
            if raw_value is None:
                continue
            try:
                result[f"server_{key}"] = int(raw_value)
            except (TypeError, ValueError):
                result[f"server_{key}"] = raw_value
    if args.seed is not None:
        result["seed"] = args.seed
    return _exit_json(EXIT_OK, result)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits with code 2 for usage; preserve it.
        return int(exc.code) if exc.code is not None else EXIT_USAGE

    # Defensive outer try — any unexpected exception gets mapped to
    # exit 5 with a one-line message on stderr + full traceback gated
    # by GENIMAGE_DEBUG. We deliberately do NOT print the traceback by
    # default because the CLI output is captured by the worker
    # subagent and fed into the model — leaking stack frames poisons
    # the context.
    try:
        return _run(args)
    except Exception as exc:
        _print_err(f"unexpected error: {type(exc).__name__}: {exc}")
        if os.environ.get("GENIMAGE_DEBUG"):
            traceback.print_exc(file=sys.stderr)
        return EXIT_UNKNOWN


if __name__ == "__main__":  # pragma: no cover - exec entrypoint
    sys.exit(main())
