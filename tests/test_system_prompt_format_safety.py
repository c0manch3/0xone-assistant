"""Regression test for system_prompt.md.

Ensures ``template.format(...)`` on the bridge system prompt succeeds for
every currently-rendered placeholder. Prevents a repeat of the hotfix where
a JSON literal ``{"job_id": N}`` was parsed by ``str.format`` as a format
field and raised ``KeyError``, silently dropping owner replies in
production.
"""

from pathlib import Path

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "assistant"
    / "bridge"
    / "system_prompt.md"
)


def test_system_prompt_format_with_all_placeholders() -> None:
    """``str.format`` on the template must not raise on literal braces."""
    assert TEMPLATE_PATH.exists(), f"template not found at {TEMPLATE_PATH}"
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    rendered = template.format(
        project_root="/app",
        vault_dir="/app/data/vault",
        skills_manifest="- skill1: ...\n- skill2: ...",
    )

    # All documented placeholders must be substituted.
    assert "{project_root}" not in rendered
    assert "{vault_dir}" not in rendered
    assert "{skills_manifest}" not in rendered

    # The escaped JSON example must survive as a single-brace literal in
    # the rendered output (double-brace escape collapses to a literal `{`).
    assert '{"job_id": N, "status": "requested"}' in rendered
