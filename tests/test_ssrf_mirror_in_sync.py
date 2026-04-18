"""H-2 guard: installer's SSRF mirror must stay byte-identical with net.py.

Review fix #13 strengthens the assertion from one-way substring-containment
to **bidirectional** block-equality: both the src and dst files carry
`SSRF_MIRROR_START` / `SSRF_MIRROR_END` sentinels; we extract the block
between them from each side and assert the extracted text is identical.
The original `in dst_text` check would have passed even when the mirror
grew *additional* statements between the markers; now any drift in
either direction fails the test.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "assistant" / "bridge" / "net.py"
DST = ROOT / "tools" / "skill_installer" / "_lib" / "_net_mirror.py"

# Anchor on the full comment-line prefix so we don't match the sentinel
# names that also appear inside the surrounding docstrings / references.
_START_LINE_PREFIX = "# --- SSRF_MIRROR_START"
_END_LINE_PREFIX = "# --- SSRF_MIRROR_END"


def _extract_between_sentinels(text: str) -> str:
    """Return the bytes between the START and END *comment-marker lines*.

    Both markers live on their own `# --- SSRF_MIRROR_* ---` line. We
    anchor on `# --- SSRF_MIRROR_` so bare string mentions inside
    docstrings ("between the SSRF_MIRROR_START/END sentinels") don't
    match. The returned slice starts at the newline after the START
    comment and ends at the newline before the END comment — both
    extractors produce byte-identical slices when the mirror is honest.
    """
    start_idx = text.index(_START_LINE_PREFIX)
    # Walk forward to the end of the START comment line.
    body_start = text.index("\n", start_idx) + 1
    end_idx = text.index(_END_LINE_PREFIX)
    # `end_idx` points at the `#` of the END line. The byte immediately
    # before is the newline that terminated the previous line; slice to
    # that position (exclusive) so we keep the trailing `\n` of the last
    # code line but drop the END marker itself.
    return text[body_start:end_idx]


def test_ssrf_mirror_byte_identical_bidirectional() -> None:
    src_text = SRC.read_text(encoding="utf-8")
    dst_text = DST.read_text(encoding="utf-8")
    src_block = _extract_between_sentinels(src_text)
    dst_block = _extract_between_sentinels(dst_text)
    assert src_block == dst_block, (
        "SSRF mirror drift between src/assistant/bridge/net.py and "
        "tools/skill_installer/_lib/_net_mirror.py: re-copy the block "
        f"verbatim.\n\nsrc bytes ({len(src_block)}):\n{src_block!r}\n\n"
        f"dst bytes ({len(dst_block)}):\n{dst_block!r}"
    )


def test_ssrf_mirror_blocks_nonempty() -> None:
    """Sanity: both sides actually contain content between the sentinels.
    Guards against a mis-merge that leaves the block empty on one side."""
    src_block = _extract_between_sentinels(SRC.read_text(encoding="utf-8"))
    dst_block = _extract_between_sentinels(DST.read_text(encoding="utf-8"))
    assert "is_private_address" in src_block
    assert "is_private_address" in dst_block
    assert "classify_url" in src_block
    assert "classify_url" in dst_block
