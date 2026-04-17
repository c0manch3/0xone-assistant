"""Spike S-2: TelegramAdapter.send_text from a background asyncio task.

Question: Plan §5.4 has SchedulerDispatcher calling
`adapter.send_text(OWNER_CHAT_ID, joined)` from a task that is NOT the
polling loop. With aiogram 3 Bot.send_message, is it safe to call
send_message concurrently from a second coroutine on the same event loop?

Pass criterion: 100/100 calls complete, no exception, no deadlock.

Method: stub aiogram Bot.send_message via monkeypatch on an actual
TelegramAdapter instance. Spawn a "polling loop" coroutine + a
"dispatcher" coroutine that both hit send_text in parallel. Count calls,
note any exception.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))


async def run() -> dict:
    # Local imports after sys.path mangling.
    from assistant.adapters.telegram import TelegramAdapter
    from assistant.config import Settings

    # Construct a Settings with the bare minimum. telegram_bot_token and
    # owner_chat_id are required. Use a clearly-fake token; we monkeypatch
    # Bot.send_message so nothing leaves the process.
    settings = Settings(  # type: ignore[call-arg]
        telegram_bot_token="0:FAKE_FOR_SPIKE",  # nosec
        owner_chat_id=1,
    )
    adapter = TelegramAdapter(settings)

    call_log: list[tuple[int, float, str]] = []
    send_lock_contention: list[float] = []

    async def fake_send_message(*, chat_id: int, text: str) -> None:
        # Simulate a tiny round-trip; note when called.
        t0 = time.perf_counter()
        call_log.append((chat_id, t0, text[:40]))
        await asyncio.sleep(0.005)
        send_lock_contention.append((time.perf_counter() - t0) * 1000.0)

    # Monkeypatch the send_message on the bound bot instance.
    adapter._bot.send_message = fake_send_message  # type: ignore[assignment,method-assign]

    async def polling_like() -> None:
        for i in range(50):
            await adapter.send_text(1, f"poll-{i}")

    async def dispatcher_like() -> None:
        for i in range(50):
            await adapter.send_text(1, f"sched-{i}")

    exceptions: list[str] = []

    async def guarded(coro):
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            exceptions.append(repr(exc))

    t0 = time.perf_counter()
    await asyncio.gather(
        guarded(polling_like()),
        guarded(dispatcher_like()),
    )
    wall_s = time.perf_counter() - t0

    # Tear down the bot session cleanly.
    try:
        await adapter._bot.session.close()
    except Exception as exc:  # noqa: BLE001
        exceptions.append(f"session_close: {exc!r}")

    return {
        "calls_observed": len(call_log),
        "exceptions": exceptions,
        "wall_seconds": wall_s,
        "first_5_calls": call_log[:5],
        "last_5_calls": call_log[-5:],
        "pass": len(call_log) == 100 and not exceptions,
    }


def main() -> None:
    result = asyncio.run(run())
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
