"""Q11 / R-13 regression — ARTEFACT_RE false-positive rate on gh CLI corpus.

Phase-8 introduces new model-reply shapes (gh issue bodies, vault-commit-push
JSON, git porcelain, cyrillic/emoji status lines, code-block JSON, github
user-avatar URLs). The phase-7 ``ARTEFACT_RE v3`` must NOT match any of
them — otherwise the scheduler's ``dispatch_reply`` would try to send a
non-existent file through ``MessengerAdapter.send_*`` and crash the turn.

We assert: for every non-blank corpus line, ``ARTEFACT_RE.search(line) is
None``. The corpus is ``tests/fixtures/gh_responses.txt`` (copied from
``spikes/phase8/spike_artefact_re_corpus.txt`` and extended per S2 with
cyrillic/emoji/code-block/URL-png/nested-json cases).

Import path note (SF-D2): ``ARTEFACT_RE`` lives in
``assistant.media.artefacts`` (module-level ``Final`` constant), NOT in
``adapters/``. Verified against the repo's actual package layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Importing from ``assistant.media.artefacts`` directly — the regex is a
# module-level ``Final[re.Pattern[str]]`` so a bare attribute import is
# fine and avoids any dispatch-reply integration side effects.
from assistant.media.artefacts import ARTEFACT_RE

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "gh_responses.txt"
_CORPUS_LINES: list[str] = [
    line
    for line in _CORPUS_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
]


def test_corpus_non_empty() -> None:
    """Sanity guard: if the fixture file moves or becomes empty, fail early
    with a clear message rather than reporting "0 parametrized tests"."""

    assert len(_CORPUS_LINES) >= 50, (
        f"corpus too small: {len(_CORPUS_LINES)} lines — expected ≥50 per S2"
    )


@pytest.mark.parametrize("line", _CORPUS_LINES, ids=range(len(_CORPUS_LINES)))
def test_no_false_positive(line: str) -> None:
    """``ARTEFACT_RE.search`` must return ``None`` on every corpus line.

    If this test fails, a new scheduler-turn output shape slipped
    through. Either the corpus has a genuine absolute path to an outbox
    extension (remove it from the fixture — it's not a false positive
    scenario), or the regex drifted (fix the regex and document in a
    new spike report).
    """
    match = ARTEFACT_RE.search(line)
    assert match is None, (
        f"false positive: {line!r} matched as artefact {match.group(0)!r}"
    )
