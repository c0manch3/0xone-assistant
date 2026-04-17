"""Spike S-5: asyncio.Queue(maxsize=3) backpressure + poison-pill shutdown.

Question: Plan §5.2 has SchedulerLoop (producer) await queue.put() and
SchedulerDispatcher (consumer) await queue.get(). When the queue fills,
producer must block cleanly (not busy-loop). And on shutdown, can we
wake a blocked producer via put_nowait(POISON) — or does that raise
QueueFull?

Pass criterion:
  * blocking on 4th put is observed (producer task state = waiting);
  * queue.put_nowait(POISON) from a third coroutine raises QueueFull
    when the queue is already maxed out (answer shapes shutdown design);
  * using queue.put(POISON) instead wakes cleanly after a get().
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

POISON: Any = object()


async def run() -> dict:
    results: dict = {}

    # --- Case A: producer blocks on 4th put (queue maxsize=3).
    q: asyncio.Queue = asyncio.Queue(maxsize=3)

    async def producer() -> list[str]:
        log = []
        for i in range(5):
            log.append(f"put-start-{i}")
            await q.put(i)
            log.append(f"put-done-{i}")
        return log

    producer_task = asyncio.create_task(producer())
    # Give producer time to fill queue and block.
    await asyncio.sleep(0.05)
    results["caseA_queue_full_after_3_puts"] = q.full()
    results["caseA_producer_blocked"] = not producer_task.done()

    # Drain one item; producer should unblock.
    _ = await q.get()
    await asyncio.sleep(0.01)
    results["caseA_after_one_get_q_full"] = q.full()  # still true (4 put'd)
    results["caseA_producer_progress"] = not producer_task.done()

    # Drain the rest.
    while not producer_task.done():
        try:
            await asyncio.wait_for(q.get(), timeout=0.1)
        except TimeoutError:
            break
    results["caseA_producer_finished"] = producer_task.done()

    # --- Case B: put_nowait(POISON) when queue is already full -> QueueFull.
    q2: asyncio.Queue = asyncio.Queue(maxsize=3)
    for i in range(3):
        q2.put_nowait(i)

    caseB_error: str | None = None
    try:
        q2.put_nowait(POISON)
    except asyncio.QueueFull as exc:
        caseB_error = f"{type(exc).__name__}"
    results["caseB_put_nowait_poison_on_full"] = caseB_error

    # --- Case C: use q.put(POISON) via a concurrent task; after one get(),
    # the poison lands and is the next get().
    q3: asyncio.Queue = asyncio.Queue(maxsize=3)
    for i in range(3):
        q3.put_nowait(i)

    async def shutdown_poison():
        await q3.put(POISON)

    poison_task = asyncio.create_task(shutdown_poison())
    await asyncio.sleep(0.01)
    results["caseC_poison_task_blocked_on_full_q"] = not poison_task.done()
    # Consumer drains.
    drained = []
    drained.append(await q3.get())
    drained.append(await q3.get())
    drained.append(await q3.get())
    poisoned_item = await q3.get()
    await poison_task
    results["caseC_drain_order"] = [str(x) if x is not POISON else "POISON" for x in drained]
    results["caseC_final_item_is_poison"] = poisoned_item is POISON

    # --- Case D: stop_event pattern. Producer awaits put; consumer that
    # respects stop_event drains remaining items and exits. Confirms the
    # alternative design (event + bounded get timeout).
    q4: asyncio.Queue = asyncio.Queue(maxsize=3)
    stop_event = asyncio.Event()

    async def consumer_with_event() -> int:
        count = 0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(q4.get(), timeout=0.05)
                count += 1
            except TimeoutError:
                continue
        return count

    c_task = asyncio.create_task(consumer_with_event())
    for i in range(5):
        await q4.put(i)
    await asyncio.sleep(0.2)
    stop_event.set()
    try:
        got = await asyncio.wait_for(c_task, timeout=1.0)
    except TimeoutError:
        got = -1
    results["caseD_stop_event_consumer_consumed"] = got
    results["caseD_stop_event_clean_exit"] = c_task.done()

    results["pass"] = (
        results["caseA_queue_full_after_3_puts"] is True
        and results["caseA_producer_blocked"] is True
        and results["caseA_producer_finished"] is True
        and results["caseB_put_nowait_poison_on_full"] == "QueueFull"
        and results["caseC_final_item_is_poison"] is True
        and results["caseD_stop_event_clean_exit"] is True
    )
    return results


def main() -> None:
    r = asyncio.run(run())
    print(json.dumps(r, indent=2, default=str))


if __name__ == "__main__":
    main()
