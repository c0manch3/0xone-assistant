"""Phase 7 fix-pack C1 — main-turn reply → ``dispatch_reply`` wiring.

Before the fix, ``TelegramAdapter._dispatch_to_handler`` ended with
``await self.send_text(chat_id, full)``. If the main-turn reply
mentioned an outbox artefact path (e.g. ``/…/outbox/x.png``), that
path would be delivered as a text string rather than as a
``send_photo`` / ``send_document`` / ``send_audio`` call. The
``_DedupLedger`` (invariant I-7.5) never saw the key, so a subsequent
subagent Stop hook emitting the same path would ALSO send the artefact
— breaking at-most-once delivery across the three call-sites.

Fix: when the adapter is constructed with a ``dedup_ledger``, route
the reply through ``dispatch_reply``. When the ledger is ``None``
(legacy tests not supplying one), fall back to raw ``send_text`` —
preserves back-compat without reintroducing the bug for the daemon
path, which now always threads the ledger.

Scenarios covered:

* Single main-turn reply containing an artefact path → ``send_photo``
  fires once, the cleaned text (with the path stripped) is sent via
  ``send_text``.
* Main-turn + subagent Stop hook both mentioning the same path →
  only ONE network photo send (I-7.5 dedup via shared ledger).
* Adapter built WITHOUT a ledger → legacy ``send_text`` path still
  receives the raw reply (back-compat).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.adapters.telegram import TelegramAdapter
from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.media.paths import ensure_media_dirs, outbox_dir
from assistant.state.db import apply_schema, connect
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.store import SubagentStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(notify_throttle_ms=1),
    )


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self, chat_id: int, text: str, message_id: int = 1) -> None:
        self.text = text
        self.chat = _FakeChat(chat_id)
        self.message_id = message_id


class _NoopCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _fake_typing(**_kwargs: Any) -> _NoopCtx:
    return _NoopCtx()


class _RecordingHandler:
    """Handler whose `handle` emits a fixed reply once."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.received: list[IncomingMessage] = []

    async def handle(self, msg: IncomingMessage, emit: Any) -> None:
        self.received.append(msg)
        await emit(self._reply)


