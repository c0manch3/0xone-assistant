"""Phase 5 fix-pack CRITICAL #2 — `_on_text` must route through `send_text`
so the `TelegramRetryAfter` retry loop (wave-2 G-W2-1) also protects
user-reply delivery, not just scheduler delivery.

Before the fix, `_on_text` called `self._bot.send_message` directly in a
loop over chunks, duplicating the split + send contract and skipping the
retry-after shield. A burst of 429s during a long conversation would
raise out of `_on_text`, which would break the aiogram handler with no
graceful recovery.

This test monkey-patches `Bot.send_message` to raise `TelegramRetryAfter`
on the first call and succeed on the second, then invokes `_on_text`
with a fake aiogram `Message`. A single logical reply must land.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter

from assistant.adapters.base import IncomingMessage
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import ClaudeSettings, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    """Minimum surface the handler needs: `.text`, `.chat.id`, `.message_id`."""

    def __init__(self, chat_id: int, text: str, message_id: int = 1) -> None:
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.message_id = message_id


class _EchoHandler:
    """Minimal handler that echoes the incoming text once."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.received: list[IncomingMessage] = []

    async def handle(self, msg: IncomingMessage, emit: Any) -> None:
        self.received.append(msg)
        await emit(self._reply)


async def test_on_text_retries_429_and_delivers_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _EchoHandler("привет")
    adapter.set_handler(handler)  # type: ignore[arg-type]

    sends: list[tuple[int, str]] = []
    attempt_counter = {"n": 0}

    async def flaky_send(**kwargs: Any) -> None:
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            raise TelegramRetryAfter(method=None, message="slow", retry_after=1)  # type: ignore[arg-type]
        sends.append((kwargs["chat_id"], kwargs["text"]))

    # Silence the retry-after sleep so the test completes quickly.
    async def fake_sleep(duration: float) -> None:
        del duration

    # Patch the ChatActionSender context so we don't hit the aiogram session.
    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def fake_typing(**_kwargs: Any) -> _NoopCtx:
        return _NoopCtx()

    monkeypatch.setattr(adapter._bot, "send_message", flaky_send)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("assistant.adapters.telegram.ChatActionSender.typing", fake_typing)

    msg = _FakeMessage(chat_id=42, text="hi")
    await adapter._on_text(msg)  # type: ignore[arg-type]

    # One reply landed after the retry-after sleep.
    assert sends == [(42, "привет")]
    # Handler got the message once.
    assert len(handler.received) == 1


async def test_on_text_empty_reply_still_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the consolidation must preserve the empty-reply fast path."""
    adapter = TelegramAdapter(_settings(tmp_path))
    handler = _EchoHandler("")  # emits empty string
    adapter.set_handler(handler)  # type: ignore[arg-type]

    sends: list[tuple[int, str]] = []

    async def fake_send(**kwargs: Any) -> None:
        sends.append((kwargs["chat_id"], kwargs["text"]))

    class _NoopCtx:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def fake_typing(**_kwargs: Any) -> _NoopCtx:
        return _NoopCtx()

    monkeypatch.setattr(adapter._bot, "send_message", fake_send)
    monkeypatch.setattr("assistant.adapters.telegram.ChatActionSender.typing", fake_typing)

    await adapter._on_text(_FakeMessage(chat_id=42, text="hi"))  # type: ignore[arg-type]
    assert sends == []
