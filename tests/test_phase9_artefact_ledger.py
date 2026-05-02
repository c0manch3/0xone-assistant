"""Phase 9 §2.13 + W2-HIGH-2 — artefact ledger + sweeper concurrency.

Covers:
  - register_artefact + mark_delivered set/clear ``in_flight``.
  - sweeper SKIPS in-flight records regardless of TTL (CRIT-3).
  - sweeper REAPS delivered records past TTL.
  - register / mark_delivered / sweep_iteration acquire the lock —
    parallel mutations + sweep do NOT raise
    ``RuntimeError: dictionary changed size during iteration``
    (W2-HIGH-2).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc.subsystem import RenderDocSubsystem


def _make_subsystem(tmp_path: Path) -> RenderDocSubsystem:
    return RenderDocSubsystem(
        artefact_dir=tmp_path / "artefacts",
        settings=RenderDocSettings(artefact_ttl_s=1, sweep_interval_s=1),
        adapter=None,
        owner_chat_id=42,
        run_dir=tmp_path / "run",
        pending_set=set(),
    )


@pytest.mark.asyncio
async def test_register_then_mark_delivered_flips_in_flight(
    tmp_path: Path,
) -> None:
    sub = _make_subsystem(tmp_path)
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    await sub.register_artefact(p, fmt="pdf", suggested_filename="out.pdf")
    assert sub._artefacts[p].in_flight is True
    await sub.mark_delivered(p)
    rec = sub._artefacts[p]
    assert rec.in_flight is False
    assert rec.delivered_at is not None


@pytest.mark.asyncio
async def test_sweeper_skips_in_flight(tmp_path: Path) -> None:
    """CRIT-3: sweeper MUST NOT delete in_flight=True records, even
    when TTL has elapsed by mtime."""
    sub = _make_subsystem(tmp_path)
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    await sub.register_artefact(p, fmt="pdf", suggested_filename="out.pdf")
    # Force the record to look "old" but still in_flight.
    rec = sub._artefacts[p]
    rec.created_at = time.monotonic() - 1000
    # Run one sweep pass — file MUST survive.
    await sub._sweep_iteration()
    assert p.exists(), "in_flight artefact deleted by sweeper"
    assert p in sub._artefacts


@pytest.mark.asyncio
async def test_sweeper_reaps_delivered_past_ttl(tmp_path: Path) -> None:
    sub = _make_subsystem(tmp_path)
    p = tmp_path / "out.pdf"
    p.write_bytes(b"%PDF-1.4\n")
    await sub.register_artefact(p, fmt="pdf", suggested_filename="out.pdf")
    await sub.mark_delivered(p)
    # Backdate the delivery so TTL (1s) has elapsed.
    sub._artefacts[p].delivered_at = time.monotonic() - 100
    await sub._sweep_iteration()
    assert not p.exists(), "delivered artefact past TTL not unlinked"
    assert p not in sub._artefacts


@pytest.mark.asyncio
async def test_concurrent_mutations_no_dict_size_error(
    tmp_path: Path,
) -> None:
    """W2-HIGH-2: parallel register + mark_delivered + sweep ticks MUST
    NOT raise RuntimeError: dictionary changed size during iteration.

    Spawn 10 register coroutines + 10 mark_delivered coroutines + a
    sweep tick concurrently; gather them all; the test passes iff
    no exception escapes.
    """
    sub = _make_subsystem(tmp_path)
    paths = [tmp_path / f"out-{i}.pdf" for i in range(10)]
    for p in paths:
        p.write_bytes(b"x")

    async def reg(p: Path) -> None:
        await sub.register_artefact(
            p, fmt="pdf", suggested_filename=p.name
        )

    async def mark(p: Path) -> None:
        # Brief yield to overlap with sweep + register.
        await asyncio.sleep(0)
        await sub.mark_delivered(p)

    tasks: list[Any] = []
    for p in paths:
        tasks.append(asyncio.create_task(reg(p)))
    for p in paths:
        tasks.append(asyncio.create_task(mark(p)))
    tasks.append(asyncio.create_task(sub._sweep_iteration()))
    # All tasks must complete without raising.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        assert not isinstance(r, BaseException), f"exception escaped: {r!r}"


@pytest.mark.asyncio
async def test_get_inflight_count_returns_dict_size(tmp_path: Path) -> None:
    """W2-LOW-1 / AC#29: RSS observer reads this for the
    ``render_doc_inflight=N`` field."""
    sub = _make_subsystem(tmp_path)
    assert sub.get_inflight_count() == 0
    p = tmp_path / "x.pdf"
    p.write_bytes(b"x")
    await sub.register_artefact(p, fmt="pdf", suggested_filename="x.pdf")
    assert sub.get_inflight_count() == 1
