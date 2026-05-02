"""Phase 9 §2.4 CRIT-5 + W2-LOW-3 — filename sanitisation matrix.

The 14-row matrix in spec §2.4 enumerates every accept / reject case
the @tool body must honour. Each row maps to one parametrised case
below.
"""

from __future__ import annotations

import pytest

from assistant.render_doc._validate_paths import (
    FilenameInvalid,
    _sanitize_filename,
)


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        # Windows-reserved, case-insensitive on basename only.
        ("CON", "windows-reserved"),
        ("con.report", "windows-reserved"),
        ("LPT5", "windows-reserved"),
        ("COM1.pdf", "windows-reserved"),
        # Path components — slash/backslash hits before dot check.
        ("../etc/passwd", "path-components"),
        ("/abs/path", "path-components"),
        ("a\\b", "path-components"),
        # Leading dot / dots-only (W2-LOW-3).
        (".hidden", "dot-prefix-or-traversal"),
        (".", "dot-prefix-or-traversal"),
        ("..", "dot-prefix-or-traversal"),
        ("...", "dot-prefix-or-traversal"),
        ("....", "dot-prefix-or-traversal"),
        # Trailing dot/space (note: ``report `` with trailing space
        # is .strip()'d to ``report`` and ACCEPTED — not rejected.
        # Trailing space INSIDE the name (post-strip) is what the
        # rule rejects).
        ("report .", "trailing-dot-or-space"),
        ("report.", "trailing-dot-or-space"),
        # Length cap (>96 codepoints).
        ("a" * 97, "too-long"),
        # Empty after normalisation.
        ("\x00", "empty-after-normalisation"),
    ],
)
def test_rejected_inputs(raw: str, expected_code: str) -> None:
    with pytest.raises(FilenameInvalid) as ei:
        _sanitize_filename(raw)
    assert ei.value.code == expected_code, (
        f"Input {raw!r} expected code {expected_code!r}, "
        f"got {ei.value.code!r}"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Cyrillic + spaces — accepted.
        ("отчёт vault 2026", "отчёт vault 2026"),
        # Emoji (Unicode category So) — accepted.
        ("📊отчёт", "📊отчёт"),
        # ZWSP (U+200B, Cf) and bidi-override (U+202E, Cf) silently
        # stripped.
        ("a\u200Bb", "ab"),
        ("report\u202Efdp", "reportfdp"),
        # report.con — basename "report" is not reserved, accept.
        ("report.con", "report.con"),
        # Reserved-only on basename: "file.con" base "file" not reserved.
        ("notes.csv.report", "notes.csv.report"),
        # Length boundary (96 codepoints exactly).
        ("a" * 96, "a" * 96),
    ],
)
def test_accepted_inputs(raw: str, expected: str) -> None:
    assert _sanitize_filename(raw) == expected


def test_null_byte_stripped_then_accepted() -> None:
    """``\\0`` is Unicode category Cc; stripped silently. Remaining
    text accepted if otherwise valid."""
    assert _sanitize_filename("a\0b") == "ab"


def test_trailing_space_stripped_then_accepted() -> None:
    """``report `` post-strip → ``report`` accepted. Strict
    trailing-space reject only fires when the trailing space
    survives ``.strip()`` — i.e. via interior whitespace plus a
    trailing dot/space (e.g. ``report .``)."""
    assert _sanitize_filename("report ") == "report"


def test_none_or_empty_returns_none() -> None:
    """Caller substitutes a default filename when sanitiser returns
    None — empty input is NOT a rejection."""
    assert _sanitize_filename(None) is None
    assert _sanitize_filename("") is None
