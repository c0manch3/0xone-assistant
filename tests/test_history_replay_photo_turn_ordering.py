"""Phase 7 commit 13 — H-10 history placeholder anchors to originating turn.

Scenario
--------
Two complete user turns, each with a text block AND an image block on
the `user` row. On replay, the bridge.history module must emit one
placeholder per image, embedding the source `turn_id` so a model reading
the replayed envelopes can tell the two images apart even when block
order within a row is subject to phase-2 insertion-order semantics.

Why turn_id anchoring matters
-----------------------------
Without `turn_id` in the placeholder, a transcript like

    [user] → [image omitted, image omitted]
    [user] → text for turn 1
    [user] → text for turn 2

would appear as two interchangeable image placeholders, ruining
reasoning of the form "the photo you sent earlier vs. just now". The
placeholder carries `turn_id=<id>` so the model can pair the image
note with the right text block even if future history flattening
reorders them.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from assistant.bridge.history import history_to_user_envelopes
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import _ensure_turn


async def _seed_user_image_plus_text(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    text: str,
    media_type: str = "image/jpeg",
) -> None:
    """Insert a user row carrying BOTH an image block AND a text block.

    Phase-2 `ConversationStore.append` persists the content list verbatim;
    this helper mirrors that shape so the replay exercises the same code
    path a future phase-7b persistence change would trigger.
    """
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": "<omitted>"},
        },
        {"type": "text", "text": text},
    ]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'user', ?, 'text')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )
    await conn.commit()


async def test_two_image_turns_replay_with_distinct_turn_id_placeholders(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "rep.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 1

    # Seed turn 1: image + text
    await _seed_user_image_plus_text(
        conn,
        chat_id=chat_id,
        turn_id="turn-one",
        text="first question",
    )
    # Seed turn 2: image + text
    await _seed_user_image_plus_text(
        conn,
        chat_id=chat_id,
        turn_id="turn-two",
        text="second question",
        media_type="image/png",
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))

    assert len(envelopes) == 2

    # --- Turn 1 envelope ---
    t1_content = envelopes[0]["message"]["content"]
    assert isinstance(t1_content, list)
    t1_texts = [b["text"] for b in t1_content if b.get("type") == "text"]
    # Exactly one image placeholder referencing turn-one.
    image_notes_t1 = [
        t
        for t in t1_texts
        if "[system-note:" in t and "turn turn-one" in t and "image (image/jpeg)" in t
    ]
    assert len(image_notes_t1) == 1
    assert "first question" in " ".join(t1_texts)
    # Placeholder MUST NOT leak into turn-two.
    assert all("turn-two" not in t for t in t1_texts)

    # --- Turn 2 envelope ---
    t2_content = envelopes[1]["message"]["content"]
    assert isinstance(t2_content, list)
    t2_texts = [b["text"] for b in t2_content if b.get("type") == "text"]
    image_notes_t2 = [
        t
        for t in t2_texts
        if "[system-note:" in t and "turn turn-two" in t and "image (image/png)" in t
    ]
    assert len(image_notes_t2) == 1
    assert "second question" in " ".join(t2_texts)
    assert all("turn-one" not in t for t in t2_texts)

    await conn.close()


async def test_image_placeholder_appears_before_user_text_within_turn(
    tmp_path: Path,
) -> None:
    """Block order within one turn: image → text (matches phase-2
    insertion order in the seeded row). The placeholder MUST precede
    the accompanying user text in the output list so the model reads
    "image context first, then user question"."""
    conn = await connect(tmp_path / "ord.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 2

    await _seed_user_image_plus_text(
        conn,
        chat_id=chat_id,
        turn_id="solo-turn",
        text="what is this?",
    )

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    assert len(envelopes) == 1
    content = envelopes[0]["message"]["content"]
    assert isinstance(content, list)
    texts = [b["text"] for b in content if b.get("type") == "text"]
    # Placeholder first, user text second.
    assert texts[0].startswith("[system-note:")
    assert "turn solo-turn" in texts[0]
    assert texts[1] == "what is this?"

    await conn.close()
