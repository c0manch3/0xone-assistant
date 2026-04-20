"""Tests for scripts/migrate_midomis_vault.py.

Lives inside scripts/ on purpose -- we don't want this one-off migration
helper polluting the main pytest suite under tests/. Run explicitly via:

    uv run pytest scripts/test_migrate_midomis_vault.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

# Make scripts/ importable for the sibling module under test.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import migrate_midomis_vault as mig  # noqa: E402, I001  # sys.path mutated above


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_midomis_tree(
    root: Path,
    chat_id: str,
    files: dict[str, str],
) -> Path:
    """Create a midomis-shaped data tree.

    `files` maps `<area>/<name>.md` (or just `_index.md`) to full file text.
    Returns the resolved data dir (i.e. the one you'd pass as --source).
    """
    vault = root / "data" / "users" / chat_id / "vault"
    for rel, content in files.items():
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root / "data"


def _read_target(target: Path, area: str, name: str) -> tuple[dict, str]:
    """Read a migrated file and return (frontmatter_dict, body)."""
    text = (target / area / name).read_text(encoding="utf-8")
    fm, body = mig.parse_source(text)
    return fm, body


# --------------------------------------------------------------------------- #
# Pure-function tests
# --------------------------------------------------------------------------- #


class TestTitleFromFilename:
    """title_from_filename: ASCII + cyrillic variants."""

    def test_kebab_case(self) -> None:
        assert (
            mig.title_from_filename("studio44-workload-platform")
            == "Studio44 Workload Platform"
        )

    def test_snake_case(self) -> None:
        assert mig.title_from_filename("api_gateway_v2") == "Api Gateway V2"

    def test_mixed_separators(self) -> None:
        assert (
            mig.title_from_filename("foo-bar_baz-qux") == "Foo Bar Baz Qux"
        )

    def test_single_word(self) -> None:
        assert mig.title_from_filename("simple") == "Simple"

    def test_cyrillic_single_word(self) -> None:
        # "dolgi" (debts) -> first letter capitalised, tail preserved.
        assert (
            mig.title_from_filename("\u0434\u043e\u043b\u0433\u0438")
            == "\u0414\u043e\u043b\u0433\u0438"
        )

    def test_cyrillic_kebab(self) -> None:
        # "moi-zametki" (my-notes) -> "Moi Zametki".
        assert (
            mig.title_from_filename(
                "\u043c\u043e\u0438-\u0437\u0430\u043c\u0435\u0442\u043a\u0438"
            )
            == "\u041c\u043e\u0438 \u0417\u0430\u043c\u0435\u0442\u043a\u0438"
        )

    def test_preserves_mixed_case_tail(self) -> None:
        # Guards against accidental .title() usage that would mangle "GPT".
        assert mig.title_from_filename("my-GPT-notes") == "My GPT Notes"

    def test_empty_stem_falls_back(self) -> None:
        # Pathological input -- we must not produce an empty title since
        # phase-4 schema says title is mandatory.
        assert mig.title_from_filename("") == "untitled"

    def test_separator_only_stem(self) -> None:
        # "---" -> no words after split -> fallback to stem itself.
        assert mig.title_from_filename("---") == "---"


# --------------------------------------------------------------------------- #
# File-level tests
# --------------------------------------------------------------------------- #


def test_skip_index(tmp_path: Path) -> None:
    """_index.md files must be skipped and never written to the target."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="42",
        files={
            "projects/_index.md": "# projects\n\n- [[alpha]]\n",
            "projects/alpha.md": (
                "---\n"
                "created: 2025-01-01\n"
                "tags: [work]\n"
                "---\n"
                "alpha body\n"
            ),
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "42",
            "--target",
            str(target),
        ]
    )

    assert rc == 0
    # Real note migrated.
    assert (target / "projects" / "alpha.md").exists()
    # _index.md NOT copied.
    assert not (target / "projects" / "_index.md").exists()


def test_transform_full_frontmatter(tmp_path: Path) -> None:
    """midomis 3-field FM -> 0xone 4-field FM (title+area added, source dropped)."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "blog/studio44-workload-platform.md": (
                "---\n"
                "created: 2024-11-15\n"
                "tags:\n"
                "  - platform\n"
                "  - work\n"
                "source: telegram\n"
                "---\n"
                "Post body with [[wiki-link]].\n"
            )
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )

    assert rc == 0
    fm, body = _read_target(target, "blog", "studio44-workload-platform.md")

    assert fm["title"] == "Studio44 Workload Platform"
    assert fm["area"] == "blog"
    assert fm["tags"] == ["platform", "work"]
    # PyYAML turns `2024-11-15` into a `datetime.date`. Compare its ISO form
    # so the test is independent of the exact runtime type.
    assert str(fm["created"]) == "2024-11-15"
    # `source:` must have been dropped.
    assert "source" not in fm
    # Body preserved exactly.
    assert body == "Post body with [[wiki-link]].\n"


def test_transform_missing_frontmatter(tmp_path: Path) -> None:
    """Plain markdown (no FM) gets synthetic title/area/created."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={"inbox/quick-thought.md": "Just a plain note, no frontmatter.\n"},
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )

    assert rc == 0
    fm, body = _read_target(target, "inbox", "quick-thought.md")

    assert fm["title"] == "Quick Thought"
    assert fm["area"] == "inbox"
    # Synthesised from mtime -> must be a valid ISO date.
    assert isinstance(fm["created"], str) or hasattr(fm["created"], "isoformat")
    # No tags / source in source -> absent from target.
    assert "tags" not in fm
    assert "source" not in fm
    # Whole original content ends up as body (nothing was a frontmatter).
    assert body == "Just a plain note, no frontmatter.\n"


