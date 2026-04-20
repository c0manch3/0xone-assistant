from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def history_to_sdk_envelopes(rows: list[dict[str, Any]], chat_id: int) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows → SDK streaming-input envelope stream.

    Row → envelope mapping (R13-verified, S7 fix):
      role='user',      block_type='text'        → user envelope (text block)
      role='user',      block_type='tool_result' → user envelope (tool_result block)
      role='assistant', block_type='text'        → assistant envelope (text block)
      role='assistant', block_type='tool_use'    → assistant envelope (tool_use block)
      block_type='thinking'                      → DROPPED
        (U2: SDK rejects cross-session thinking signature.)

    R12 defence: treat NULL ``block_type`` as ``'text'`` (shouldn't happen
    after migration 0002 but the runtime default is free insurance).
    R10 note: ``session_id`` is cosmetic; the SDK ignores it in streaming-
    input mode and assigns its own UUID. We keep it so the envelope stays
    self-consistent with our logs.

    Rows are grouped by consecutive ``(turn_id, role)`` so that multiple
    same-role rows within a turn become a single multi-block envelope,
    matching the shape the SDK originally produced.
    """
    session_id = f"chat-{chat_id}"

    # Preserve temporal order (load_recent already ORDER BY id ASC).
    current_key: tuple[str, str] | None = None
    buffer: list[dict[str, Any]] = []

    def flush() -> Iterator[dict[str, Any]]:
        if not buffer or current_key is None:
            return
        _turn_id, role = current_key
        content: Any
        # Collapse a single user text-block to the plain-string shape —
        # the SDK accepts either, and mirroring the original turn is
        # friendlier to the model.
        if len(buffer) == 1 and buffer[0].get("type") == "text" and role == "user":
            content = buffer[0]["text"]
        else:
            content = list(buffer)
        envelope: dict[str, Any] = {
            "type": role,  # "user" or "assistant"
            "message": {"role": role, "content": content},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
        yield envelope

    for row in rows:
        btype = row.get("block_type") or "text"  # R12 defence
        if btype == "thinking":
            # U2: SDK rejects cross-session thinking signature.
            continue

        role = row["role"]
        # Normalize role: B5 — DB stores tool_result rows with role='user'
        # (because the handler classifies ToolResultBlock as role='user'
        # per the Anthropic tools API). No remapping needed here.
        turn_id = row["turn_id"]
        key = (turn_id, role)

        if current_key != key:
            # Boundary — flush accumulated buffer for the previous (turn, role).
            yield from flush()
            current_key = key
            buffer = []

        blocks = row.get("content") or []
        if isinstance(blocks, list):
            buffer.extend(blocks)
        else:
            # Defensive: legacy single-block dict.
            buffer.append(blocks)

    # Final flush after loop.
    yield from flush()
