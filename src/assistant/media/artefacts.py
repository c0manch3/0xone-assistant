"""Artefact regex + classifier for media delivery (phase 7).

The model, when instructed via SKILL.md §4.5, ends its reply with
one or more absolute paths under `<data_dir>/media/outbox/`.
`adapters/dispatch_reply.py` extracts those paths, path-guards them,
and routes each via the appropriate `MessengerAdapter.send_*`
method.

The regex is the **v3** variant from phase-7 spike S-2 (corpus
43/46 — three known acceptable residual failures documented in
`spikes/phase7_s2_report.json`). Earlier variants:

  * **v1** (pitfall #2) — matched URLs with `.png` paths as
    artefacts; matched relative `./outbox/x.png`; over-extended
    across adjacent paths. REJECTED.
  * **v2** — fixed URL false positive and adjacency, but still
    matched IPv6-URL inline paths. REJECTED.
  * **v3** (this module) — widens the negative lookbehind to also
    reject a leading `.` / `:` (URL scheme residues) and adds `/`
    to the stop-set lookahead for cleaner adjacent-path handling.

The regex is NOT a path-guard; it only extracts candidates. The
caller MUST additionally:
  1. `resolve()` the candidate.
  2. Assert it `is_relative_to(outbox_root_resolved)`.
  3. Assert `path.exists()` — regex v3 may extract
     `/abs/outbox/x.png` from `/abs/outbox/x.png/y` (pitfall #10);
     only `exists()` distinguishes real artefacts from trailing
     noise.

See `plan/phase7/implementation.md` §2.5 for the canonical spec and
§0 pitfall #2 for the rationale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

# Allowed artefact extensions, grouped by dispatch destination.
# Ordering within each group is insignificant to the regex; the
# classifier uses set-membership below.
_PHOTO_EXT: Final[tuple[str, ...]] = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO_EXT: Final[tuple[str, ...]] = (
    ".mp3",
    ".ogg",
    ".oga",
    ".wav",
    ".m4a",
    ".flac",
)
_DOC_EXT: Final[tuple[str, ...]] = (
    ".pdf",
    ".docx",
    ".txt",
    ".xlsx",
    ".rtf",
)
_ALL_EXT: Final[tuple[str, ...]] = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

# --- v3 regex (spike S-2 verified) ---------------------------------
#
# Structure (left-to-right):
#
#   (?<![\w/.:])           -- negative lookbehind rejecting `.` / `:`
#                             (URL scheme residues like
#                             "http://host.com/abs/outbox/x.png"
#                             where the scheme ends with `:`), plus
#                             `\w` (word char) and `/` (already part
#                             of an absolute path prefix) so we don't
#                             match in the middle of a longer token.
#   (/[^\s`\"'<>()\[\]]+?  -- capture group 1: absolute path (leading
#                             `/`) whose body contains no whitespace
#                             and no bracketed/backtick/quote wrappers.
#                             Non-greedy (`+?`) so the match ends at
#                             the FIRST valid extension, not the last.
#   (?:.png|.jpg|...)      -- dynamic alternation of allowed
#                             extensions, re.escape()-d to defuse
#                             the dots.
#   (?=[\s`\"'<>()\[\].,;:!?/]|$) -- stop-set lookahead enforcing a
#                             post-extension delimiter (whitespace,
#                             punct, closing bracket/quote/backtick,
#                             or EOS). Note `/` is in the stop set so
#                             `/abs/outbox/x.png/y` matches
#                             `/abs/outbox/x.png` (see pitfall #10).
#
# Case-insensitive so `.PNG` / `.Png` / `.png` all match.
#
# ``re.UNICODE`` is the Python 3 default (re.U = 0 in re.compile
# flag accounting) so ``\s`` matches NBSP (U+00A0) and the rest of
# the Unicode whitespace class out of the box — this is already
# load-bearing for Russian-text corpora and the spike S-2 corpus
# included NBSP cases.
#
# Fix-pack D5: the zero-width joiner family (``\u200B`` / ``\u200C``
# / ``\u200D``) is NOT part of Python's ``\s`` but can legitimately
# appear in chat text as an invisible separator the model itself did
# not intend. We add them to the body-forbid set and the stop-set
# lookahead so ``/abs/x.png\u200Bend`` terminates the path at the
# ZWSP rather than failing to match altogether — mirroring NBSP
# behaviour and preventing a hostile client from smuggling invisible
# separators that silently inflate the path.
#
# Known residual false-negatives (documented in S-2):
#   1. `/abs/x.png/abs/y.pdf` — extracts only the first path;
#      acceptable per S-2 (the model should not emit two adjacent
#      absolute paths).
#   2. `готово:/abs/outbox/x.png` (no space after colon) — NO match;
#      SKILL.md H-13 rule "always put a space after `:` before an
#      outbox path" ensures the model never produces this form.
#   3. `/abs/outbox/x.png/y` — matches x.png (trailing `y` treated
#      as noise); the downstream `exists()` check rejects non-files.
_ZW_TERMINATORS: Final[str] = "\u200b\u200c\u200d"
ARTEFACT_RE: re.Pattern[str] = re.compile(
    rf"(?<![\w/.:])(/[^\s`\"'<>()\[\]{_ZW_TERMINATORS}]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    rf"(?=[\s`\"'<>()\[\].,;:!?/{_ZW_TERMINATORS}]|$)",
    re.IGNORECASE | re.UNICODE,
)


# Fast-lookup sets for the classifier. We rebuild them from the
# ordered tuples so a future extension added to one of the `_*_EXT`
# tuples is automatically routed correctly.
_PHOTO_SET: Final[frozenset[str]] = frozenset(_PHOTO_EXT)
_AUDIO_SET: Final[frozenset[str]] = frozenset(_AUDIO_EXT)
_DOC_SET: Final[frozenset[str]] = frozenset(_DOC_EXT)


def classify_artefact(path: Path) -> str:
    """Return the dispatch kind for an artefact path.

    Returns one of `"photo"`, `"audio"`, `"document"`. Raises
    `ValueError` on an unknown extension — callers are expected to
    have filtered with `ARTEFACT_RE` first, so an unknown suffix
    here means the regex drifted from the extension tables (a bug
    to surface loudly, not silently send as "document").

    The comparison is lower-cased so `.PNG` / `.Png` / `.png` all
    classify as `photo` — matching `ARTEFACT_RE`'s `re.IGNORECASE`
    behaviour so there's no split-brain between extract and classify.
    """
    suffix = path.suffix.lower()
    if suffix in _PHOTO_SET:
        return "photo"
    if suffix in _AUDIO_SET:
        return "audio"
    if suffix in _DOC_SET:
        return "document"
    raise ValueError(
        f"unknown artefact extension {suffix!r} for path {path}"
    )


__all__ = [
    "ARTEFACT_RE",
    "classify_artefact",
]
