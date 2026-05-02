"""Phase 9 §2.4 CRIT-5 — filename sanitisation for ``render_doc``.

The owner-supplied (or model-supplied) ``filename`` argument is
sanitised server-side BEFORE forming the artefact's
``suggested_filename``. The rule is intentionally restrictive:

  - Strip Unicode categories Cc/Cf/Co/Cs/Cn (control / format incl.
    bidi-override + ZWSP / private-use / surrogate / unassigned).
  - Reject path components (``/``, ``\\``, ``\\0``).
  - Reject leading dot or ``..`` (traversal + Unix hidden).
  - Reject trailing dot or space (Windows compat).
  - Reject Windows-reserved basenames (CON, PRN, AUX, NUL, COM1-9,
    LPT1-9), case-insensitive.
  - Reject empty, dots-only (``.``, ``..``, ``...``), too-long (>96
    codepoints).
  - Accept Cyrillic, emoji (Unicode So), spaces inside the name,
    Unicode dashes.

Caller appends ``.{fmt}`` after the sanitiser returns. The matrix in
spec §2.4 enumerates 14 rows of expected behaviour.
"""

from __future__ import annotations

import re
import unicodedata

# Cc=control, Cf=format (incl. ZWSP/ZWJ/U+202E bidi),
# Co=private-use, Cs=surrogate, Cn=unassigned.
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})

_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$",
    re.IGNORECASE,
)

_MAX_LEN = 96


class FilenameInvalid(ValueError):  # noqa: N818 - exposed name predates style rule
    """Raised by :func:`_sanitize_filename` on rejected input.

    Carries a short kebab-case ``code`` attribute the @tool body maps
    into the ``error`` envelope field (``sanitize-<code>``).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sanitize_filename(raw: str | None) -> str | None:
    """Return a cleaned filename WITHOUT extension, or ``None`` if
    ``raw`` was None / empty (caller substitutes a default).

    Raises :class:`FilenameInvalid` on rejected input. The exception's
    ``code`` attribute is one of the kebab-case identifiers documented
    in spec §2.4.
    """
    if raw is None or not raw:
        return None
    # Strip rejected unicode categories (silent — owner gets clean name).
    cleaned = "".join(
        c for c in raw if unicodedata.category(c) not in _REJECTED_CATEGORIES
    )
    cleaned = cleaned.strip()
    if not cleaned:
        raise FilenameInvalid("empty-after-normalisation")
    # W2-LOW-3: dots-only inputs (".", "..", "...") all reject as
    # dot-prefix-or-traversal — they look like dot-prefix or traversal
    # but require explicit rows in the matrix.
    if cleaned.replace(".", "") == "":
        raise FilenameInvalid("dot-prefix-or-traversal")
    # Reject path components.
    if any(sep in cleaned for sep in ("/", "\\", "\0")):
        raise FilenameInvalid("path-components")
    # Reject .. / leading dot.
    if cleaned.startswith(".") or ".." in cleaned:
        raise FilenameInvalid("dot-prefix-or-traversal")
    # Reject trailing dot/space (Windows compat).
    if cleaned[-1] in (".", " "):
        raise FilenameInvalid("trailing-dot-or-space")
    # Reject Windows-reserved basenames.
    base = cleaned.split(".", 1)[0]
    if _WINDOWS_RESERVED.match(base):
        raise FilenameInvalid("windows-reserved")
    # Length cap (codepoints, not bytes).
    if len(cleaned) > _MAX_LEN:
        raise FilenameInvalid("too-long")
    return cleaned
