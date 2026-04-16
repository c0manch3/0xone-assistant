from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def history_to_user_envelopes(rows: list[dict[str, Any]], chat_id: int) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows → SDK user-envelope stream.

    Per spike R1: history is fed as a sequence of `"type":"user"` envelopes.
    Assistant turns do NOT need to be emitted back — the SDK reconstructs
    context from the user envelopes in stream order.

    Phase 2 simplification (U1 unverified): tool_use/tool_result blocks are
    NOT replayed. To prevent the model from naively repeating an already-done
    tool call, we prepend a synthetic system-note to the first user envelope
    of any turn whose assistant side touched tools:

        [system-note: в прошлом ходе были вызваны инструменты: <names>.
         Результаты получены.]

    ThinkingBlocks (block_type='thinking') are skipped — SDK refuses
    cross-session thinking replay (R2).
    """
    session_id = f"chat-{chat_id}"

    # Group rows by turn_id preserving first-seen order.
    by_turn: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        if row.get("block_type") == "thinking":
            continue
        turn_id = row["turn_id"]
        if turn_id not in by_turn:
            by_turn[turn_id] = []
            order.append(turn_id)
        by_turn[turn_id].append(row)

    for turn_id in order:
        user_texts: list[str] = []
        tool_names: list[str] = []
        for row in by_turn[turn_id]:
            if row["role"] == "user":
                for block in row["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            user_texts.append(text)
            elif row.get("block_type") == "tool_use":
                for block in row["content"]:
                    if isinstance(block, dict):
                        name = block.get("name")
                        if isinstance(name, str) and name and name not in tool_names:
                            tool_names.append(name)

        if not user_texts:
            # Turn without a user-side text — nothing to feed.
            continue

        if tool_names:
            note = (
                "[system-note: в прошлом ходе были вызваны инструменты: "
                f"{', '.join(tool_names)}. Результаты получены.]"
            )
            user_texts = [note, *user_texts]

        content: str | list[dict[str, Any]]
        if len(user_texts) == 1:
            content = user_texts[0]
        else:
            content = [{"type": "text", "text": t} for t in user_texts]

        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
