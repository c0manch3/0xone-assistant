"""Phase 6e — audio bridge / user bridge isolation (Alt-C).

When the audio bridge's semaphore is fully booked (audio_max_concurrent=1
→ one in-flight voice job holds the slot for the duration of the bg
task), the user-text bridge MUST remain free to serve owner /ping
without queueing on the same lock.

Two orthogonal checks:

- ``test_audio_sem_full_does_not_block_user_bridge`` — directly proves
  the semaphores are independent: hold the audio bridge's slot, then
  acquire+release the user bridge's. No deadlock or ordering churn.
- ``test_user_and_audio_bridges_have_distinct_semaphores`` — pins the
  invariant that ``ClaudeBridge`` instances do NOT share a class-level
  semaphore, so a future refactor that "moves the semaphore to the
  module" tickles this guard.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def _build_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456:" + "x" * 30,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(
            timeout=30,
            max_concurrent=2,        # user/picker
            audio_max_concurrent=1,  # audio bridge
            history_limit=5,
        ),
    )


async def test_audio_sem_full_does_not_block_user_bridge(
    tmp_path: Path,
) -> None:
    """Hold audio_bridge sem; user_bridge must still acquire its own."""
    settings = _build_settings(tmp_path)
    user_bridge = ClaudeBridge(settings)
    audio_bridge = ClaudeBridge(
        settings,
        max_concurrent_override=settings.claude.audio_max_concurrent,
    )

    # audio_bridge has Semaphore(1): the override drives it.
    assert audio_bridge._sem._value == 1  # type: ignore[attr-defined]
    # user_bridge keeps Semaphore(2) from settings.claude.max_concurrent.
    assert user_bridge._sem._value == 2  # type: ignore[attr-defined]

    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_audio_slot() -> None:
        async with audio_bridge._sem:  # type: ignore[attr-defined]
            held.set()
            await release.wait()

    audio_holder = asyncio.create_task(hold_audio_slot())
    try:
        await asyncio.wait_for(held.wait(), timeout=1.0)

        # While the audio sem is fully held, user_bridge.sem still
        # has 2/2 free — acquire+release MUST complete instantly.
        # ``asyncio.wait_for(..., 0.5)`` is generous; on a healthy
        # event loop this is sub-millisecond.
        async def quick_user() -> None:
            async with user_bridge._sem:  # type: ignore[attr-defined]
                pass

        await asyncio.wait_for(quick_user(), timeout=0.5)
    finally:
        release.set()
        await audio_holder


async def test_user_and_audio_bridges_have_distinct_semaphores(
    tmp_path: Path,
) -> None:
    """The override path constructs a NEW Semaphore — not a shared one."""
    settings = _build_settings(tmp_path)
    user_bridge = ClaudeBridge(settings)
    picker_bridge = ClaudeBridge(settings)
    audio_bridge = ClaudeBridge(
        settings,
        max_concurrent_override=settings.claude.audio_max_concurrent,
    )

    # Semaphore objects must be three distinct instances; this
    # invariant is what makes the bg audio path independent of the
    # user-text + picker paths.
    seen = {id(user_bridge._sem), id(picker_bridge._sem), id(audio_bridge._sem)}  # type: ignore[attr-defined]
    assert len(seen) == 3

    # Picker keeps the user-text default, NOT the audio override.
    assert picker_bridge._sem._value == 2  # type: ignore[attr-defined]


async def test_two_concurrent_audio_jobs_serialise_on_audio_sem(
    tmp_path: Path,
) -> None:
    """With audio_max_concurrent=1, a second voice job blocks until
    the first finishes — confirms the FIFO contract that mirrors the
    Mac whisper-server's hard ``Semaphore(1)``.
    """
    settings = _build_settings(tmp_path)
    audio_bridge = ClaudeBridge(
        settings,
        max_concurrent_override=settings.claude.audio_max_concurrent,
    )
    sem = audio_bridge._sem  # type: ignore[attr-defined]
    assert sem._value == 1  # type: ignore[attr-defined]

    enter1 = asyncio.Event()
    can_release1 = asyncio.Event()
    enter2 = asyncio.Event()

    async def job1() -> None:
        async with sem:
            enter1.set()
            await can_release1.wait()

    async def job2() -> None:
        async with sem:
            enter2.set()

    t1 = asyncio.create_task(job1())
    await asyncio.wait_for(enter1.wait(), timeout=1.0)

    t2 = asyncio.create_task(job2())
    # job2 must NOT enter while job1 holds the only slot.
    await asyncio.sleep(0.05)
    assert not enter2.is_set()

    can_release1.set()
    await asyncio.wait_for(enter2.wait(), timeout=1.0)
    await asyncio.gather(t1, t2)
