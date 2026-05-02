"""Phase 9 fix-pack F11 (QH-1+3+4 / W3-MED-3/4/5) — lifecycle tests
for the safety-critical ACs that previously had ZERO behavioural
coverage.

Covers:
  - **AC#11** concurrency cap: ``render_max_concurrent=2`` + 3
    parallel ``subsystem.render`` calls — third must wait.
  - **AC#12** per-tool timeout: @tool body wraps render() in
    ``asyncio.wait_for(timeout=tool_timeout_s)``; on TimeoutError the
    envelope carries ``reason='timeout', error='tool-timeout-exceeded'``.
  - **AC#21** Daemon.stop SIGTERM-to-pandoc: a hung pandoc subprocess
    receives SIGTERM at 5s then SIGKILL at 10s.
  - **AC#21a** cumulative drain budget under 80s for the orchestrated
    sub-stages.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from assistant.config import RenderDocSettings, Settings
from assistant.main import Daemon
from assistant.render_doc import _subprocess as subp
from assistant.render_doc.subsystem import RenderDocSubsystem
from assistant.tools_sdk import render_doc as render_doc_tool


@pytest.mark.asyncio
async def test_ac11_concurrency_cap_third_waits(tmp_path: Path) -> None:
    """AC#11: with ``render_max_concurrent=2``, three parallel renders
    must serialise — the third blocks on the semaphore until one of
    the first two completes."""
    artefact_dir = tmp_path / "artefacts"
    artefact_dir.mkdir(parents=True, exist_ok=True)
    (artefact_dir / ".staging").mkdir(parents=True, exist_ok=True)
    settings = RenderDocSettings(
        enabled=True, render_max_concurrent=2
    )
    pending: set[asyncio.Task[object]] = set()
    sub = RenderDocSubsystem(
        artefact_dir=artefact_dir,
        settings=settings,
        adapter=None,
        owner_chat_id=1,
        run_dir=tmp_path / "run",
        pending_set=pending,
    )

    in_flight = 0
    max_in_flight = 0
    gate = asyncio.Event()

    async def _slow_dispatch(**_: object) -> object:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await gate.wait()
        in_flight -= 1
        # Build a minimal RenderResult so render() returns cleanly.
        from assistant.render_doc.subsystem import RenderResult
        # Drop a real file so register_artefact has something.
        path = artefact_dir / f"{int(time.monotonic_ns())}.pdf"
        path.write_bytes(b"%PDF-1\n%%EOF\n")
        await sub.register_artefact(
            path, fmt="pdf", suggested_filename="x.pdf"
        )
        return RenderResult(
            ok=True,
            fmt="pdf",
            suggested_filename="x.pdf",
            path=path,
            bytes_out=10,
            duration_ms=1,
        )

    sub._dispatch = _slow_dispatch  # type: ignore[assignment]

    tasks = [
        asyncio.create_task(
            sub.render(
                "# x", "pdf", filename=f"f{i}", task_handle=None
            )
        )
        for i in range(3)
    ]
    # Let the scheduler establish the first two in-flight.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if in_flight == 2:
            break
    assert max_in_flight == 2, (
        f"semaphore must cap at 2; got max_in_flight={max_in_flight}"
    )
    # Release the gate; all three complete.
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r.ok for r in results)
    # The third was queued — total max_in_flight stays at 2.
    assert max_in_flight == 2


@pytest.mark.asyncio
async def test_ac12_tool_timeout_emits_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC#12: ``asyncio.wait_for(timeout=tool_timeout_s)`` cancels a
    hanging render and emits ``reason='timeout',
    error='tool-timeout-exceeded'``."""
    artefact_dir = tmp_path / "artefacts"
    artefact_dir.mkdir(parents=True, exist_ok=True)
    (artefact_dir / ".staging").mkdir(parents=True, exist_ok=True)
    # Smallest viable settings for a quick test (sub-second timeout).
    settings = RenderDocSettings(
        enabled=True,
        tool_timeout_s=1,
        pdf_pandoc_timeout_s=1,
        pdf_weasyprint_timeout_s=0,
    )
    pending: set[asyncio.Task[object]] = set()
    sub = RenderDocSubsystem(
        artefact_dir=artefact_dir,
        settings=settings,
        adapter=None,
        owner_chat_id=1,
        run_dir=tmp_path / "run",
        pending_set=pending,
    )
    render_doc_tool.configure_render_doc(subsystem=sub)
    monkeypatch.setattr(
        sub,
        "_dispatch",
        lambda **_kw: asyncio.sleep(60),
    )

    # ``render_doc`` is wrapped by the SDK ``@tool`` decorator;
    # invoke ``.handler`` for the underlying coroutine.
    result = await render_doc_tool.render_doc.handler(
        {"content_md": "# x", "format": "pdf", "filename": "tt"}
    )
    payload_text = result["content"][0]["text"]
    assert '"reason":"timeout"' in payload_text or (
        result.get("reason") == "timeout"
    )
    assert "tool-timeout-exceeded" in payload_text or (
        result.get("error") == "tool-timeout-exceeded"
    )
    # Cleanup so subsequent tests can configure a fresh subsystem.
    render_doc_tool.reset_render_doc_for_tests()


