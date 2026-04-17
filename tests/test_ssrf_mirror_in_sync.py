"""H-2 guard: installer's SSRF mirror must stay byte-identical with net.py.

Reads the block between the `SSRF_MIRROR_START` and `SSRF_MIRROR_END`
sentinels in `src/assistant/bridge/net.py` and asserts the same block
appears verbatim in `tools/skill-installer/_lib/_net_mirror.py`. If the
upstream is ever edited without copying, this fails — operator must
re-copy.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "assistant" / "bridge" / "net.py"
DST = ROOT / "tools" / "skill-installer" / "_lib" / "_net_mirror.py"


def _extract_block(text: str, start_marker: str, end_marker: str) -> str:
    a = text.index(start_marker) + len(start_marker)
    b = text.index(end_marker)
    return text[a:b]


def test_ssrf_mirror_byte_identical() -> None:
    src_text = SRC.read_text(encoding="utf-8")
    dst_text = DST.read_text(encoding="utf-8")
    src_block = _extract_block(src_text, "SSRF_MIRROR_START", "SSRF_MIRROR_END").strip()
    assert src_block in dst_text, (
        "tools/skill-installer/_lib/_net_mirror.py must contain an exact copy "
        "of the SSRF block from src/assistant/bridge/net.py — re-copy the "
        "block between the SSRF_MIRROR_START / SSRF_MIRROR_END sentinels."
    )
