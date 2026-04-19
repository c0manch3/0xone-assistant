"""URL detector: regex behaviour + S-7 edge cases + handler contract that
the ConversationStore gets the ORIGINAL text (not the enriched envelope)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler, _detect_urls
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

# ---------------------------------------------------------------- regex


def test_url_detector_plain_https() -> None:
    assert _detect_urls("посмотри https://example.com/x") == ["https://example.com/x"]


def test_url_detector_strips_trailing_punctuation() -> None:
    # "вот URL: https://github.com/x/y., дальше"
    assert _detect_urls("тут https://github.com/x/y., дальше") == ["https://github.com/x/y"]


def test_url_detector_handles_parens_without_eating_url() -> None:
    # "см. (https://github.com/x/y)."
    assert _detect_urls("см. (https://github.com/x/y).") == ["https://github.com/x/y"]


def test_url_detector_markdown_link() -> None:
    # "[link](https://github.com/x/y)" — brackets & parens are excluded.
    assert _detect_urls("[link](https://github.com/x/y)") == ["https://github.com/x/y"]


def test_url_detector_preserves_encoded_chars() -> None:
    assert _detect_urls("file https://github.com/x/y%20z done") == ["https://github.com/x/y%20z"]


def test_url_detector_two_urls() -> None:
    urls = _detect_urls("два: http://a.com и git@github.com:x/y")
    assert len(urls) == 2
    assert "http://a.com" in urls
    assert "git@github.com:x/y" in urls


def test_url_detector_no_match() -> None:
    assert _detect_urls("без урла") == []


def test_url_detector_caps_at_three() -> None:
    text = " ".join(f"https://a{i}.com" for i in range(10))
    assert len(_detect_urls(text)) == 3


# ---------------------------------------------------------------- handler contract


class _FakeBridge:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None
        self.last_user_text: str | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, history, image_blocks
        self.last_system_notes = list(system_notes) if system_notes is not None else None
        self.last_user_text = user_text
        for item in self._items:
            yield item


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.01,
        usage=None,
        result="ok",
        uuid="u",
    )


async def test_handler_passes_system_notes_when_url_present(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "u.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _FakeBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="ok"),
            _result(),
        ]
    )
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(chat_id=42, text="поставь https://github.com/x/y")
    await handler.handle(msg, emit)

    # Ephemeral enrichment went to the bridge ...
    assert bridge.last_system_notes is not None
    assert len(bridge.last_system_notes) == 1
    assert "https://github.com/x/y" in bridge.last_system_notes[0]

    # ... but ConversationStore saw the ORIGINAL text only.
    async with conn.execute(
        "SELECT content_json FROM conversations WHERE chat_id = ? AND role = 'user'",
        (42,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    import json

    content = json.loads(row[0])
    assert content == [{"type": "text", "text": "поставь https://github.com/x/y"}]

    # user_text passed to bridge remains the original — enrichment sits in
    # system_notes, which the bridge builds into the envelope at send time.
    assert bridge.last_user_text == "поставь https://github.com/x/y"

    await conn.close()


async def test_handler_no_system_notes_when_no_url(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "u2.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    bridge = _FakeBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            _result(),
        ]
    )
    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    await handler.handle(IncomingMessage(chat_id=7, text="без урла"), emit)
    assert bridge.last_system_notes is None
    await conn.close()
