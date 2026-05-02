"""Phase 9 fix-pack F4 (CR-2) — pandoc subprocess timeout path drains
stdout/stderr pipes via ``proc.communicate()`` (not bare
``proc.wait()``).

If pandoc fills the OS pipe buffer (~64 KiB stderr) and we time out,
``proc.wait()`` would block waiting for kernel SIGTERM delivery —
which the kernel can't dispatch until the pipe drains, which we
stopped doing. Replacing ``wait()`` with ``communicate()`` (drains
both pipes concurrently while waiting) closes the deadlock.
"""

from __future__ import annotations

import asyncio

import pytest

from assistant.config import RenderDocSettings
from assistant.render_doc import _subprocess as subp


class _FakeProc:
    """Mock asyncio subprocess: communicate() initially never resolves
    (simulating pandoc with full stderr pipe), then on a second call
    it returns immediately (post-terminate drain)."""

    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.communicate_calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            # First call simulates the long-running pandoc that ignores
            # signals + has full pipes — sleep until cancelled.
            await asyncio.sleep(60)
            return (b"", b"")  # pragma: no cover
        # Subsequent calls return whatever stderr was buffered, fast.
        # Mimics post-terminate drain finishing within grace.
        self.returncode = -15  # SIGTERM
        return (b"", b"oversized stderr " * 8000)

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    async def wait(self) -> int:  # pragma: no cover - not called by F4
        await asyncio.sleep(60)
        return -9


@pytest.mark.asyncio
async def test_terminate_path_uses_communicate_to_drain_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On TimeoutError, the post-terminate phase MUST call
    ``proc.communicate()`` (drains pipes), not ``proc.wait()``."""
    fake = _FakeProc()

    async def _fake_create(*args: object, **kwargs: object) -> _FakeProc:
        return fake

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_create,
    )

    settings = RenderDocSettings(
        pandoc_sigterm_grace_s=0.5,
        pandoc_sigkill_grace_s=0.5,
    )

    with pytest.raises(asyncio.TimeoutError):
        await subp.run_pandoc(
            ["pandoc", "-v"],
            timeout_s=0.05,
            settings=settings,
        )

    assert fake.terminate_called is True
    # Two communicate calls: 1st (initial run) was cancelled by
    # wait_for; 2nd was the post-terminate grace drain.
    assert fake.communicate_calls >= 2, (
        f"expected post-terminate communicate() drain; "
        f"got calls={fake.communicate_calls}"
    )


class _FakeProcWithLargeStderr:
    """Mimics OOM-killed pandoc — emits returncode=-9 + spam stderr."""

    def __init__(self) -> None:
        self.pid = 999
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self._calls = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        self._calls += 1
        if self._calls == 1:
            # Long-running first call.
            await asyncio.sleep(60)
            return (b"", b"")  # pragma: no cover
        self.returncode = -9
        return (b"", b"")

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    async def wait(self) -> int:  # pragma: no cover
        return -9


@pytest.mark.asyncio
async def test_signal_killed_returncode_not_coerced_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``proc.returncode == -9`` (OOM/SIGKILL) MUST surface as -9, not 0.

    The pre-fix-pack ``return (proc.returncode or 0, ...)`` masked
    every signal-killed exit as a clean rc=0 — the renderer would
    then read a non-existent staging output and surface a confusing
    ``pandoc-no-output`` instead of the real ``pandoc-exit-(-9)``.
    """
    # We exercise the success path with rc=-9 via a fake whose first
    # communicate() resolves with a negative returncode.
    class _SignalledProc:
        pid = 1
        returncode: int | None = -9
        terminate_called = False
        kill_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

        def terminate(self) -> None:
            self.terminate_called = True

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:  # pragma: no cover
            return -9

    fake = _SignalledProc()

    async def _fake_create(*args: object, **kwargs: object) -> _SignalledProc:
        return fake

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create
    )

    settings = RenderDocSettings()
    rc, _, _ = await subp.run_pandoc(
        ["pandoc", "-v"],
        timeout_s=10,
        settings=settings,
    )
    assert rc == -9, "negative rc must NOT be coerced to 0"
