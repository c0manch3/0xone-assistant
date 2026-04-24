"""Shared pytest fixtures for the 0xone-assistant test suite."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_installer_ctx() -> Iterator[None]:
    """Reset the module-level ``configure_installer`` one-shot guard
    between tests (S11 wave-3).

    ``configure_installer`` became idempotent + strict to block silent
    re-configuration with different ``(project_root, data_dir)`` pairs.
    Each test uses its own ``tmp_path`` so without this reset the second
    test to call ``configure_installer`` would raise ``RuntimeError``.
    """
    from assistant.tools_sdk.installer import reset_installer_for_tests

    reset_installer_for_tests()
    yield
    reset_installer_for_tests()


@pytest.fixture(autouse=True)
def _reset_memory_ctx() -> Iterator[None]:
    """Reset the memory module state so each test starts clean.

    Mirrors the installer's autouse fixture — every test that builds a
    tmp-path vault needs ``configure_memory`` to accept the new paths.
    """
    from assistant.tools_sdk.memory import reset_memory_for_tests

    reset_memory_for_tests()
    yield
    reset_memory_for_tests()


@pytest.fixture(autouse=True)
def _reset_scheduler_ctx() -> Iterator[None]:
    """Reset the scheduler module state so each test starts clean.

    Parallel to ``_reset_memory_ctx`` — every scheduler-tool test
    configures a fresh store against ``tmp_path``.
    """
    from assistant.tools_sdk.scheduler import reset_scheduler_for_tests

    reset_scheduler_for_tests()
    yield
    reset_scheduler_for_tests()


class FakeClock:
    """Deterministic clock for async tick-loop tests (RQ4).

    ``now()`` returns the current virtual time; ``sleep(s)`` advances
    the virtual clock by ``s`` seconds AND yields to the event loop
    so pending coroutines run. The ``slept`` list records every
    ``sleep`` call — useful for asserting cadence in tests.

    Default start is ``2026-01-01 00:00 UTC`` — well-defined, not a
    DST transition, not a leap day, easy to reason about.
    """

    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.slept: list[float] = []

    def now(self) -> dt.datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += dt.timedelta(seconds=seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self._now += dt.timedelta(seconds=seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def memory_ctx(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Configure the memory subsystem against a tmp-path vault.

    Guards against accidentally touching the owner's real vault at
    ``~/.local/share/0xone-assistant/vault`` (M2.5).
    """
    from assistant.tools_sdk import memory as mm

    mm.reset_memory_for_tests()
    vault = tmp_path / "vault"
    idx = tmp_path / "memory-index.db"
    # Safety rail — never let a test vault collide with the owner's
    # real one.
    assert not str(tmp_path).startswith(
        os.path.expanduser("~/.local/share/0xone-assistant")
    ), f"tmp_path {tmp_path} must not overlap the real data dir"
    mm.configure_memory(
        vault_dir=vault, index_db_path=idx, max_body_bytes=1_048_576
    )
    yield vault, idx
    mm.reset_memory_for_tests()


@pytest.fixture
def seed_vault_copy(tmp_path: Path) -> Path:
    """Copy the owner's real 12-note seed vault into ``tmp_path``.

    Uses a distinct subdirectory (``seed_vault``) rather than ``vault``
    so co-existing ``memory_ctx`` fixtures that also build ``vault``
    don't collide.
    """
    src = Path.home() / ".local" / "share" / "0xone-assistant" / "vault"
    if not src.exists():
        pytest.skip(f"seed vault missing at {src}")
    dst = tmp_path / "seed_vault"
    shutil.copytree(src, dst)
    return dst
