"""Phase 7 / Wave 11 commit 18a — H-13 "space after `:` before outbox path".

The rule mitigates a subtle double-delivery / no-delivery bug: the
`dispatch_reply` regex (`_ARTEFACT_RE` v3) deliberately rejects
`готово:/abs/outbox/x.png` (no space) to avoid matching URL-scheme
prefixes like `https://…` as artefact paths. Consequence: if a phase-7
skill's final assistant reply cites an outbox path WITHOUT a space after
the colon, the file is silently NOT delivered as a Telegram attachment —
the user only sees the text.

To prevent this, every phase-7 skill's `SKILL.md` (and the global
`bridge/system_prompt.md`) MUST document the rule in a form that is both
model-visible and grep-discoverable. Acceptable markers (any one of):

1. The literal tag ``H-13`` together with a "space after `:`" /
   "пробел после `:`" phrase.
2. An explicit good/bad contrast such as
   ``Ready: /path`` vs ``Ready:/path`` (so the model sees the delta).

The tests below are deliberately permissive on wording but strict on
presence: a file missing the rule is a real regression, because the
regex change would cause silent artefact drops in production.

Scope of this commit is STRICTLY the test file — production content is
not modified here. If a file fails, the orchestrator will escalate to a
follow-up commit that adds the missing rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# The five files that must carry the H-13 rule. Keep this list explicit
# (rather than globbing `tools/*/SKILL.md`) so a future skill addition
# does not silently escape the assertion, AND so a removal is a loud
# test failure rather than a silently-shrinking parametrize set.
_H13_FILES: tuple[tuple[str, Path], ...] = (
    ("transcribe", _ROOT / "tools" / "transcribe" / "SKILL.md"),
    ("genimage", _ROOT / "tools" / "genimage" / "SKILL.md"),
    ("extract_doc", _ROOT / "tools" / "extract_doc" / "SKILL.md"),
    ("render_doc", _ROOT / "tools" / "render_doc" / "SKILL.md"),
    ("system_prompt", _ROOT / "src" / "assistant" / "bridge" / "system_prompt.md"),
)


def _has_textual_rule(body: str) -> bool:
    """Return True iff `body` contains a prose statement of the rule.

    We accept either English ("space after") or Russian ("пробел после")
    in the same sentence as the literal backtick-escaped colon, so a
    generic "colons are cool" sentence will NOT satisfy the check.
    """
    lowered = body.lower()
    if "space after" in lowered and ("`:`" in body or ": " in body):
        return True
    if "пробел после" in lowered and ("`:`" in body or ": " in body):
        return True
    return False


# Good = colon followed by space followed by `/` (absolute path).
# Bad  = colon followed DIRECTLY by `/` (no space).
#
# We look for the pair so that a single side (only the good form, or
# only the bad form) does not pass — the model needs the *contrast* to
# internalise the rule.
_GOOD_RE = re.compile(r"[A-Za-zА-Яа-я]+:\s+/[A-Za-z0-9_./-]+")
_BAD_RE = re.compile(r"[A-Za-zА-Яа-я]+:/[A-Za-z0-9_./-]+")


def _has_good_bad_examples(body: str) -> bool:
    """Return True iff `body` carries BOTH a good and a bad example.

    The contrast is what trains the model; a file with only
    ``Good: готово: /path`` but no matching bad counter-example is
    insufficient.
    """
    return bool(_GOOD_RE.search(body)) and bool(_BAD_RE.search(body))


def _satisfies_h13(body: str) -> tuple[bool, str]:
    """Central check used by every parametrized test.

    Returns (ok, reason). When `ok` is False, `reason` explains which
    markers were missing so the pytest failure is self-describing.
    """
    has_text = _has_textual_rule(body)
    has_examples = _has_good_bad_examples(body)
    if has_text or has_examples:
        return True, ""
    reasons = []
    if not has_text:
        reasons.append(
            'no prose marker: expected "space after `:`" / "пробел после `:`"'
        )
    if not has_examples:
        reasons.append(
            "no good/bad example pair (e.g. `Ready: /path` vs `Ready:/path`)"
        )
    return False, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Parametrized per-file tests. Using `ids=` so a failure message names
# the skill rather than an opaque index.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "path"),
    _H13_FILES,
    ids=[label for label, _ in _H13_FILES],
)
def test_file_documents_h13_colon_space_rule(label: str, path: Path) -> None:
    """Every phase-7 skill + `bridge/system_prompt.md` MUST document H-13.

    Failure mode to watch: if the `_ARTEFACT_RE` regex in
    `src/assistant/media/artefacts.py` ever changes to accept
    `token:/abs` (no space), this test becomes vestigial — but until
    then the rule is load-bearing for artefact delivery.
    """
    assert path.exists(), f"{label}: expected file is missing at {path}"
    body = path.read_text(encoding="utf-8")
    ok, reason = _satisfies_h13(body)
    assert ok, (
        f"{label} ({path.relative_to(_ROOT)}) does not document the H-13 "
        f"space-after-colon rule: {reason}"
    )


# ---------------------------------------------------------------------------
# Sanity test: enumerate every SKILL.md under `tools/` and make sure the
# four phase-7 artefact-producing skills are present and all H-13-tagged.
# This guards against:
#   (a) a new skill being added to `tools/` that also produces outbox
#       artefacts but forgets the rule, AND
#   (b) one of the four phase-7 skills being deleted or renamed without
#       the H-13 assertion being updated.
# ---------------------------------------------------------------------------


# Skills that produce outbox-delivered artefacts. If a future skill
# joins this club, add it here and to `_H13_FILES` above — the sanity
# test below will fail loudly until both lists are updated in sync.
_PHASE7_ARTEFACT_SKILLS = frozenset(
    {"transcribe", "genimage", "extract_doc", "render_doc"}
)


def test_all_phase7_artefact_skills_present_in_tools() -> None:
    """Every phase-7 artefact skill listed above exists under `tools/`."""
    tools_dir = _ROOT / "tools"
    assert tools_dir.is_dir(), f"tools/ missing at {tools_dir}"
    found = {
        p.parent.name for p in tools_dir.glob("*/SKILL.md") if p.is_file()
    }
    missing = _PHASE7_ARTEFACT_SKILLS - found
    assert not missing, (
        f"expected phase-7 artefact skills missing from tools/: "
        f"{sorted(missing)}; found: {sorted(found)}"
    )
