"""Regression tests for shipped skills on disk.

Phase-2 smoke test revealed a YAML ScannerError in the shipped
`skills/ping/SKILL.md`: an unquoted description containing
`{"pong": true}` was interpreted by the YAML scanner as a flow mapping.
The existing unit tests in `test_manifest_cache.py` use hermetic,
inline SKILL.md fixtures and never touched the real shipped files, so
the bug reached production.

These tests guard against that regression by parsing the actual
SKILL.md files shipped in `skills/` at the repo root — if anyone ships
broken frontmatter, CI catches it before the daemon does.
"""

from __future__ import annotations

from pathlib import Path

from assistant.bridge.skills import build_manifest, parse_skill

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PROJECT_ROOT / "skills"


def test_shipped_ping_skill_parses() -> None:
    """`skills/ping/SKILL.md` must have valid YAML frontmatter.

    `parse_skill` swallows YAML errors implicitly — it calls
    `yaml.safe_load` without a try/except, so a malformed frontmatter
    surfaces as `yaml.scanner.ScannerError` at call time. We assert
    here that the call succeeds and the returned dict has the fields
    the bridge relies on.
    """
    ping_skill = SKILLS_DIR / "ping" / "SKILL.md"
    assert ping_skill.exists(), f"shipped ping skill missing at {ping_skill}"

    meta = parse_skill(ping_skill)

    # `parse_skill` returns an empty dict on missing frontmatter —
    # a populated dict with `name` set means parsing succeeded.
    assert meta, "parse_skill returned empty dict — frontmatter missing or malformed"
    assert meta["name"] == "ping"
    assert meta["description"], "ping skill must have a non-empty description"
    assert "allowed_tools" in meta


def test_ping_skill_body_contains_marker() -> None:
    """Regression: ping SKILL.md body must instruct text-gen marker response.

    Phase 2 ping skill is text-generation, not tool-invocation (Variant C per
    plan/phase2/known-debt.md#D1). Owner smoke test expects Claude to respond
    with 'PONG-FROM-SKILL-OK' marker — body must contain that exact string.
    """
    project_root = Path(__file__).resolve().parents[1]
    skill_path = project_root / "skills" / "ping" / "SKILL.md"
    assert skill_path.exists()
    text = skill_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md must have YAML frontmatter"
    body = parts[2]
    assert "PONG-FROM-SKILL-OK" in body, (
        "Ping SKILL.md body must contain marker 'PONG-FROM-SKILL-OK' "
        "(text-gen pattern, see known-debt.md#D1). Original imperative "
        "body deprecated — Opus does not reliably execute Bash from body."
    )


def test_shipped_skills_manifest_builds() -> None:
    """`build_manifest` must succeed on the real `skills/` directory.

    This mirrors what `_render_system_prompt` does on every turn in the
    daemon. If any shipped SKILL.md has broken frontmatter, this call
    raises — exactly as it did on the phase-2 smoke test.
    """
    assert SKILLS_DIR.exists(), f"skills directory missing at {SKILLS_DIR}"

    # Must not raise.
    manifest = build_manifest(SKILLS_DIR)

    # `build_manifest` returns a rendered markdown string, not a list.
    assert isinstance(manifest, str)
    # Sentinel strings the function returns when no skills are available —
    # neither should apply: phase 2 ships exactly the ping skill.
    assert manifest != "(skills directory missing)"
    assert manifest != "(no skills registered yet)"
    # The ping skill must appear in the rendered manifest.
    assert "ping" in manifest
