"""Review wave 3: FTS5 default is phrase-form; --raw opts into operators."""

from __future__ import annotations

from pathlib import Path

from tests._helpers.memory_cli import run_memory


def test_search_hyphen_does_not_crash(tmp_path: Path) -> None:
    """Hyphenated tokens (e.g. `test-file`) are common; FTS5 parses
    `-` as column separator unless we phrase-quote the query."""
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
        stdin="foo-bar stuff",
    )
    res = run_memory("search", "foo-bar", vault_dir=vault, index_db=idx)
    # MUST NOT crash with exit 5. Either hit or no-hit is fine.
    assert res.rc == 0, (res.stdout, res.stderr)


def test_search_double_quote_escapes(tmp_path: Path) -> None:
    """A double-quote inside the query should be escaped, not syntax-error."""
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
        stdin='he said "hi"',
    )
    res = run_memory("search", 'said "hi"', vault_dir=vault, index_db=idx)
    assert res.rc == 0, (res.stdout, res.stderr)


def test_search_or_literal_by_default(tmp_path: Path) -> None:
    """`A OR B` default-matches as a literal 4-char phrase, NOT the operator."""
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
        stdin="alpha beta",  # contains neither 'A' nor 'OR' nor 'B'
    )
    res = run_memory("search", "A OR B", vault_dir=vault, index_db=idx)
    assert res.rc == 0
    # No hit — "A OR B" as a phrase does not appear in the body.
    assert res.json_out["data"]["hits"] == []


def test_search_raw_allows_fts5_operator(tmp_path: Path) -> None:
    """With --raw, user gets OR/AND operators."""
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
        stdin="alpha",
    )
    run_memory(
        "write",
        "inbox/b.md",
        "--title",
        "T",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="beta",
    )
    res = run_memory("search", "--raw", "alpha OR beta", vault_dir=vault, index_db=idx)
    assert res.rc == 0, (res.stdout, res.stderr)
    hits = res.json_out["data"]["hits"]
    assert len(hits) == 2
