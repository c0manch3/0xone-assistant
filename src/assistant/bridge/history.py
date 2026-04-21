from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def history_to_sdk_envelopes(
    rows: list[dict[str, Any]], chat_id: int
) -> Iterator[dict[str, Any]]:
    """Render prior conversation history as a single plain-text context
    envelope.

    Fix B (incident S13): the previous implementation emitted **one
    stream_input envelope per prior row (or per consecutive same-role
    group)**. The Claude Code CLI treats each ``type: 'user'`` envelope
    as a **separate pending prompt** — so a 9-row history produced ~10
    pending prompts, and the CLI processed them sequentially across N
    API iterations inside a single ``query()``.  Combined with the
    bridge's original ``return`` after the first ``ResultMessage``, this
    silently dropped every iteration beyond the first — the owner saw
    stale greetings on every turn instead of the real (latest) reply.

    Fixed shape: we emit **at most one** user envelope whose content is
    a plain-text transcript of the prior turns, wrapped in markers that
    tell the model "this is context, not a live prompt". The caller
    appends the current user message as a separate envelope, so the CLI
    sees exactly two pending prompts (context + current) and runs one
    API iteration — matching every other chat integration we've seen.

    Rules:
      - Thinking blocks are dropped (U2: SDK rejects cross-session
        thinking signatures).
      - Unknown block shapes are skipped defensively.
      - Empty history → no envelopes at all.
      - Tool use / tool results are rendered as short, annotated lines
        so the model can see what happened without us having to replay
        the structured blocks (which would reintroduce the multi-envelope
        queue behaviour).
    """
    if not rows:
        return

    lines: list[str] = []
    for row in rows:
        btype = row.get("block_type") or "text"  # R12 defence
        if btype == "thinking":
            # U2: SDK rejects cross-session thinking signature.
            continue

        role = row.get("role")
        if role not in ("user", "assistant"):
            continue

        blocks = row.get("content") or []
        if not isinstance(blocks, list):
            blocks = [blocks]

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype_inner = block.get("type")
            if btype_inner == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    lines.append(f"{role}: {text}")
            elif btype_inner == "tool_use":
                name = block.get("name", "?")
                lines.append(f"{role}: [invoked tool {name}]")
            elif btype_inner == "tool_result":
                content = block.get("content")
                snippet = str(content)[:200] if content is not None else "(empty)"
                lines.append(f"{role}: [tool_result: {snippet}]")
            # Unknown block types intentionally skipped.

    if not lines:
        return

    body = "\n".join(lines)
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "[Previous conversation context — for your reference; "
                "do not respond to it directly unless the current message "
                "references it]\n"
                f"{body}\n"
                "[End of previous context]"
            ),
        },
        "parent_tool_use_id": None,
        # R10: cosmetic — SDK assigns its own UUID per query.
        "session_id": f"chat-{chat_id}",
    }