def test_malformed_yaml_error_logged(tmp_path: Path) -> None:
    """Broken YAML frontmatter -> file appears in errors, others still migrate."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "projects/broken.md": (
                "---\n"
                "key: : :\n"           # deliberately malformed YAML
                "  - nested??: ][\n"
                "---\n"
                "body\n"
            ),
            "projects/good.md": (
                "---\n"
                "created: 2025-02-02\n"
                "tags: [ok]\n"
                "---\n"
                "good body\n"
            ),
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )

    # Partial success: one error -> rc == 2.
    assert rc == 2
    # Good file was still migrated.
    assert (target / "projects" / "good.md").exists()
    # Broken file was NOT written.
    assert not (target / "projects" / "broken.md").exists()


def test_malformed_yaml_reported_in_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON summary must contain the broken file under errors[]."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "projects/broken.md": "---\nkey: : :\n---\nbody\n",
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )
    assert rc == 2

    out = capsys.readouterr().out
    summary = json.loads(out)
    assert len(summary["errors"]) == 1
    assert "broken.md" in summary["errors"][0]["path"]
    assert "malformed" in summary["errors"][0]["reason"].lower()


def test_dry_run_no_writes(tmp_path: Path) -> None:
    """--dry-run must produce the same JSON summary but touch no files."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "blog/hello.md": (
                "---\ncreated: 2025-03-01\ntags: [x]\n---\nbody\n"
            ),
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
            "--dry-run",
        ]
    )

    assert rc == 0
    # Target directory must not exist (nothing was written).
    assert not target.exists()


def test_body_preserved_wikilinks(tmp_path: Path) -> None:
    """Wiki-links, image embeds, code fences, cyrillic -- all verbatim."""
    body = (
        "Intro \u0442\u0435\u043a\u0441\u0442 \u043d\u0430 "
        "\u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.\n"
        "\n"
        "See [[another-note]] and [[inbox/idea|Idea]].\n"
        "\n"
        "![[diagram.png]]\n"
        "\n"
        "```python\n"
        "def hello():\n"
        "    return '\u043f\u0440\u0438\u0432\u0435\u0442'\n"
        "```\n"
        "\n"
        "Trailing line.\n"
    )
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "notes/rich-note.md": (
                "---\ncreated: 2025-04-01\ntags: [rich]\nsource: tg\n---\n"
                + body
            ),
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )
    assert rc == 0

    _, out_body = _read_target(target, "notes", "rich-note.md")
    assert out_body == body


# --------------------------------------------------------------------------- #
# CLI / fatal-path tests
# --------------------------------------------------------------------------- #


def test_missing_source_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = mig.run(
        [
            "--source",
            str(tmp_path / "does-not-exist"),
            "--chat-id",
            "7",
            "--target",
            str(tmp_path / "vault"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "fatal" in err.lower()


def test_root_level_file_skipped(tmp_path: Path) -> None:
    """A file directly under vault/ (no area folder) must not be migrated."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={"_index.md": "root index"},  # goes directly under vault/
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )
    assert rc == 0
    assert not target.exists()  # nothing was written


def test_json_summary_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON summary has the documented keys and correct counts."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "blog/a.md": "---\ncreated: 2025-01-01\n---\nA\n",
            "blog/b.md": "---\ncreated: 2025-01-02\n---\nB\n",
            "blog/_index.md": "# blog\n",
            "inbox/c.md": "plain\n",
        },
    )
    target = tmp_path / "vault"

    rc = mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )
    assert rc == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["source_files"] == 4
    assert summary["migrated"] == 3
    assert summary["skipped_index"] == 1
    assert summary["errors"] == []
    assert summary["areas"] == ["blog", "inbox"]
    assert summary["target_dir"] == str(target.resolve())


def test_frontmatter_output_is_deterministic(tmp_path: Path) -> None:
    """Key order in emitted YAML: title, area, tags, created."""
    source_data = _make_midomis_tree(
        tmp_path,
        chat_id="7",
        files={
            "blog/x.md": (
                "---\nsource: tg\ntags: [q]\ncreated: 2025-01-01\n---\nbody\n"
            ),
        },
    )
    target = tmp_path / "vault"
    mig.run(
        [
            "--source",
            str(source_data),
            "--chat-id",
            "7",
            "--target",
            str(target),
        ]
    )

    text = (target / "blog" / "x.md").read_text(encoding="utf-8")
    # Extract just the YAML block text to inspect ordering.
    assert text.startswith("---\n")
    yaml_block = text.split("---\n", 2)[1]
    lines = [ln.split(":", 1)[0] for ln in yaml_block.strip().splitlines() if ":" in ln]
    # First four top-level keys, in order:
    assert lines[:4] == ["title", "area", "tags", "created"]
    # Round-trip sanity: still parseable.
    parsed = yaml.safe_load(yaml_block)
    assert parsed["title"] == "X"
    assert parsed["area"] == "blog"
