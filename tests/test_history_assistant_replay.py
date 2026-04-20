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


def test_user_assistant_user_sequence() -> None:
    """R13 / S7: three consecutive rows (user, assistant, user) → three
    envelopes in order, each with the correct `type` field."""
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
    assert [e["type"] for e in envs] == ["user", "assistant", "user"]
    assert envs[0]["message"] == {"role": "user", "content": "remember 777333"}
    assert envs[1]["message"]["role"] == "assistant"
    assert envs[1]["message"]["content"] == [{"type": "text", "text": "Got it — 777333."}]
    assert envs[2]["message"] == {"role": "user", "content": "what number?"}


def test_thinking_blocks_dropped() -> None:
    """U2: cross-session thinking signature is unsafe — drop the row."""
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
    assert envs[0]["type"] == "assistant"
    assert envs[0]["message"]["content"] == [{"type": "text", "text": "final answer"}]


def test_tool_use_and_tool_result_preserved() -> None:
    """ToolUseBlock lives on an assistant envelope; ToolResultBlock lives
    on a user envelope (B5 classification round-trips correctly)."""
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
    assert [e["type"] for e in envs] == ["assistant", "user"]
    assert envs[0]["message"]["content"][0]["type"] == "tool_use"
    assert envs[1]["message"]["content"][0]["type"] == "tool_result"


def test_same_role_rows_within_turn_merge_into_one_envelope() -> None:
    """Multiple consecutive same-(turn, role) rows collapse into one
    multi-block envelope — matches the original SDK shape."""
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
            role="assistant",
            block_type="text",
            content=[{"type": "text", "text": "Here's the listing."}],
            row_id=2,
        ),
    ]
    envs = list(history_to_sdk_envelopes(rows, chat_id=1))
    assert len(envs) == 1
    assert envs[0]["type"] == "assistant"
    content = envs[0]["message"]["content"]
    assert isinstance(content, list)
    assert [b["type"] for b in content] == ["tool_use", "text"]


def test_empty_history_yields_nothing() -> None:
    assert list(history_to_sdk_envelopes([], chat_id=1)) == []
