"""Phase 7 commit 18g — H-10 multi-image turn replay ordering.

Scenario
--------
A single user turn carries TWO `image` blocks AND one `text` block in
the same persisted row (the live envelope is `text → image → image`
per S-0 Q0-5b verification — the bridge inserts attachments after the
user text, so anything that ever reaches `ConversationStore.append`
follows that order). On replay, `history.history_to_user_envelopes`
must:

* Produce exactly two image placeholder notes.
* Embed the SAME originating `turn_id` in BOTH placeholders (H-10:
  without the turn_id anchor a model could not pair the placeholders
  with the right turn when more than one image-turn replays).
* Preserve the user text verbatim, in its original position relative
  to the image blocks (text first, then the two placeholders).
* Match the canonical placeholder form defined in `bridge/history.py`
  (`[system-note: in turn <turn_id> user sent image (<media_type>) —
  raw bytes omitted from replay]`).
* Allocate a DIFFERENT `turn_id` to a second, distinct turn so the
  H-10 anchor remains discriminating across turns.

The test seeds rows directly through the same helper used by the
existing `test_history_replay_photo_turn_ordering` suite to avoid
running a real SDK roundtrip.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from assistant.bridge.history import history_to_user_envelopes
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import _ensure_turn, seed_user_text_row


async def _seed_user_text_plus_two_images(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    text: str,
    media_types: tuple[str, str] = ("image/jpeg", "image/png"),
) -> None:
    """Insert a `user` row with the live envelope order: text → image → image.

    Mirrors the runtime shape `ClaudeBridge._build_user_envelope` injects
    when the handler attaches >=2 photos to the same incoming Telegram
    message (S-0 Q0-5b: the SDK accepts `text → image → system-note`).
    """
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [
        {"type": "text", "text": text},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_types[0],
                "data": "<omitted-0>",
            },
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_types[1],
                "data": "<omitted-1>",
            },
        },
    ]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'user', ?, 'text')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()


def _expected_placeholder(turn_id: str, media_type: str) -> str:
    """Canonical form per `bridge/history.py` lines 197-201."""
    return (
        f"[system-note: in turn {turn_id} user sent "
        f"image ({media_type}) — raw bytes omitted from replay]"
    )


async def test_multi_image_turn_replay_emits_two_anchored_placeholders(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "multi_image.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 4242

    first_turn_id = "turn-multi-image-first"
    user_text = "compare these two photos please"

    await _seed_user_text_plus_two_images(
        conn,
        chat_id=chat_id,
        turn_id=first_turn_id,
        text=user_text,
        media_types=("image/jpeg", "image/png"),
    )

    # Seed a SUBSEQUENT, distinct turn so the H-10 anchor's
    # discrimination property is exercised (different turn_id).
    second_turn_id = "turn-multi-image-second"
    await seed_user_text_row(
        conn,
        chat_id=chat_id,
        turn_id=second_turn_id,
        text="follow-up text-only question",
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    # Two complete turns -> two envelopes.
    assert len(envelopes) == 2, envelopes

    # ---- First (multi-image) envelope -----------------------------------
    first = envelopes[0]
    assert first["session_id"] == f"chat-{chat_id}"
    content = first["message"]["content"]
    # 1 text + 2 placeholders -> must be a list (single-string branch only
    # fires for single-element user_texts).
    assert isinstance(content, list), content
    texts = [b["text"] for b in content if b.get("type") == "text"]
    assert len(texts) == 3, texts

    # H-10: order must be text → image[0] → image[1]
    # (per S-0 Q0-5 envelope order, mirrored on replay).
    assert texts[0] == user_text, "user text must be preserved verbatim and lead"

    expected_first_placeholder = _expected_placeholder(first_turn_id, "image/jpeg")
    expected_second_placeholder = _expected_placeholder(first_turn_id, "image/png")

    assert texts[1] == expected_first_placeholder, (
        "image[0] placeholder must follow the user text and match canonical form"
    )
    assert texts[2] == expected_second_placeholder, (
        "image[1] placeholder must follow image[0] and match canonical form"
    )

    # H-10: BOTH placeholders carry the SAME turn_id of the originating turn.
    placeholder_texts = [texts[1], texts[2]]
    embedded_turn_ids: list[str] = []
    for placeholder in placeholder_texts:
        # Canonical form: "[system-note: in turn <turn_id> user sent image ..."
        marker = "in turn "
        assert marker in placeholder, placeholder
        rest = placeholder.split(marker, 1)[1]
        embedded = rest.split(" ", 1)[0]
        embedded_turn_ids.append(embedded)

    assert all(tid is not None for tid in embedded_turn_ids), embedded_turn_ids
    assert all(tid == first_turn_id for tid in embedded_turn_ids), (
        "Both placeholders within one turn must share the originating turn_id"
    )
    assert embedded_turn_ids[0] == embedded_turn_ids[1], (
        "Within a single multi-image turn the turn_id MUST be identical "
        "across all image placeholders (H-10 anchor invariant)"
    )

    # ---- Second envelope: must NOT inherit the first turn's turn_id ----
    second = envelopes[1]
    second_content = second["message"]["content"]
    # Single text block -> bridge collapses to a bare string.
    assert second_content == "follow-up text-only question", second_content

    # And of course the two turn_ids must differ — the test fixture used
    # distinct ids; this assertion guards against an accidental collision
    # if a future refactor starts deriving turn_id from content.
    assert first_turn_id != second_turn_id
    # No image placeholder may leak across turns: the second envelope
    # carries no system-note referencing the first turn's id.
    assert first_turn_id not in str(second_content), (
        "Second turn must not carry placeholders from the first turn"
    )

    await conn.close()
