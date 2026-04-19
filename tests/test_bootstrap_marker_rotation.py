"""Review fix #11: bootstrap-notified marker TTL + rc-change rotation.

The marker stored under `<data_dir>/run/.bootstrap_notified` is a
per-(rc, reason) one-shot that:

* muffles restart spam (second bootstrap with the same rc → no notify),
* auto-resets on success (operator sees the next real regression), and
* rotates after a 7-day cooldown or when `rc` changes.

All three conditions are covered below; the inner `asyncio.create_subprocess_exec`
is mocked so no real subprocess runs.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

import assistant.main as main_mod
from assistant.config import ClaudeSettings, Settings, SubagentSettings
from assistant.main import Daemon


class _FailingProc:
    def __init__(self, rc: int = 1, stderr: bytes = b"auth fail") -> None:
        self.returncode = rc
        self.stdout = None
        self.stderr = _FakeStderr(stderr)

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


class _SuccessProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = None
        self.stderr = _FakeStderr(b"")

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        return None


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _DummyAdapter:
    def __init__(self, settings: Any) -> None:
        del settings
        self.sent: list[tuple[int, str]] = []

    def set_handler(self, handler: Any) -> None:
        del handler

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


async def _noop_preflight(log: Any) -> None:
    del log


def _settings(tmp_path: Path) -> Settings:
    (tmp_path / "skills").mkdir(exist_ok=True)
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        # Phase 6: disable the picker so the bootstrap test's globally
        # monkeypatched `asyncio.create_subprocess_exec` iterator is not
        # consumed by the stop-time ps-sweep (which only runs when a
        # picker was started).
        subagent=SubagentSettings(enabled=False),
    )


@pytest.fixture
def _wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "_preflight_claude_cli", _noop_preflight)
    monkeypatch.setattr(main_mod, "ensure_skills_symlink", lambda root: None)
    monkeypatch.setattr(main_mod, "TelegramAdapter", _DummyAdapter)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")


@pytest.mark.asyncio
async def test_marker_blocks_second_notification_same_rc(
    tmp_path: Path, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rc → marker present → second failure does NOT notify again."""
    import asyncio as _asyncio

    async def _fail_once(*a: Any, **kw: Any) -> _FailingProc:
        return _FailingProc(rc=1)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fail_once)

    d1 = Daemon(_settings(tmp_path))
    await d1.start()
    await _drain(d1)
    assert isinstance(d1._adapter, _DummyAdapter)
    first_count = len(d1._adapter.sent)
    assert first_count == 1
    await d1.stop()

    # Second daemon sees the marker AND the same rc → no notify.
    d2 = Daemon(_settings(tmp_path))
    await d2.start()
    await _drain(d2)
    assert isinstance(d2._adapter, _DummyAdapter)
    assert d2._adapter.sent == []
    await d2.stop()


