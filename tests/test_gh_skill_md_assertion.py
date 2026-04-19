"""Structural assertions on ``skills/gh/SKILL.md`` (phase-8 C2).

We parse the YAML frontmatter with a small regex rather than pyyaml so
the test stays fast and stdlib-only — the same approach used by phase-7
skill sanity tests. The exit-code matrix check pins the full 11-row table
(0..10) to the file so future edits can't silently drop a code the
daemon/CLI depends on.
"""

from __future__ import annotations

import pathlib
import re

_SKILL_PATH = pathlib.Path(__file__).resolve().parents[1] / "skills" / "gh" / "SKILL.md"


def _read_skill() -> str:
    assert _SKILL_PATH.is_file(), f"SKILL.md missing at {_SKILL_PATH}"
    return _SKILL_PATH.read_text(encoding="utf-8")


def _extract_frontmatter(text: str) -> str:
    m = re.match(r"^---\n(?P<fm>.+?)\n---\n", text, re.DOTALL)
    assert m, "frontmatter block missing (expected `---\\n...\\n---\\n` header)"
    return m.group("fm")


def test_frontmatter_has_name_description_allowed_tools() -> None:
    fm = _extract_frontmatter(_read_skill())
    assert re.search(r"^name:\s*gh\s*$", fm, re.MULTILINE), (
        "frontmatter must declare `name: gh`"
    )
    assert re.search(r"^description:\s*\S", fm, re.MULTILINE), (
        "frontmatter must declare a non-empty `description:` line"
    )
    # Manifest pins allowed-tools strictly to [Bash] at C2 — read-only gh
    # calls + daily vault-commit-push all go through Bash subprocesses.
    assert re.search(r"^allowed-tools:\s*\[Bash\]\s*$", fm, re.MULTILINE), (
        "frontmatter must declare `allowed-tools: [Bash]`"
    )


def test_exit_code_matrix_lists_all_eleven_codes() -> None:
    text = _read_skill()
    # Each row starts with `| <code> ` (space separates the cell from the
    # meaning column). Pin every code 0..10 so a dropped row fails loudly.
    for code in range(11):
        needle = f"| {code} "
        assert needle in text, f"exit-code matrix missing row for code {code}"


def test_h13_rule_is_documented() -> None:
    """Phase-7 H-13 rule: artefact paths must have a space after `:`.

    The rule's presence is a hard requirement across every skill that
    surfaces outbox paths to the model. We grep for the canonical phrasing
    fragment so a future edit that rewords it into oblivion fails.
    """
    text = _read_skill().lower()
    # Both halves must be present — the constraint (space after colon) AND
    # the phase-7 attribution so readers know where the rule comes from.
    assert "space" in text and "after" in text and ":" in text, (
        "H-13 rule text not found (expected phrasing about a space after `:`)"
    )
    assert "h-13" in text or "h13" in text, (
        "H-13 rule attribution missing (link back to phase-7 skills/memory)"
    )


def test_has_at_least_five_dialog_examples() -> None:
    text = _read_skill()
    # Examples reference the CLI via `python tools/gh/main.py ...` — count
    # those as a conservative lower bound (some examples may show bare
    # subcommand snippets without the full interpreter path).
    count = text.count("python tools/gh/main.py")
    assert count >= 5, f"expected >= 5 CLI references in examples, got {count}"
