"""U1 (unverified): direct replay of tool_use/tool_result blocks in history.

Phase 2 intentionally avoids re-emitting SDK tool blocks — instead a synthetic
system-note is prepended. This test documents the *future* contract where we
feed tool_use/tool_result verbatim and expect SDK to accept them. It is marked
xfail(strict=False): xpass signals we can enable real replay in phase 3+.

The test is a pure unit — no real SDK call. It builds the envelope our bridge
*would* emit under a hypothetical "replay tool blocks" mode and asserts the
SDK contract we believe holds. Until U1 is empirically validated, this stays
xfail.
"""

from __future__ import annotations

import pytest


@pytest.mark.xfail(strict=False, reason="U1: tool_use/tool_result replay not verified")
def test_tool_block_replay_in_history_envelope() -> None:
    # Hypothetical replay envelope shape (NOT produced by phase 2 bridge):
    replay_envelope = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "ok",
                    "is_error": False,
                },
                {"type": "text", "text": "now continue"},
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "chat-1",
    }

    # The invariant we'd assert post-verification: SDK accepts this envelope.
    # Until proven, fail deliberately so xfail stays in force.
    raise AssertionError(
        "U1 unverified: phase 2 bridge does NOT replay tool blocks; "
        "xpass means replay is safe to enable."
    )
    # Unreachable — kept to document the post-verification check.
    assert replay_envelope["message"]["content"][0]["type"] == "tool_use"
