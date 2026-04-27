"""Phase 6a fix-pack — ``_classify_block`` ToolResultBlock unit tests.

Spec §E mandates explicit unit tests asserting ``_classify_block``
handles:
  (a) the empty case where the Read tool wasn't invoked;
  (b) the case where the Read tool was invoked with a text-only
      ``ToolResultBlock.content`` (string);
  (c) defensive: a ``ToolResultBlock`` with multimodal-list content
      (phase 6a doesn't expect this but the encoder must not crash).

The handler persists every assistant/user envelope by JSON-serialising
the payload via ``json.dumps`` — these tests pin the round-trip so any
future SDK shape change to ``ToolResultBlock.content`` (e.g. ``list[
ContentBlock]``) is caught here, not at runtime in production.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import ToolResultBlock, ToolUseBlock

from assistant.handlers.message import _classify_block


def test_classify_block_empty_read_case_skips_text_out() -> None:
    """No ``ToolResultBlock`` in the bridge stream — ``_classify_block``
    on a synthetic ``ToolUseBlock(name="Read", ...)`` returns
    ``role='assistant', block_type='tool_use'`` and ``text_out=None``.

    The handler emits nothing to the user for tool_use blocks (no
    ``text_out``); the Read tool is invisible to the chat UI.
    """
    block = ToolUseBlock(
        id="toolu_01",
        name="Read",
        input={"file_path": "/app/.uploads/abc__sample.pdf"},
    )
    role, payload, text_out, block_type = _classify_block(block)

    assert role == "assistant"
    assert block_type == "tool_use"
    assert text_out is None
    assert payload["type"] == "tool_use"
    assert payload["id"] == "toolu_01"
    assert payload["name"] == "Read"
    assert payload["input"] == {"file_path": "/app/.uploads/abc__sample.pdf"}

    # Round-trips through json.dumps without raising.
    json.dumps(payload, ensure_ascii=False)


def test_classify_block_text_only_tool_result_round_trips_through_json() -> None:
    """Read tool returned text-only content — ``_classify_block``
    classifies it as ``role='user', block_type='tool_result'`` (B5
    contract) and the payload round-trips through ``json.dumps``.

    Catches a future SDK shape change (e.g. ``content: list[
    ContentBlock]``) — the encoder would explode and the test fails
    loudly here rather than silently corrupting the persisted history
    on production.
    """
    extracted_pdf_text = "Page 1 text\nPage 2 text\nКириллица OK"
    block = ToolResultBlock(
        tool_use_id="toolu_01",
        content=extracted_pdf_text,
        is_error=False,
    )
    role, payload, text_out, block_type = _classify_block(block)

    # B5: SDK streaming-input mode requires tool_result on the USER
    # envelope. Storing with role='tool' silently drops on replay.
    assert role == "user"
    assert block_type == "tool_result"
    assert text_out is None
    assert payload["type"] == "tool_result"
    assert payload["tool_use_id"] == "toolu_01"
    assert payload["content"] == extracted_pdf_text
    assert payload["is_error"] is False

    # Round-trips through json.dumps with ensure_ascii=False (cyrillic).
    encoded = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["content"] == extracted_pdf_text
    assert "Кириллица" in encoded


def test_classify_block_multimodal_list_tool_result_does_not_crash() -> None:
    """Defensive: a ``ToolResultBlock`` with multimodal-list content
    (phase 6a doesn't expect this — Read tool over OAuth-CLI returns a
    string per RQ1 — but a future SDK shape change must NOT crash
    ``_classify_block``).

    The classifier passes ``content`` through verbatim; downstream JSON
    encoding handles the list-of-dicts shape natively.
    """
    multimodal_content: list[dict[str, Any]] = [
        {"type": "text", "text": "extracted text from page 1"},
        {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
    ]
    block = ToolResultBlock(
        tool_use_id="toolu_02",
        content=multimodal_content,  # type: ignore[arg-type]
        is_error=False,
    )

    # ``_classify_block`` MUST NOT raise on the unexpected shape.
    role, payload, _text_out, block_type = _classify_block(block)
    assert role == "user"
    assert block_type == "tool_result"
    assert payload["content"] == multimodal_content

    # JSON-serialisation is the load-bearing contract: the persisted
    # row must be decodable later. Any future SDK shape that breaks
    # this fails here loudly instead of corrupting history silently.
    encoded = json.dumps(payload, ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["content"][0]["text"] == "extracted text from page 1"
    assert decoded["content"][1]["type"] == "image"


def test_classify_block_tool_result_error_flag_preserved() -> None:
    """``ToolResultBlock(is_error=True)`` (e.g. Read tool refused by
    the file-tool hook) round-trips with ``is_error=True``. The handler
    relies on this field for error replay semantics.
    """
    block = ToolResultBlock(
        tool_use_id="toolu_03",
        content="permission denied",
        is_error=True,
    )
    role, payload, _text_out, block_type = _classify_block(block)
    assert role == "user"
    assert block_type == "tool_result"
    assert payload["is_error"] is True
    assert payload["content"] == "permission denied"
