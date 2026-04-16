"""U5 (unverified): HookMatcher accepts regex in `matcher=`.

If verified, we could collapse 7 HookMatchers into 2 (one for Bash + regex for
file tools). Today we register 7 explicit matchers. xpass → safe to consolidate.
"""

from __future__ import annotations

import pytest
from claude_agent_sdk import HookMatcher


@pytest.mark.xfail(strict=False, reason="U5: HookMatcher regex matcher not verified")
def test_hookmatcher_accepts_regex() -> None:
    # The constructor accepts any string — this is purely an SDK-behaviour
    # question: does it dispatch based on regex match or exact tool name?
    matcher = HookMatcher(matcher="Ba.*", hooks=[])
    assert matcher.matcher == "Ba.*"

    raise AssertionError(
        "U5 unverified: we cannot confirm the SDK treats `matcher=` as a regex "
        "without a live PreToolUse flight. xpass → collapse to 2 matchers."
    )
