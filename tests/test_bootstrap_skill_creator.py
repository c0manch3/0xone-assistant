"""Fire-and-forget bootstrap invariants.

The most important assertion: `Daemon.start()` returns promptly
regardless of how the mocked bootstrap subprocess behaves. We prove it by
making the subprocess "sleep 60 s" and wrapping `start()` in a 500 ms
asyncio.wait_for.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings
from assistant.main import Daemon


class _NeverWaitProc:
    """`.wait()` sleeps 60 s, `.kill()` is a no-op. Used to prove
    Daemon.start() is NOT blocking on this subprocess."""

    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = None
        self.stderr = None

    async def wait(self) -> int:
        await asyncio.sleep(60)
        return 0

    def kill(self) -> None:
        return None


class _QuickProc:
    """Exits rc=0 instantly."""

    def __init__(self, rc: int = 0, stderr: bytes = b"") -> None:
        self.returncode = rc
        self.stdout = None
        self.stderr = _FakeStderr(stderr)

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return self.returncode

    def kill(self) -> None:
        return None


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _DummyAdapter:
    """Stand-in for TelegramAdapter — start()/stop() are instant, and
    `send_text` records calls so we can verify bootstrap-notify behaviour."""

    def __init__(self, settings: Any) -> None:
        del settings
        self.sent: list[tuple[int, str]] = []
        self._handler: Any = None

    def set_handler(self, handler: Any) -> None:
        self._handler = handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "skills").mkdir()
    (tmp_path / ".claude").mkdir()
    # System prompt template is loaded lazily on first turn — not during start.
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=12345,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


@pytest.fixture
def _wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheap wiring — preflight/symlink/adapter all stubbed."""
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)


async def _noop_preflight(log: Any) -> None:
    del log


@pytest.mark.asyncio
async def test_start_does_not_wait_for_bootstrap(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*a: Any, **kw: Any) -> _NeverWaitProc:
        del a, kw
        return _NeverWaitProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(_settings)
    t0 = time.monotonic()
    # Review fix #14: 0.5 s is brittle under CI load — 2.0 s still proves
    # we did not block on the 60-s `.wait()` (a 30x safety margin against
    # the "never hit" mock).
    await asyncio.wait_for(daemon.start(), timeout=2.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    # Background tasks must be alive (held on the daemon instance, not GC'd).
    # Phase-5: `sweep_run_dirs` is fast and may have completed + been
    # removed from `_bg_tasks` by its done-callback before we inspect
    # here; only `skill_creator_bootstrap` (never-ending under _NeverWaitProc)
    # is guaranteed alive at this point.
    names = {t.get_name() for t in daemon._bg_tasks}
    assert "skill_creator_bootstrap" in names
    # Clean up so pytest event loop doesn't bleed tasks into the next test.
    for t in list(daemon._bg_tasks):
        t.cancel()
    await asyncio.gather(*daemon._bg_tasks, return_exceptions=True)
    await daemon.stop()


@pytest.mark.asyncio
async def test_start_skips_bootstrap_when_gh_missing(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[bool] = []

    async def _spawn(*a: Any, **kw: Any) -> _QuickProc:
        spawned.append(True)
        return _QuickProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    daemon = Daemon(_settings)
    await daemon.start()
    # Give background tasks a microtask to run the gh-check path.
    await asyncio.sleep(0.02)
    assert not spawned
    await daemon.stop()


@pytest.mark.asyncio
async def test_start_skips_bootstrap_when_skill_already_present(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[bool] = []

    async def _spawn(*a: Any, **kw: Any) -> _QuickProc:
        spawned.append(True)
        return _QuickProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")
    (_settings.project_root / "skills" / "skill-creator").mkdir()

    daemon = Daemon(_settings)
    await daemon.start()
    await asyncio.sleep(0.02)
    assert not spawned
    await daemon.stop()


@pytest.mark.asyncio
async def test_bootstrap_failure_notifies_owner_once(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _spawn(*a: Any, **kw: Any) -> _QuickProc:
        return _QuickProc(rc=1, stderr=b"oops")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(_settings)
    await daemon.start()
    # Wait for the bootstrap task to complete. Phase-5 scheduler bg-tasks
    # (`scheduler_*`) are infinite loops; filter them out here — they are
    # properly cancelled in `daemon.stop()` below.
    await asyncio.gather(
        *[t for t in daemon._bg_tasks if not (t.get_name() or "").startswith("scheduler_")],
        return_exceptions=True,
    )
    adapter = daemon._adapter
    assert isinstance(adapter, _DummyAdapter)
    assert len(adapter.sent) == 1
    assert "skill-creator" in adapter.sent[0][1]
    # Marker must exist.
    assert (_settings.data_dir / "run" / ".bootstrap_notified").exists()
    await daemon.stop()


@pytest.mark.asyncio
async def test_bootstrap_failure_does_not_renotify(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-seed a valid, current-rc marker; the next failure with the same
    rc must NOT message again (review fix #11 rotation semantics).
    """
    import json as _json
    import time as _time

    (_settings.data_dir / "run").mkdir(parents=True)
    (_settings.data_dir / "run" / ".bootstrap_notified").write_text(
        _json.dumps(
            {
                "rc": 1,
                "reason": "failed",
                "ts_epoch": _time.time(),
                "ts": "2026-04-17T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    async def _spawn(*a: Any, **kw: Any) -> _QuickProc:
        return _QuickProc(rc=1, stderr=b"still broken")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(_settings)
    await daemon.start()
    await asyncio.gather(
        *[t for t in daemon._bg_tasks if not (t.get_name() or "").startswith("scheduler_")],
        return_exceptions=True,
    )
    adapter = daemon._adapter
    assert isinstance(adapter, _DummyAdapter)
    assert adapter.sent == []
    await daemon.stop()


@pytest.mark.asyncio
async def test_bootstrap_happy_path_creates_skill_and_sentinel(
    _settings: Settings, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix #17: after a successful bootstrap the
    `skills/skill-creator/` directory appears and the hot-reload sentinel
    is touched (the installer writes it as its last step on success)."""
    created = {"done": False}

    async def _spawn(*a: Any, **kw: Any) -> _QuickProc:
        # Simulate the installer's side effects: drop a skill dir + sentinel.
        (_settings.project_root / "skills" / "skill-creator").mkdir()
        (_settings.project_root / "skills" / "skill-creator" / "SKILL.md").write_text(
            "---\nname: skill-creator\ndescription: ok\n---\n", encoding="utf-8"
        )
        (_settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
        (_settings.data_dir / "run" / "skills.dirty").touch()
        created["done"] = True
        return _QuickProc(rc=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(_settings)
    await daemon.start()
    await asyncio.gather(
        *[t for t in daemon._bg_tasks if not (t.get_name() or "").startswith("scheduler_")],
        return_exceptions=True,
    )
    assert created["done"]
    assert (_settings.project_root / "skills" / "skill-creator" / "SKILL.md").is_file()
    assert (_settings.data_dir / "run" / "skills.dirty").exists()
    await daemon.stop()
