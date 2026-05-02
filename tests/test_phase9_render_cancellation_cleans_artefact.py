"""Phase 9 fix-pack F5 (CR-3) — timeout-cancel mid-``register_artefact``
must NOT leak ledger row + on-disk file.

Pre-fix-pack: ``asyncio.wait_for(timeout=tool_timeout_s)`` could
cancel the inner task at the ``register_artefact`` await point or
just after; the file lived on disk with ``in_flight=True`` until
daemon restart because the TTL sweeper skips ``in_flight=True``
records. Fix wraps dispatch+register in a ``CancelledError`` handler
that pops the ledger row + unlinks the file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc.subsystem import RenderDocSubsystem


@pytest.mark.asyncio
async def test_cancellation_after_register_cleans_ledger_and_disk(
    tmp_path: Path,
) -> None:
    """A cancel during the render's post-register settle pops the row
    + unlinks the on-disk artefact — no leak."""
    artefact_dir = tmp_path / "artefacts"
    artefact_dir.mkdir(parents=True, exist_ok=True)
    (artefact_dir / ".staging").mkdir(parents=True, exist_ok=True)

    settings = RenderDocSettings(enabled=True)
    pending: set[asyncio.Task[object]] = set()
    sub = RenderDocSubsystem(
        artefact_dir=artefact_dir,
        settings=settings,
        adapter=None,
        owner_chat_id=1,
        run_dir=tmp_path / "run",
        pending_set=pending,
    )

    # Manually pre-populate a fake artefact + ledger row to simulate
    # the state right after ``register_artefact`` ran but BEFORE
    # the @tool body returned. Then trigger render() which will
    # observe a no-op dispatch + a cancel mid-flight.
    leaked_path = artefact_dir / "leak.pdf"
    leaked_path.write_bytes(b"%PDF-x")
    suggested = "leak.pdf"
    await sub.register_artefact(
        leaked_path, fmt="pdf", suggested_filename=suggested
    )
    assert leaked_path.exists()
    assert leaked_path in sub._artefacts

    # Force the render() body to raise CancelledError mid-dispatch.
    async def _cancel_dispatch(**_: object) -> object:
        raise asyncio.CancelledError("simulated timeout cancel")

    sub._dispatch = _cancel_dispatch  # type: ignore[assignment]

    with pytest.raises(asyncio.CancelledError):
        await sub.render(
            "# x", "pdf", filename="leak", task_handle=None
        )

    # F5: the leaked row + on-disk file MUST have been swept by the
    # CancelledError handler that wraps dispatch+register.
    assert leaked_path not in sub._artefacts, (
        "leaked ledger row not cleaned up"
    )
    assert not leaked_path.exists(), "leaked artefact file not unlinked"