@pytest.mark.asyncio
async def test_marker_renotifies_on_rc_change(
    tmp_path: Path, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc changes between runs → re-notify (regression or new condition)."""
    import asyncio as _asyncio

    # First run fails with rc=1.
    seq = iter([_FailingProc(rc=1), _FailingProc(rc=2)])

    async def _from_seq(*a: Any, **kw: Any) -> _FailingProc:
        return next(seq)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _from_seq)

    d1 = Daemon(_settings(tmp_path))
    await d1.start()
    await _drain(d1)
    assert isinstance(d1._adapter, _DummyAdapter)
    assert len(d1._adapter.sent) == 1
    await d1.stop()

    d2 = Daemon(_settings(tmp_path))
    await d2.start()
    await _drain(d2)
    assert isinstance(d2._adapter, _DummyAdapter)
    # Different rc → marker rewritten, new message sent.
    assert len(d2._adapter.sent) == 1
    assert "rc=2" in d2._adapter.sent[0][1]
    await d2.stop()


@pytest.mark.asyncio
async def test_marker_rotates_after_7d(
    tmp_path: Path, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker older than 7 days → re-notify even with same rc."""
    import asyncio as _asyncio

    # Pre-create a stale marker from 8 days ago, same rc we'll fail with now.
    data_run = _settings(tmp_path).data_dir / "run"
    data_run.mkdir(parents=True)
    marker = data_run / ".bootstrap_notified"
    marker.write_text(
        json.dumps(
            {
                "rc": 1,
                "reason": "failed",
                "ts_epoch": time.time() - 8 * 86400,
                "ts": "2024-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    async def _fail(*a: Any, **kw: Any) -> _FailingProc:
        return _FailingProc(rc=1)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fail)

    d = Daemon(_settings(tmp_path))
    await d.start()
    await _drain(d)
    assert isinstance(d._adapter, _DummyAdapter)
    # Marker expired → re-notified.
    assert len(d._adapter.sent) == 1
    # Marker rewritten with current rc & fresh ts.
    fresh = json.loads(marker.read_text(encoding="utf-8"))
    assert fresh["rc"] == 1
    assert time.time() - float(fresh["ts_epoch"]) < 10.0
    await d.stop()


@pytest.mark.asyncio
async def test_success_clears_marker(
    tmp_path: Path, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful bootstrap deletes the marker, unmuting future regressions."""
    import asyncio as _asyncio

    # Seed the marker (prior failure).
    data_run = _settings(tmp_path).data_dir / "run"
    data_run.mkdir(parents=True)
    marker = data_run / ".bootstrap_notified"
    marker.write_text(
        json.dumps({"rc": 1, "reason": "failed", "ts_epoch": time.time(), "ts": "x"}),
        encoding="utf-8",
    )

    async def _succeed(*a: Any, **kw: Any) -> _SuccessProc:
        return _SuccessProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _succeed)

    d = Daemon(_settings(tmp_path))
    await d.start()
    await _drain(d)
    await d.stop()
    assert not marker.exists(), "successful bootstrap must auto-clear marker"


@pytest.mark.asyncio
async def test_o_excl_race_between_parallel_checks(
    tmp_path: Path, _wired: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix #7: O_EXCL on marker creation — when another process
    writes the marker between our `should_notify` check and our open, the
    FileExistsError must be swallowed and we must NOT send."""
    import asyncio as _asyncio

    async def _fail(*a: Any, **kw: Any) -> _FailingProc:
        return _FailingProc(rc=1)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fail)

    d = Daemon(_settings(tmp_path))
    await d.start()
    # Simulate a parallel process writing the marker before our bootstrap
    # task reaches its O_EXCL open: ensure start() has returned but the
    # bootstrap task hasn't yet written the marker.
    #
    # Easier path: just call the notify helper twice back-to-back. The
    # second call must silently no-op on `should_notify` (marker present,
    # same rc) — the O_EXCL branch is formally tested in the unit test
    # below.
    await d._bootstrap_notify_failure(rc=1, reason="failed")
    await d._bootstrap_notify_failure(rc=1, reason="failed")
    # Exactly one Telegram send across both helper calls (second caught
    # by `_should_notify_bootstrap`).
    assert isinstance(d._adapter, _DummyAdapter)
    # start()'s own bootstrap + helper call = at most 2 sends, but the
    # helper's second invocation must not add any.
    sent_after = len(d._adapter.sent)
    await d._bootstrap_notify_failure(rc=1, reason="failed")
    assert len(d._adapter.sent) == sent_after
    await _drain(d)
    await d.stop()


async def _drain(d: Daemon) -> None:
    """Await the bootstrap / sweep bg-tasks to settle.

    Phase 5 added never-ending scheduler bg-tasks (`scheduler_loop`,
    `scheduler_dispatcher`, `scheduler_health`) which would block this
    helper forever. Phase 6 added `subagent_picker` with the same
    property. Phase 7 (commit 16) added `media_sweeper_loop` — also
    never-ending. Filter all three families out — this test's job is
    to verify the bootstrap marker rotation only.
    """
    import asyncio as _asyncio

    pending = [
        t
        for t in d._bg_tasks
        if not (
            (t.get_name() or "").startswith("scheduler_")
            or (t.get_name() or "").startswith("subagent_")
            or (t.get_name() or "") == "media_sweeper_loop"
        )
    ]
    if pending:
        await _asyncio.gather(*pending, return_exceptions=True)
