"""U2 (unverified): SDK rejects cross-session ThinkingBlock replay.

Phase 2 strips thinking blocks from history envelopes before feeding them to
the SDK. This test documents the *reason*: we believe the SDK refuses to
accept foreign thinking blocks. Until empirically confirmed (would require a
real SDK call), the test stays xfail.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=False, reason="U2: cross-session thinking rejection unverified")
def test_sdk_rejects_foreign_thinking_block() -> None:
    # The envelope we'd need to feed to trigger rejection:
    envelope_with_thinking = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "foreign reasoning",
                    "signature": "fake-signature",
                }
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "chat-1",
    }

    raise AssertionError(
        "U2 unverified: requires a real SDK call with a foreign thinking block "
        "to confirm rejection. xpass → safe to relax thinking-filter in history."
    )
    assert envelope_with_thinking  # unreachable marker
