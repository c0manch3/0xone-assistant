"""Phase 6 fix-pack CRITICAL #1 — skill description steers async UX to CLI.

Devil C-1/C-2: the SKILL.md frontmatter description is what the skill-
router / model matches against. Before the fix-pack it promised "via
the native Task tool" for long-running work, which (per S-6-0 Q1 FAIL)
actually blocks the main turn because the native Task tool is a
synchronous RPC on SDK 0.1.59 + CLI 2.1.114. Default UX path: owner
sees "bot typing..." for minutes — worst-case.

Post-fix, the description must explicitly name the CLI path as the
preferred default for async UX, and flag the native Task tool as
blocking-only-appropriate-for-short-tasks.

We parse the YAML frontmatter the same way the skill loader does (split
on `---` lines) so a cosmetic-only change to the body text doesn't
regress this contract.
"""

from __future__ import annotations

from pathlib import Path

_SKILL_PATH = Path(__file__).resolve().parents[1] / "skills" / "task" / "SKILL.md"


def _read_description() -> str:
    """Extract the `description:` value from the YAML frontmatter.

    We don't use PyYAML to keep this test hermetic — YAML is a project-
    wide dependency anyway but the SKILL.md files are tiny and the
    frontmatter is a predictable shape (opening `---`, key-value pairs,
    closing `---`). The description is a single line on one key.
    """
    text = _SKILL_PATH.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    # parts = ["", "<frontmatter>", "<body>"]
    assert len(parts) >= 3, "SKILL.md must have YAML frontmatter"
    frontmatter = parts[1]
    for line in frontmatter.splitlines():
        stripped = line.strip()
        if stripped.startswith("description:"):
            # Strip quotes; PyYAML rules would handle escapes, but our
            # descriptions don't carry any. Keep the simple split.
            value = stripped.split(":", 1)[1].strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            return value
    raise AssertionError("SKILL.md frontmatter missing `description:`")


def test_description_names_cli_path_for_async() -> None:
    """For async UX the description must recommend the CLI spawn path."""
    description = _read_description()
    # Must reference the CLI path explicitly so the skill router can
    # match on it.
    assert "tools/task/main.py" in description, description
    # Must call out that native Task blocks, not async.
    lowered = description.lower()
    assert "block" in lowered, (
        "description must flag the native Task tool as blocking — else "
        "the model will pick native Task for long writeups and the owner "
        "will see 'bot typing...' for minutes (devil C-1 / Q1 FAIL)."
    )


def test_description_flags_native_task_as_blocking() -> None:
    """Explicit evidence that the skill description warns about the
    blocking semantics of the native Task tool — protects against a
    future rewrite that silently regresses the guidance."""
    description = _read_description()
    # The string "native Task" (case-sensitive on the tool name) with
    # some form of "block" nearby is a strong signal.
    assert "native Task" in description, description
    # Be tolerant of phrasing: either "BLOCKS" or "blocking" or "blocks".
    assert "block" in description.lower()
