"""Phase 8 fix-pack F7 — AC#25 RSS observer ``vault_sync_pending`` field.

The phase-6e ``Daemon._rss_observer`` emits a structured ``daemon_rss``
event every ``rss_interval_s`` seconds correlating RSS with the
in-flight bg / persist / subagent counts. Phase 8 (W2-M4) extended
the payload with ``vault_sync_pending`` so a stuck push surfaces in
the RSS sample stream.

AC#25 invariants:

  - ``settings.vault_sync.enabled=False`` (default) → field is OMITTED
    from the log payload (because ``Daemon._vault_sync`` is None).
    AC#5 parity.
  - ``settings.vault_sync.enabled=True`` → field is present and equal
    to ``len(Daemon._vault_sync_pending)``.

We stub ``/proc/self/status`` reading and the underlying log binding
to capture the payload directly, so the test runs on macOS dev hosts
too.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest


class _FakeLogger:
    """Captures structlog-style ``info`` calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def debug(self, *_a: Any, **_kw: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_ac25_field_omitted_when_subsystem_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``Daemon._vault_sync is None`` (default — vault_sync
    disabled), the ``daemon_rss`` log line MUST NOT include
    ``vault_sync_pending`` (AC#5 parity)."""
    from assistant import main as main_mod
    from assistant.config import Settings

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )
    daemon = main_mod.Daemon(settings)
    # Vault sync subsystem is None (default).
    assert daemon._vault_sync is None
    # Stub the structlog logger inside _rss_observer to a fake.
    fake_log = _FakeLogger()
    monkeypatch.setattr(
        main_mod, "get_logger", lambda _name: fake_log
    )

    # Stub /proc/self/status read.
    proc_text = "VmRSS:    102400 kB\n"

    class _FakeFile:
        def __init__(self, text: str) -> None:
            self._lines = text.splitlines(keepends=True)

        def __iter__(self) -> Any:
            return iter(self._lines)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def _fake_open(_path: str, *_a: Any, **_kw: Any) -> _FakeFile:
        return _FakeFile(proc_text)

    monkeypatch.setattr("builtins.open", _fake_open)

    # Run a single observer iteration: spawn the coroutine, wait
    # briefly, then cancel.
    obs_task = asyncio.create_task(
        daemon._rss_observer(interval_s=10.0)
    )
    await asyncio.sleep(0.05)
    obs_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await obs_task

    # At least one daemon_rss event captured.
    rss_events = [e for e in fake_log.events if e[0] == "daemon_rss"]
    assert rss_events
    payload = rss_events[0][1]
    assert "vault_sync_pending" not in payload
    # The other fields ARE present.
    assert "rss_mb" in payload
    assert "bg_tasks" in payload


@pytest.mark.asyncio
async def test_ac25_field_present_when_subsystem_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the subsystem is constructed, ``vault_sync_pending`` is
    present and equals ``len(_vault_sync_pending)``."""
    from assistant import main as main_mod
    from assistant.config import Settings, VaultSyncSettings
    from assistant.vault_sync.subsystem import VaultSyncSubsystem

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        vault_sync=VaultSyncSettings(
            enabled=True,
            repo_url="git@github.com:c0manch3/0xone-vault.git",
            manual_tool_enabled=True,
        ),
    )
    daemon = main_mod.Daemon(settings)
    # Construct the subsystem so the observer's ``is not None`` check
    # passes.
    run_dir = tmp_path / "data" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    vault = tmp_path / "data" / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    daemon._vault_sync = VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings.vault_sync,
        adapter=None,
        owner_chat_id=42,
        run_dir=run_dir,
        pending_set=daemon._vault_sync_pending,
    )
    # Inject 2 fake pending tasks.

    async def _stub() -> None:
        await asyncio.sleep(60.0)

    t1 = asyncio.create_task(_stub())
    t2 = asyncio.create_task(_stub())
    daemon._vault_sync_pending.add(t1)
    daemon._vault_sync_pending.add(t2)

    fake_log = _FakeLogger()
    monkeypatch.setattr(
        main_mod, "get_logger", lambda _name: fake_log
    )

    proc_text = "VmRSS:    102400 kB\n"

    class _FakeFile:
        def __init__(self, text: str) -> None:
            self._lines = text.splitlines(keepends=True)

        def __iter__(self) -> Any:
            return iter(self._lines)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    def _fake_open(_path: str, *_a: Any, **_kw: Any) -> _FakeFile:
        return _FakeFile(proc_text)

    monkeypatch.setattr("builtins.open", _fake_open)

    obs_task = asyncio.create_task(
        daemon._rss_observer(interval_s=10.0)
    )
    await asyncio.sleep(0.05)
    obs_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await obs_task

    rss_events = [e for e in fake_log.events if e[0] == "daemon_rss"]
    assert rss_events
    payload = rss_events[0][1]
    assert payload.get("vault_sync_pending") == 2

    # Cleanup background tasks.
    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
