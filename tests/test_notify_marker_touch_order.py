"""Phase 5 fix-pack HIGH #6 — `_notify_with_marker` touches the marker
BEFORE `send_text`.

Reserving the cooldown up-front prevents a duplicate recap on a quick
restart: if `send_text` fails (timeout, flood-wait, crash during chunk
send), the marker is still written so the next boot respects the
cooldown. A lost notification is strictly better than spamming the
owner with the same "пропущено N" message on every restart in a
reboot loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assistant.config import ClaudeSettings, Settings
from assistant.main import Daemon


class _FakeAdapterRaises:
    """Adapter whose `send_text` always raises. Exercises the "send fails
    but marker was already touched" path."""

    def __init__(self) -> None:
        self.sends: list[tuple[int, str]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sends.append((chat_id, text))
        raise RuntimeError("telegram down")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


async def test_marker_written_even_when_send_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)
    daemon = Daemon(_settings(tmp_path))
    daemon._adapter = _FakeAdapterRaises()  # type: ignore[assignment]

    marker = tmp_path / "data" / "run" / ".marker-test"

    # send_text raises — the notify wrapper must still touch the marker.
    await daemon._notify_with_marker(marker, cooldown_s=3600, msg="hi")

    assert marker.exists(), "marker must be touched even on send_text failure"


async def test_marker_blocks_second_call_within_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure the fresh-marker check still short-circuits a second call
    within the cooldown window after a failed send."""
    del monkeypatch
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)
    adapter = _FakeAdapterRaises()
    daemon = Daemon(_settings(tmp_path))
    daemon._adapter = adapter  # type: ignore[assignment]

    marker = tmp_path / "data" / "run" / ".marker-test"
    await daemon._notify_with_marker(marker, cooldown_s=3600, msg="first")
    # second call within cooldown — must NOT attempt send_text.
    await daemon._notify_with_marker(marker, cooldown_s=3600, msg="second")

    # Exactly ONE send_text attempt (which raised).
    assert adapter.sends == [(42, "first")], f"expected single attempt, got {adapter.sends!r}"


async def test_bypass_still_writes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bypass=True` still touches the marker so per-instance latching (for
    the heartbeat watchdog) has a place to read cooldowns from if needed
    on a subsequent daemon process."""
    del monkeypatch
    (tmp_path / "data" / "run").mkdir(parents=True, exist_ok=True)

    class _OkAdapter:
        def __init__(self) -> None:
            self.sends: list[tuple[int, str]] = []

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send_text(self, chat_id: int, text: str) -> None:
            self.sends.append((chat_id, text))

    daemon = Daemon(_settings(tmp_path))
    daemon._adapter = _OkAdapter()  # type: ignore[assignment]

    marker = tmp_path / "data" / "run" / ".bypass-marker"
    await daemon._notify_with_marker(marker, cooldown_s=3600, msg="x", bypass=True)
    assert marker.exists()


_ = asyncio  # keep import for lint consistency
_ = Any
