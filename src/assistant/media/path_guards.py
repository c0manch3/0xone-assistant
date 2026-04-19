"""Shared CLI path-guard helpers (phase-7 fix-pack I3 + I7).

Before this module, the four tool CLIs under ``tools/`` each rolled
their own path-guard logic:

* ``tools/transcribe/main.py``     — strict-resolved input-file guard.
* ``tools/extract_doc/main.py``    — strict-resolved input-file guard.
* ``tools/genimage/main.py``       — to-be-created output-file guard
  with ``resolve(strict=False)`` (POSIX: does NOT collapse ``..`` when
  intermediate components don't exist, opening a subtle traversal
  hole on some filesystem layouts).
* ``tools/render_doc/main.py``     — to-be-created output-file guard
  with the correct parent-resolve + re-append pattern.

Two consolidated validators live here:

* :func:`validate_existing_input_path` — for files the CLI READS
  (transcribe / extract_doc input).
* :func:`validate_future_output_path` — for files the CLI WRITES
  (genimage / render_doc output).

Both return a successfully-validated :class:`pathlib.Path` or raise
:class:`PathGuardError` with a human-readable reason. Callers wrap
the ``PathGuardError`` into their CLI-specific exit-code envelope;
the helpers themselves never ``sys.exit`` or touch stdout/stderr.

Design rationale:

* **Stdlib-only** — phase-7 §0 constraint so the tools CLIs retain
  their "depend on stdlib + explicit deps only" posture.
* **Strict semantics** — symlink handling follows ``is_relative_to``
  after a strict parent resolve. A symlink that escapes the
  containing root is rejected; a symlink that stays INSIDE is
  allowed (the guard is concerned with the effective filesystem
  location, not the lexical spelling).
* **Non-fatal** — helpers raise rather than ``sys.exit`` so the
  caller's argparse / JSON-envelope story stays centralised in the
  CLI's own ``main()``.

The module lives under ``src/assistant/media/`` because the four
CLIs all run inside the same installed venv — the daemon installs
``assistant`` as a distribution, so ``from assistant.media.path_guards
import ...`` resolves regardless of how the CLI was launched
(direct ``python tools/<x>/main.py`` via bash hook, or
``python -m tools.<x>.main`` for tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


class PathGuardError(ValueError):
    """Raised by the guard helpers when a path fails validation.

    Inherits from ``ValueError`` so callers that already catch
    ``ValueError`` around resolve() calls get a consistent shape,
    while callers that want to distinguish guard rejections from
    other validation errors can catch ``PathGuardError`` directly.
    """


def _normalise_suffix(suffix: str) -> str:
    """Lowercase + ensure leading dot for a comparison-ready suffix.

    Accepts both ``".png"`` and ``"png"`` so caller tables stay
    ergonomic. An empty suffix is rejected — callers should pass a
    concrete allow-list, never the wildcard "any suffix".
    """
    if not suffix:
        raise ValueError("empty suffix in allow-list")
    lowered = suffix.lower()
    return lowered if lowered.startswith(".") else f".{lowered}"


def _normalise_allow_list(suffixes: Iterable[str]) -> frozenset[str]:
    out = frozenset(_normalise_suffix(s) for s in suffixes)
    if not out:
        raise ValueError("allowed_suffixes must contain at least one entry")
    return out


def validate_existing_input_path(
    raw: Path | str,
    *,
    allowed_suffixes: Iterable[str],
    max_bytes: int | None = None,
) -> Path:
    """Validate a path the CLI will READ from.

    Preconditions checked (in order):

    1. Non-empty input (empty string is rejected outright).
    2. Absolute path required — caller-relative resolution is always
       ambiguous across worker / main-turn / subprocess boundaries.
    3. ``resolve(strict=True)`` — raises ``FileNotFoundError`` /
       ``OSError`` when the path cannot be resolved; re-raised as
       :class:`PathGuardError` with a readable message.
    4. ``is_file()`` — rejects directories, sockets, FIFOs.
    5. Suffix lands inside ``allowed_suffixes`` (case-insensitive
       comparison).
    6. Optional: ``path.stat().st_size <= max_bytes``.

    Returns the resolved path on success.
    """
    if raw is None or (isinstance(raw, str) and not raw):
        raise PathGuardError("path is empty")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise PathGuardError(f"path must be absolute, got {str(raw)!r}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PathGuardError(f"path does not exist: {raw}") from exc
    except (OSError, RuntimeError) as exc:
        raise PathGuardError(f"cannot resolve path: {exc}") from exc
    if not resolved.is_file():
        raise PathGuardError(f"path is not a regular file: {resolved}")

    allow = _normalise_allow_list(allowed_suffixes)
    suffix = resolved.suffix.lower()
    if suffix not in allow:
        raise PathGuardError(
            f"unsupported extension {resolved.suffix!r}; allowed: {sorted(allow)}"
        )

    if max_bytes is not None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise PathGuardError(f"cannot stat file: {exc}") from exc
        if size > max_bytes:
            raise PathGuardError(f"file size {size} exceeds cap {max_bytes}")

    return resolved


def validate_future_output_path(
    raw: Path | str,
    *,
    root: Path,
    allowed_suffixes: Iterable[str],
) -> Path:
    """Validate a path the CLI will CREATE / OVERWRITE.

    Same safety shape as :func:`validate_existing_input_path` but
    tailored to output paths. ``resolve(strict=True)`` is NOT usable
    here because the file does not exist yet; instead we:

    1. Reject empty input / padded whitespace in the filename.
    2. Reject filenames containing path separators (``/`` / ``\\``)
       so ``--out foo/../../etc/passwd.png`` cannot escape through a
       lexical trick.
    3. ``resolve(strict=True)`` the PARENT — the daemon pre-creates
       ``<data_dir>/media/outbox/`` at startup so its parent always
       resolves strictly. Strict resolve collapses ``..`` through
       real directories; a ``..``-chain that escapes the root is
       caught by the subsequent ``is_relative_to`` test.
    4. Re-append the (validated) filename onto the resolved parent
       to produce the final path candidate.
    5. Assert ``final.is_relative_to(root.resolve())`` — catches
       traversal through symlinks as well as lexical ``..``.
    6. Suffix lands inside ``allowed_suffixes`` (case-insensitive).

    Returns the final path on success. The caller is responsible for
    creating / overwriting the file — this helper makes no FS
    modifications.
    """
    if raw is None or (isinstance(raw, str) and not raw):
        raise PathGuardError("output path is empty")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise PathGuardError(f"output path must be absolute, got {str(raw)!r}")

    name = candidate.name
    if not name or name != name.strip():
        raise PathGuardError(
            "output filename is empty or padded with whitespace"
        )
    if "/" in name or "\\" in name:
        raise PathGuardError(
            f"output filename must not contain path separators: {name!r}"
        )

    root_resolved = root.resolve()
    if not root_resolved.exists():
        raise PathGuardError(f"output root directory missing: {root_resolved}")

    try:
        parent_resolved = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathGuardError(
            f"output parent cannot be resolved: {exc}"
        ) from exc

    final = parent_resolved / name
    # `is_relative_to` catches both lexical `..` escapes (already
    # collapsed by resolve()) and symlink-based traversal because the
    # parent resolution followed the symlink to its real target.
    try:
        inside_root = final.is_relative_to(root_resolved)
    except ValueError:
        inside_root = False
    if not inside_root:
        raise PathGuardError(
            f"output path must live under {root_resolved} (got {final})"
        )

    allow = _normalise_allow_list(allowed_suffixes)
    suffix = final.suffix.lower()
    if suffix not in allow:
        raise PathGuardError(
            f"output suffix {suffix!r} unsupported; expected one of {sorted(allow)}"
        )

    return final


__all__ = [
    "PathGuardError",
    "validate_existing_input_path",
    "validate_future_output_path",
]
