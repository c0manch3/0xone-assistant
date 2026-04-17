from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# Default truncate cap for a single tool_result snippet inside the synthetic
# note. Phase 4 Q1: the replay stream still does NOT include raw tool_use /
# tool_result blocks (SDK contract U1 remains unverified), so instead each
# multi-block turn gets a pre-pended `[system-note: ...]` with up to this
# many characters of each tool_result.content. Overridable at the call-site
# via `history_to_user_envelopes(..., tool_result_truncate=settings.memory.\
# history_tool_result_truncate_chars)`.
TOOL_RESULT_TRUNCATE = 2000


def _stringify_tool_result_content(content: Any) -> str:
    """Normalise `ToolResultBlock.content` into a display string.

    Spike S-B.1 observed `content: str` for every Bash variant (single-line
    JSON, multi-line Cyrillic, error-exit). The dataclass signature allows
    `str | list[dict] | None`; we keep the list/bytes branches as defensive
    future-proofing for SDK paths that may surface text+image blocks
    (Read binary, WebFetch HTML) without any empirical evidence in 0.1.59.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        # Never observed in 0.1.59; fall through to a placeholder so the
        # truncator does not explode on non-str input.
        return f"[binary content: {len(content)}B]"
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif btype in ("image", "image_url"):
                parts.append(f"[{btype} block]")
            else:
                parts.append(f"[{btype or 'unknown'} block]")
        return "".join(parts)
    return f"[non-text content: {type(content).__name__}]"


def _render_tool_summary(
    tool_names: list[str],
    tool_results_by_name: dict[str, list[dict[str, Any]]],
    truncate: int,
) -> str:
    """Build the synthetic `[system-note: ...]` body for one replayed turn.

    The model sees this note prepended to the text content of the next
    user envelope; it is never persisted to ConversationStore (the raw
    rows stay there for traceability).

    Output shape (Russian; the assistant is prompted in Russian):

        [system-note: в прошлом ходе вызваны инструменты: memory, Bash.
         результат memory: {"hits":[...]}
         ошибка Bash: Exit code 1\\nboom
         Для полного вывода вызови инструмент снова.]
    """
    lines: list[str] = ["в прошлом ходе вызваны инструменты: " + ", ".join(tool_names) + "."]
    for name in tool_names:
        for r in tool_results_by_name.get(name, []):
            text = _stringify_tool_result_content(r.get("content"))
            if len(text) > truncate:
                # Python slicing is code-point safe (spike S-B.4 verified on
                # Cyrillic). Strip trailing whitespace from the partial
                # chunk so the marker lands on a clean boundary.
                text = text[:truncate].rstrip() + "...(truncated)"
            prefix = "ошибка" if r.get("is_error") else "результат"
            lines.append(f"{prefix} {name}: {text}")
    lines.append("Для полного вывода вызови инструмент снова.")
    return "[system-note: " + "\n".join(lines) + "]"


def _build_tool_name_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Scan the WHOLE history; return `tool_use_id -> tool_name`.

    Correction (review wave 3, should-fix #8): the previous docstring
    claimed phase-2 `_run_turn` splits `tool_use` and `tool_result` into
    different `turn_id`s. That is NOT how phase 2 actually stores them —
    the handler allocates a single `turn_id` per user→assistant cycle
    and every block from that cycle shares it.

    The global (not per-turn) map is intentional future-proofing for
    cases where that invariant changes:

    * phase-5 scheduler may inject a memory-recall trigger as a fresh
      turn whose user message depends on a `tool_result` from the
      previous real turn;
    * SDK `resume=session_id` experiments (U4-1) may redistribute blocks
      across turns in ways phase 2 never did;
    * the B2 regression test (`test_history_toolname_map_spans_turns`)
      seeds the cross-turn split explicitly to exercise this path.

    Building the map once across all rows keeps `history_to_user_envelopes`
    correct regardless of how future phases partition the history.
    """
    m: dict[str, str] = {}
    for row in rows:
        if row.get("block_type") != "tool_use":
            continue
        content = row.get("content")
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tu_id = block.get("id")
            name = block.get("name")
            if isinstance(tu_id, str) and isinstance(name, str) and name:
                m[tu_id] = name
    return m


def history_to_user_envelopes(
    rows: list[dict[str, Any]],
    chat_id: int,
    *,
    tool_result_truncate: int = TOOL_RESULT_TRUNCATE,
) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows -> SDK user-envelope stream.

    Per spike R1: history is fed as a sequence of `"type":"user"` envelopes.
    Assistant turns do NOT need to be emitted back — the SDK reconstructs
    context from the user envelopes in stream order.

    Phase 4 Q1 extension: tool_use / tool_result blocks are STILL not
    replayed to the SDK directly (U1 unverified). Instead a synthetic
    `[system-note: ...]` is prepended to the first user envelope of any
    turn whose assistant side touched tools; the note now carries the
    first `tool_result_truncate` chars of each result content (plus
    `ошибка`/`результат` prefix based on `is_error`). Memory recall blocks
    are finally visible to the model across turns (phase 2 dropped them
    entirely).

    `ThinkingBlock` rows (`block_type='thinking'`) are skipped — SDK
    refuses cross-session thinking replay (R2).
    """
    session_id = f"chat-{chat_id}"
    tool_name_by_id = _build_tool_name_map(rows)  # B2: whole-history map

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
        results_by_name: dict[str, list[dict[str, Any]]] = {}

        for row in by_turn[turn_id]:
            if row["role"] == "user":
                for block in row["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            user_texts.append(text)
            elif row.get("block_type") == "tool_use":
                for block in row["content"]:
                    if not isinstance(block, dict):
                        continue
                    name = block.get("name")
                    if isinstance(name, str) and name and name not in tool_names:
                        tool_names.append(name)
            elif row.get("block_type") == "tool_result":
                for block in row["content"]:
                    if not isinstance(block, dict):
                        continue
                    tu_id = block.get("tool_use_id")
                    # B2: resolve via the GLOBAL map, not the per-turn one.
                    name = (
                        tool_name_by_id.get(tu_id) if isinstance(tu_id, str) else None
                    ) or "unknown"
                    if name not in tool_names:
                        tool_names.append(name)
                    results_by_name.setdefault(name, []).append(block)

        if not user_texts:
            continue

        if tool_names:
            note = _render_tool_summary(tool_names, results_by_name, truncate=tool_result_truncate)
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
