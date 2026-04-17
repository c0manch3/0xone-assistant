"""Memory CLI: `[[wikilinks]]` round-trip verbatim and are extracted on read."""

from __future__ import annotations

from pathlib import Path

from _memlib.frontmatter import extract_wikilinks
from tests._helpers.memory_cli import run_memory


def test_wikilinks_round_trip(tmp_path: Path) -> None:
    body = "see [[target-note]] for context and [[other|alias]] details"
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
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
    assert "[[target-note]]" in text
    assert "[[other|alias]]" in text


def test_extract_wikilinks_unit() -> None:
    links = extract_wikilinks("see [[a]] then [[b|alias]] and [[c]]")
    assert links == ["a", "b", "c"]


def test_read_extracts_wikilinks(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="link to [[other]]",
    )
    r = run_memory("read", "inbox/a.md", vault_dir=vault, index_db=idx)
    assert r.json_out["data"]["wikilinks"] == ["other"]
