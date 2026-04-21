"""Tests for ``history_to_sdk_envelopes`` (post S13 fix).

Prior shape (per-row envelopes) caused the CLI to queue one pending prompt
per prior row, triggering multiple API iterations inside a single
``query()`` — the root cause of the S13 marker-drop incident. The new
shape emits AT MOST ONE collapsed context envelope rendering prior turns
as plain text, so the CLI sees exactly two pending prompts (context +
current) for any non-empty history.
"""

from __future__ import annotations

from assistant.bridge.history import history_to_sdk_envelopes


def _row(
    *,
    turn_id: str,
    role: str,
    block_type: str,
    content: list[dict[str, object]],
    row_id: int = 1,
) -> dict[str, object]:
    return {
        "id": row_id,
        "chat_id": 42,
        "turn_id": turn_id,
        "role": role,
        "content": content,
        "meta": None,
        "created_at": "2026-04-20T12:00:00Z",
        "block_type": block_type,
    }


def test_empty_history_yields_nothing() -> None:
    """No rows → no envelopes. The caller's own current-user envelope is
    the only thing the CLI sees in that case."""
    assert list(history_to_sdk_envelopes([], chat_id=1)) == []


def test_history_collapses_into_single_user_envelope() -> None:
    """S13 regression: even a multi-row, multi-role history must produce
    exactly ONE envelope — never N. Per-row envelopes trigger CLI queue
    batching and cause subsequent iterations to be dropped by bridge.ask.
    """
    rows = [
        _row(
            turn_id="t1",
            role="user",
            block_type="text",
            content=[{"type": "text", "text": "remember 777333"}],
            row_id=1,
        ),
        _row(
            turn_id="t1",
            role="assistant",
            block_type="text",
            content=[{"type": "text", "text": "Got it — 777333."}],
            row_id=2,
        ),
        _row(
            turn_id="t2",
            role="user",
            block_type="text",
            content=[{"type": "text", "text": "what number?"}],
            row_id=3,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=42))
    assert len(envs) == 1
    env = envs[0]
    assert env["type"] == "user"
    assert env["message"]["role"] == "user"
    content = env["message"]["content"]
    assert isinstance(content, str)
    # Text renders prior turns in order, prefixed by role label.
    assert "user: remember 777333" in content
    assert "assistant: Got it — 777333." in content
    assert "user: what number?" in content
    # Context markers present so the model knows not to respond to history.
    assert "Previous conversation context" in content
    assert "End of previous context" in content


def test_thinking_blocks_dropped() -> None:
    """U2: cross-session thinking signatures are unsafe. Thinking rows
    must not appear in the rendered context, but surrounding rows are
    still rendered."""
    rows = [
        _row(
            turn_id="t1",
            role="assistant",
            block_type="thinking",
            content=[
                {
                    "type": "thinking",
                    "thinking": "internal monologue",
                    "signature": "opaque",
                }
            ],
            row_id=1,
        ),
        _row(
            turn_id="t1",
            role="assistant",
            block_type="text",
            content=[{"type": "text", "text": "final answer"}],
            row_id=2,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=1))
    assert len(envs) == 1
    content = envs[0]["message"]["content"]
    assert isinstance(content, str)
    assert "internal monologue" not in content
    assert "final answer" in content


def test_tool_use_and_result_rendered_as_annotated_lines() -> None:
    """Tool use / tool result rows become short annotated lines so the
    model can see the cause/effect without us replaying structured blocks
    (which would reintroduce the multi-envelope queue problem)."""
    rows = [
        _row(
            turn_id="t1",
            role="assistant",
            block_type="tool_use",
            content=[
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "Bash",
                    "input": {"command": "ls"},
                }
            ],
            row_id=1,
        ),
        _row(
            turn_id="t1",
            role="user",
            block_type="tool_result",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "tu1",
                    "content": "file1\n",
                    "is_error": False,
                }
            ],
            row_id=2,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=1))
    assert len(envs) == 1
    content = envs[0]["message"]["content"]
    assert isinstance(content, str)
    assert "assistant: [invoked tool Bash]" in content
    assert "user: [tool_result:" in content
    assert "file1" in content


def test_only_thinking_blocks_yields_nothing() -> None:
    """If the only rows are thinking blocks (dropped by U2 rule), we
    should emit nothing rather than an empty context envelope."""
    rows = [
        _row(
            turn_id="t1",
            role="assistant",
            block_type="thinking",
            content=[
                {
                    "type": "thinking",
                    "thinking": "x",
                    "signature": "y",
                }
            ],
            row_id=1,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=1))
    assert envs == []


def test_session_id_preserved_for_logging_consistency() -> None:
    """R10: ``session_id`` on envelopes is cosmetic (SDK assigns a UUID),
    but we keep it stable per chat so log correlation still works."""
    rows = [
        _row(
            turn_id="t1",
            role="user",
            block_type="text",
            content=[{"type": "text", "text": "hi"}],
            row_id=1,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=99))
    assert len(envs) == 1
    assert envs[0]["session_id"] == "chat-99"
    assert envs[0]["parent_tool_use_id"] is None
