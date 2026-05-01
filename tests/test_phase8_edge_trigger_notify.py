"""Phase 8 §2.7 — edge-trigger Telegram notify state machine.

AC#4 / AC#20 / AC#21:
- ok → fail edge fires ONE notify (the first failure).
- fail → fail with consecutive_failures < milestone is silent.
- consecutive_failures == milestone (5/10/24) fires a milestone
  notify.
- fail → ok edge fires a recovery notify, resets state.
- The state file persists across restart (a fresh instance loads the
  same state).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from assistant.adapters.base import (
    AttachmentKind,
    Handler,
    IncomingMessage,
    MessengerAdapter,
)
from assistant.config import VaultSyncSettings
from assistant.vault_sync.subsystem import VaultSyncSubsystem


class _FakeAdapter(MessengerAdapter):
    """Minimal ``MessengerAdapter`` impl that records ``send_text``
    calls in-memory."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def set_handler(self, handler: Handler) -> None:
        return None

    def set_transcription(self, _service: Any) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    async def reply(self, msg: IncomingMessage, text: str) -> None:
        self.messages.append((msg.chat_id, text))

    async def show_typing(self, _chat_id: int) -> None:
        return None

    async def stop_typing(self, _chat_id: int) -> None:
        return None

    async def download_attachment(
        self, _msg: IncomingMessage, _kind: AttachmentKind
    ) -> Path:
        raise NotImplementedError


def _build_subsystem(
    tmp_path: Path,
    *,
    adapter: MessengerAdapter | None,
) -> VaultSyncSubsystem:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    (vault / ".git").mkdir(exist_ok=True)
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    settings = VaultSyncSettings(
        enabled=True,
        repo_url="git@github.com:c0manch3/0xone-vault.git",
        manual_tool_enabled=True,
        notify_milestone_failures=(5, 10, 24),
    )
    return VaultSyncSubsystem(
        vault_dir=vault,
        index_db_lock_path=tmp_path / "memory-index.db.lock",
        settings=settings,
        adapter=adapter,
        owner_chat_id=42,
        run_dir=run,
        pending_set=set(),
    )


async def test_ok_to_fail_edge_notifies(tmp_path: Path) -> None:
    """First failure → notify; consecutive_failures = 1."""
    adapter = _FakeAdapter()
    sub = _build_subsystem(tmp_path, adapter=adapter)
    assert sub._state.last_state == "ok"
    await sub._handle_failure_edge("network down")
    assert sub._state.last_state == "fail"
    assert sub._state.consecutive_failures == 1
    assert len(adapter.messages) == 1
    assert "vault sync failed" in adapter.messages[0][1]


async def test_fail_to_fail_silent_until_milestone(
    tmp_path: Path,
) -> None:
    """Consecutive failures 2..4 are silent; failure #5 fires a
    milestone."""
    adapter = _FakeAdapter()
    sub = _build_subsystem(tmp_path, adapter=adapter)
    # Seed state to fail with 1 failure.
    sub._state.last_state = "fail"
    sub._state.consecutive_failures = 1
    sub._state.save(sub._state_path)
    for _ in range(3):  # 2, 3, 4 — silent
        await sub._handle_failure_edge("still down")
    assert sub._state.consecutive_failures == 4
    assert len(adapter.messages) == 0
    # 5th failure → milestone.
    await sub._handle_failure_edge("still down")
    assert sub._state.consecutive_failures == 5
    assert len(adapter.messages) == 1
    assert "still failing" in adapter.messages[0][1]
    assert "5" in adapter.messages[0][1]


async def test_milestone_notifies_at_10_and_24(tmp_path: Path) -> None:
    """Subsequent milestones at 10 and 24 also fire."""
    adapter = _FakeAdapter()
    sub = _build_subsystem(tmp_path, adapter=adapter)
    sub._state.last_state = "fail"
    sub._state.consecutive_failures = 9
    sub._state.save(sub._state_path)
    await sub._handle_failure_edge("err")  # → 10 → milestone
    assert sub._state.consecutive_failures == 10
    assert len(adapter.messages) == 1
    # Skip ahead to 23 silent failures, then 24th fires.
    sub._state.consecutive_failures = 23
    sub._state.save(sub._state_path)
    adapter.messages.clear()
    await sub._handle_failure_edge("err")  # → 24
    assert sub._state.consecutive_failures == 24
    assert len(adapter.messages) == 1


async def test_recovery_notify_resets_state(tmp_path: Path) -> None:
    """fail → ok edge fires a recovery message and resets state."""
    adapter = _FakeAdapter()
    sub = _build_subsystem(tmp_path, adapter=adapter)
    sub._state.last_state = "fail"
    sub._state.consecutive_failures = 3
    sub._state.save(sub._state_path)
    await sub._handle_success_edge()
    assert sub._state.last_state == "ok"
    assert sub._state.consecutive_failures == 0
    assert len(adapter.messages) == 1
    msg = adapter.messages[0][1]
    assert "recovered" in msg
    assert "3" in msg


async def test_ok_to_ok_silent(tmp_path: Path) -> None:
    """Successful tick when already ok → no notify."""
    adapter = _FakeAdapter()
    sub = _build_subsystem(tmp_path, adapter=adapter)
    assert sub._state.last_state == "ok"
    await sub._handle_success_edge()
    assert len(adapter.messages) == 0
    assert sub._state.last_state == "ok"


def test_state_persists_across_restart(tmp_path: Path) -> None:
    """A second subsystem instance against the same run_dir loads the
    persisted state — last_state and consecutive_failures survive."""
    adapter = _FakeAdapter()
    sub1 = _build_subsystem(tmp_path, adapter=adapter)
    sub1._state.last_state = "fail"
    sub1._state.consecutive_failures = 7
    sub1._state.save(sub1._state_path)
    sub2 = _build_subsystem(tmp_path, adapter=adapter)
    assert sub2._state.last_state == "fail"
    assert sub2._state.consecutive_failures == 7
