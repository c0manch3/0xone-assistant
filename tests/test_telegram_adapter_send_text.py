"""Phase 5 / wave-2 G-W2-1 — Telegram adapter send_text retry on
TelegramRetryAfter.

Three cases:
  * Normal send — exactly one call to `Bot.send_message`.
  * RetryAfter on first call, success on second — sleep observed, single
    logical `send_text` completes without raising.
  * RetryAfter on first three calls — fourth is gated by MAX_ATTEMPTS
    and the exception propagates.

Uses the same `Bot.send_message` monkeypatching pattern as phase-2
spike S-2 — no real Telegram traffic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter

from assistant.adapters.telegram import TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS, TelegramAdapter
from assistant.config import ClaudeSettings, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


async def test_send_text_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    calls: list[tuple[int, str]] = []

    async def fake_send_message(**kwargs: Any) -> Any:
        calls.append((kwargs["chat_id"], kwargs["text"]))
        return None

    monkeypatch.setattr(adapter._bot, "send_message", fake_send_message)
    await adapter.send_text(42, "hello")
    assert calls == [(42, "hello")]


async def test_send_text_retries_on_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))
    calls: list[tuple[int, str]] = []
    attempt_counter = {"n": 0}

    async def flaky_send_message(**kwargs: Any) -> Any:
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            raise TelegramRetryAfter(method=None, message="slow", retry_after=3)  # type: ignore[arg-type]
        calls.append((kwargs["chat_id"], kwargs["text"]))
        return None

    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    monkeypatch.setattr(adapter._bot, "send_message", flaky_send_message)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    await adapter.send_text(42, "hello")
    assert calls == [(42, "hello")]
    # One retry-after sleep of `retry_after + 1` = 4 seconds.
    assert sleeps == [4]


async def test_send_text_exhausts_retries_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = TelegramAdapter(_settings(tmp_path))

    async def always_retry(**kwargs: Any) -> Any:
        del kwargs
        raise TelegramRetryAfter(method=None, message="slow", retry_after=1)  # type: ignore[arg-type]

    async def fake_sleep(duration: float) -> None:
        del duration

    monkeypatch.setattr(adapter._bot, "send_message", always_retry)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    with pytest.raises(TelegramRetryAfter):
        await adapter.send_text(42, "hello")

    # MAX_ATTEMPTS = 2 → 3 total tries (initial + 2 retries) before give-up.
    assert TELEGRAM_RETRY_AFTER_MAX_ATTEMPTS == 2


async def test_send_text_splits_and_retries_per_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long messages split into chunks; each chunk owns its retry budget."""
    adapter = TelegramAdapter(_settings(tmp_path))
    # Two chunks: the first RetryAfter (retries once), second clean.
    call_log: list[str] = []
    first_chunk_failed_once = {"done": False}

    async def flaky(**kwargs: Any) -> Any:
        text = kwargs["text"]
        if text.startswith("A") and not first_chunk_failed_once["done"]:
            first_chunk_failed_once["done"] = True
            raise TelegramRetryAfter(method=None, message="slow", retry_after=1)  # type: ignore[arg-type]
        call_log.append(text[:1])
        return None

    async def fake_sleep(duration: float) -> None:
        del duration

    monkeypatch.setattr(adapter._bot, "send_message", flaky)
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", fake_sleep)

    # Build a 2-chunk message: chunk 1 starts "A…", chunk 2 starts "B…".
    # split_for_telegram cuts on paragraph / newline; put a newline between.
    part_a = "A" + ("x" * 4000)
    part_b = "B" + ("y" * 2000)
    body = part_a + "\n\n" + part_b
    await adapter.send_text(42, body)

    # Two logical chunks delivered.
    assert call_log == ["A", "B"]
