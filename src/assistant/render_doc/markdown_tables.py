"""Phase 9 §2.8 — pure-Python pipe-syntax table parser for the XLSX
renderer.

Owner-frozen choice (R3.13): no new dep — we keep the parser minimal
(pipe tables only; no fenced grid, no extension tables). Test target
≥10 cases enumerated in spec §3 Wave B B1.

Algorithm:
  1. Split ``content_md`` by lines; preserve original line numbers
     for error reporting.
  2. Walk lines top-to-bottom; whenever a line looks like a header
     (starts with ``|``, has ≥2 cells, next non-blank line is a
     separator like ``|---|---|``), absorb subsequent body rows
     until a non-table line OR EOF.
  3. Each cell gets ``\\|`` → ``|`` un-escaping + leading/trailing
     whitespace stripped.

Returns ``list[Table]``; an empty list signals "no tables found"
(the @tool body maps that to ``markdown-no-tables``).

Error handling:
  - Header without separator → :exc:`MarkdownTableError("malformed")`.
  - Body row with mismatched col count → :exc:`MarkdownTableError`
    (spec v3 strict policy; coder may relax to pad-with-empty).
  - Multi-line cells (Markdown extension we don't support) →
    parse continues; the line is treated as a body row in the same
    table as long as it has the same column count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A separator row consists of cells made entirely of dashes, optionally
# wrapped in colons for alignment markers (``:---``, ``---:``,
# ``:---:``). Single ``|---|`` separator with ≥3 dashes per cell.
_SEPARATOR_CELL_RE = re.compile(r"^\s*:?-{3,}:?\s*$")


@dataclass
class Table:
    """Parsed pipe-table.

    ``alignments`` parallels ``header`` and is one of ``"left"``,
    ``"right"``, ``"center"``, or ``"none"`` per cell. The XLSX
    renderer ignores alignments in v1 (openpyxl ``write_only=True``
    doesn't expose per-cell alignment cheaply); we preserve them for
    future use.
    """

    header: list[str]
    alignments: list[str]
    rows: list[list[str]] = field(default_factory=list)


class MarkdownTableError(ValueError):
    """Raised by :func:`parse` on malformed pipe-table syntax.

    Carries a short kebab-case ``code`` attribute the @tool body maps
    to the ``error`` envelope field.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _split_pipes(line: str) -> list[str]:
    """Split a pipe-table line into cells, honouring ``\\|`` escapes.

    Strips the leading and trailing pipe characters (markdown pipe
    tables conventionally bracket each row) before splitting; tables
    written without leading/trailing pipes still parse correctly.
    """
    # Replace ``\\|`` with a sentinel that cannot appear in real
    # markdown, split on ``|``, then restore.
    sentinel = "\x00ESC_PIPE\x00"
    line = line.replace("\\|", sentinel)
    # Strip leading + trailing pipe.
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells = [
        c.strip().replace(sentinel, "|") for c in line.split("|")
    ]
    return cells


def _alignment_of(cell: str) -> str:
    """Map ``:---``, ``---:``, ``:---:`` (and ``---``) to alignment."""
    cell = cell.strip()
    left = cell.startswith(":")
    right = cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return "none"


def _is_separator_row(line: str) -> bool:
    """Return True iff every cell in ``line`` is a separator marker."""
    cells = _split_pipes(line)
    if not cells:
        return False
    return all(_SEPARATOR_CELL_RE.match(c) for c in cells)


def parse(content_md: str) -> list[Table]:
    """Extract every pipe-table found in ``content_md``.

    Returns an empty list on no tables. Raises :exc:`MarkdownTableError`
    on malformed table structure (header without separator, body row
    with mismatched column count, etc.).
    """
    if not content_md or not content_md.strip():
        return []

    tables: list[Table] = []
    lines = content_md.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        # Header candidate must contain a pipe AND have at least one
        # non-empty cell.
        if "|" not in line:
            i += 1
            continue
        header_cells = _split_pipes(line)
        if not header_cells or all(not c for c in header_cells):
            i += 1
            continue
        # Look ahead for separator (skip blank lines as a defensive
        # tolerance — strict CommonMark wants the separator on the
        # very next line).
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j >= n:
            i += 1
            continue
        sep_line = lines[j]
        if "|" not in sep_line or not _is_separator_row(sep_line):
            # No separator → not a table; advance past header line.
            i += 1
            continue
        sep_cells = _split_pipes(sep_line)
        if len(sep_cells) != len(header_cells):
            raise MarkdownTableError("markdown-malformed")
        alignments = [_alignment_of(c) for c in sep_cells]
        table = Table(header=header_cells, alignments=alignments)
        # Walk body rows.
        k = j + 1
        while k < n:
            body_line = lines[k]
            stripped = body_line.strip()
            if not stripped or "|" not in body_line:
                break
            body_cells = _split_pipes(body_line)
            if len(body_cells) != len(header_cells):
                raise MarkdownTableError("markdown-malformed")
            table.rows.append(body_cells)
            k += 1
        tables.append(table)
        i = k
    return tables