@pytest.mark.asyncio
async def test_ac21_pandoc_sigterm_then_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC#21: a hung pandoc subprocess receives SIGTERM, then SIGKILL
    after the configured grace if SIGTERM doesn't take effect."""

    class _ZombieProc:
        pid = 4242
        returncode: int | None = None
        terminate_called = False
        kill_called = False
        comm_calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self.comm_calls += 1
            # Always blocks — simulates a process ignoring signals.
            await asyncio.sleep(60)
            return (b"", b"")  # pragma: no cover

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:  # pragma: no cover
            return -9

    fake = _ZombieProc()

    async def _fake_create(*args: object, **kwargs: object) -> _ZombieProc:
        return fake

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create
    )

    settings = RenderDocSettings(
        pandoc_sigterm_grace_s=0.1,
        pandoc_sigkill_grace_s=0.1,
    )
    with pytest.raises(asyncio.TimeoutError):
        await subp.run_pandoc(
            ["pandoc", "-v"],
            timeout_s=0.05,
            settings=settings,
        )
    assert fake.terminate_called is True
    assert fake.kill_called is True


@pytest.mark.asyncio
async def test_ac21a_cumulative_drain_budget_under_80s(
    tmp_path: Path,
) -> None:
    """AC#21a: the orchestrated drain stages (render + adapter + vault
    + bg cancel + audio + subagent) MUST complete within an 80s budget
    when each stage drains within its own short window.

    We don't simulate full real timings — we wire each stage's
    internal sleep to a small value and assert the wall-clock for
    Daemon.stop() stays well under the 116-130s admitted worst case.
    """
    settings = Settings(
        telegram_bot_token="x" * 16, owner_chat_id=1
    )
    settings.render_doc.render_drain_timeout_s = 0.1
    settings.vault_sync.drain_timeout_s = 0.1
    settings.audio_bg.drain_timeout_s = 0.1
    settings.subagent.drain_timeout_s = 0.1

    class _Adapter:
        async def start(self) -> None: ...

        async def stop(self) -> None:
            await asyncio.sleep(0.05)

        async def send_text(self, *_: object, **__: object) -> None: ...

    class _Sub:
        force_disabled = False

        async def mark_orphans_delivered_at_shutdown(self) -> None: ...

        def get_inflight_count(self) -> int:
            return 0

    d = Daemon.__new__(Daemon)
    d._settings = settings
    d._adapter = _Adapter()  # type: ignore[assignment]
    d._sched_loop = None
    d._sched_dispatcher = None
    d._sub_picker = None
    d._render_doc_pending = set()
    d._render_doc = _Sub()  # type: ignore[assignment]
    d._vault_sync_pending = set()
    d._audio_persist_pending = set()
    d._sub_pending_updates = set()
    d._bg_tasks = set()
    d._conn = None  # type: ignore[assignment]
    d._lock_fd = None

    start = time.monotonic()
    await d.stop()
    elapsed = time.monotonic() - start
    assert elapsed < 80.0, (
        f"cumulative drain MUST be <80s; got {elapsed:.2f}s"
    )
