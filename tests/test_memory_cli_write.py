"""Memory CLI `write`: happy path + frontmatter serialization + stdin body."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests._helpers.memory_cli import run_memory


def test_write_happy_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    idx = tmp_path / "memory-index.db"
    res = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "Test",
        "--body",
        "-",
        vault_dir=vault,
        index_db=idx,
        stdin="hello\n",
    )
    assert res.rc == 0, res.stderr
    payload = res.json_out
    assert payload["ok"] is True
    assert payload["data"] == {"path": "inbox/a.md", "title": "Test", "area": "inbox"}
    # File on disk + frontmatter parses.
    note_path = vault / "inbox" / "a.md"
    assert note_path.exists()
    text = note_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    # Everything between the fences is valid YAML; title matches.
    front = text.split("---\n", 2)[1]
    meta = yaml.safe_load(front)
    assert meta["title"] == "Test"
    assert "created" in meta
    assert meta["area"] == "inbox"


def test_write_stdin_only(tmp_path: Path) -> None:
    """`--body` must be `-` — positional / literal body is rejected."""
    res = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "T",
        "--body",
        "hello",  # NOT '-'
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin=None,
    )
    assert res.rc == 2
    assert res.json_err["ok"] is False
    assert "stdin" in res.json_err["error"].lower()


def test_write_tags_comma_split(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "inbox/people.md",
        "--title",
        "People",
        "--tags",
        "personal, family, wife",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="content",
    )
    assert res.rc == 0
    text = (tmp_path / "v" / "inbox" / "people.md").read_text(encoding="utf-8")
    front = text.split("---\n", 2)[1]
    meta = yaml.safe_load(front)
    assert meta["tags"] == ["personal", "family", "wife"]


def test_write_requires_title(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "inbox/a.md",
        "--title",
        "",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="x",
    )
    assert res.rc == 3
    assert "title" in res.json_err["error"].lower()


def test_write_rejects_absolute_path(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "/etc/passwd.md",
        "--title",
        "pwn",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="x",
    )
    assert res.rc == 3


def test_write_rejects_dotdot(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "../escape.md",
        "--title",
        "pwn",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="x",
    )
    assert res.rc == 3


def test_write_rejects_non_md_extension(tmp_path: Path) -> None:
    res = run_memory(
        "write",
        "inbox/a.txt",
        "--title",
        "pwn",
        "--body",
        "-",
        vault_dir=tmp_path / "v",
        index_db=tmp_path / "i.db",
        stdin="x",
    )
    assert res.rc == 3