async def test_main_turn_reply_with_outbox_path_sends_photo_and_cleans_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main-turn reply ends with an outbox `.png` path → `send_photo`
    fires exactly once, the cleaned text (path stripped) arrives via
    `send_text`."""
    settings = _settings(tmp_path)
    await ensure_media_dirs(settings.data_dir)
    outbox = outbox_dir(settings.data_dir)
    artefact = outbox / "a.png"
    artefact.write_bytes(b"PNG")

    ledger = _DedupLedger()
    adapter = TelegramAdapter(settings, dedup_ledger=ledger)

    reply_body = f"готово: {artefact}"
    adapter.set_handler(_RecordingHandler(reply_body))  # type: ignore[arg-type]

    photos: list[Path] = []
    texts: list[str] = []

    async def fake_send_photo(
        chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        photos.append(path)

    async def fake_send_text(chat_id: int, text: str) -> None:
        del chat_id
        texts.append(text)

    monkeypatch.setattr(adapter, "send_photo", fake_send_photo)
    monkeypatch.setattr(adapter, "send_text", fake_send_text)
    monkeypatch.setattr(
        "assistant.adapters.telegram.ChatActionSender.typing", _fake_typing
    )

    await adapter._on_text(_FakeMessage(chat_id=42, text="сделай картинку"))  # type: ignore[arg-type]

    # Exactly one photo delivered (via dispatch_reply path).
    assert len(photos) == 1
    assert photos[0] == artefact.resolve()
    # Cleaned text survives, with the raw path stripped.
    assert len(texts) == 1
    assert str(artefact) not in texts[0]
    assert "готово" in texts[0]


async def test_main_turn_and_subagent_stop_share_dedup_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main-turn reply mentions an outbox path; subagent Stop hook
    fires later mentioning the SAME path. With a shared `_DedupLedger`
    the photo is sent exactly ONCE across the two call-sites (I-7.5).
    """
    settings = _settings(tmp_path)
    await ensure_media_dirs(settings.data_dir)
    outbox = outbox_dir(settings.data_dir)
    artefact = outbox / "shared.png"
    artefact.write_bytes(b"PNG")

    ledger = _DedupLedger()
    adapter = TelegramAdapter(settings, dedup_ledger=ledger)

    photos: list[Path] = []

    async def fake_send_photo(
        chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        photos.append(path)

    async def fake_send_text(chat_id: int, text: str) -> None:
        del chat_id, text

    monkeypatch.setattr(adapter, "send_photo", fake_send_photo)
    monkeypatch.setattr(adapter, "send_text", fake_send_text)
    monkeypatch.setattr(
        "assistant.adapters.telegram.ChatActionSender.typing", _fake_typing
    )

    # Main-turn leg: reply body containing the artefact path.
    reply_body = f"готово: {artefact}"
    adapter.set_handler(_RecordingHandler(reply_body))  # type: ignore[arg-type]
    await adapter._on_text(_FakeMessage(chat_id=42, text="do it"))  # type: ignore[arg-type]

    # Subagent Stop leg: build hooks against the SAME ledger and fire
    # a Stop event whose `last_assistant_message` also carries the path.
    conn = await connect(tmp_path / "h.db")
    try:
        await apply_schema(conn)
        store = SubagentStore(conn, lock=asyncio.Lock())
        pending: set[asyncio.Task[Any]] = set()
        hooks = make_subagent_hooks(
            store=store,
            adapter=adapter,
            settings=settings,
            pending_updates=pending,
            dedup_ledger=ledger,
        )
        start_cb = hooks["SubagentStart"][0].hooks[0]
        stop_cb = hooks["SubagentStop"][0].hooks[0]
        await start_cb(
            {"agent_id": "agent-shared-1", "agent_type": "general", "session_id": "p"},
            None,
            None,
        )
        await stop_cb(
            {
                "agent_id": "agent-shared-1",
                "agent_transcript_path": None,
                "session_id": "s",
                "last_assistant_message": f"finished: {artefact}",
            },
            None,
            None,
        )
        # Drain the scheduled notify task.
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
    finally:
        await conn.close()

    # I-7.5: photo delivered EXACTLY ONCE across both call-sites.
    assert len(photos) == 1, (
        f"expected 1 photo send across main-turn + subagent Stop, "
        f"got {len(photos)} ({photos!r})"
    )


async def test_main_turn_without_ledger_falls_back_to_send_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: adapter constructed WITHOUT a ledger preserves the
    legacy plain-text path.

    This matters for every legacy test that builds a `TelegramAdapter`
    without plumbing a ledger through — they must not start failing
    because the fix-pack introduced a mandatory dependency.
    """
    settings = _settings(tmp_path)
    await ensure_media_dirs(settings.data_dir)
    outbox = outbox_dir(settings.data_dir)
    artefact = outbox / "legacy.png"
    artefact.write_bytes(b"PNG")

    adapter = TelegramAdapter(settings)  # no ledger
    assert adapter._dedup_ledger is None

    photos: list[Path] = []
    texts: list[str] = []

    async def fake_send_photo(
        chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del chat_id, caption
        photos.append(path)

    async def fake_send_text(chat_id: int, text: str) -> None:
        del chat_id
        texts.append(text)

    reply_body = f"готово: {artefact}"
    adapter.set_handler(_RecordingHandler(reply_body))  # type: ignore[arg-type]
    monkeypatch.setattr(adapter, "send_photo", fake_send_photo)
    monkeypatch.setattr(adapter, "send_text", fake_send_text)
    monkeypatch.setattr(
        "assistant.adapters.telegram.ChatActionSender.typing", _fake_typing
    )

    await adapter._on_text(_FakeMessage(chat_id=42, text="do it"))  # type: ignore[arg-type]

    # Legacy path: no photo delivery, raw reply (including the path)
    # routed through send_text unchanged.
    assert photos == []
    assert texts == [reply_body]


async def test_main_turn_sends_inside_typing_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix-pack I5: the final dispatch must happen INSIDE the
    `ChatActionSender.typing(...)` context so the 'typing…' indicator
    stays visible through the send, not just through the handler."""
    settings = _settings(tmp_path)
    await ensure_media_dirs(settings.data_dir)

    ledger = _DedupLedger()
    adapter = TelegramAdapter(settings, dedup_ledger=ledger)

    observed_order: list[str] = []

    class _TracingCtx:
        async def __aenter__(self) -> None:
            observed_order.append("typing_enter")

        async def __aexit__(self, *_exc: object) -> None:
            observed_order.append("typing_exit")

    def _tracing_typing(**_kwargs: Any) -> _TracingCtx:
        return _TracingCtx()

    async def fake_send_text(chat_id: int, text: str) -> None:
        del chat_id, text
        observed_order.append("send_text")

    adapter.set_handler(_RecordingHandler("plain reply"))  # type: ignore[arg-type]
    monkeypatch.setattr(adapter, "send_text", fake_send_text)
    monkeypatch.setattr(
        "assistant.adapters.telegram.ChatActionSender.typing", _tracing_typing
    )

    await adapter._on_text(_FakeMessage(chat_id=42, text="hi"))  # type: ignore[arg-type]

    # send_text must occur BEFORE typing_exit.
    assert observed_order == ["typing_enter", "send_text", "typing_exit"], (
        f"send must run inside the typing ctx, got {observed_order!r}"
    )
