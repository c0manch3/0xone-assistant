"""S3: body with literal `---` at column 0 is indented to avoid frontmatter spoof."""

from __future__ import annotations

from pathlib import Path

from _memlib.frontmatter import sanitize_body
from tests._helpers.memory_cli import run_memory


def test_sanitize_indents_dashes() -> None:
    src = "first\n---\nsecond\n"
    out = sanitize_body(src)
    assert "first\n ---\nsecond\n" == out


def test_sanitize_preserves_inline_dashes() -> None:
    """`foo --- bar` in the middle of a line is fine (not column 0)."""
    src = "foo --- bar\n"
    assert sanitize_body(src) == src


def test_write_sanitizes_body(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    body = "line1\n---\nline2\n"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin=body,
    )
    text = (vault / "inbox" / "a.md").read_text(encoding="utf-8")
    # Original dash line must be indented now — no fence spoof.
    assert "\n ---\n" in text
    # And `memory read` still parses frontmatter correctly.
    r = run_memory("read", "inbox/a.md", vault_dir=vault, index_db=idx)
    assert r.rc == 0
    body_back = r.json_out["data"]["body"]
    assert " ---" in body_back
