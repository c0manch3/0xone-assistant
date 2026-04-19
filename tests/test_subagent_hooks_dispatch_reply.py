"""Phase 7 / commit 15 — subagent Stop hook → `dispatch_reply` switch.

Covers the H-11 fix-pack invariant: `make_subagent_hooks` does NOT
take an `outbox_root` parameter. The Stop-hook closure derives it
via `outbox_dir(settings.data_dir)` on every fire so the two values
can never drift apart.

Scenarios:
  * Stop hook with a body that contains an outbox artefact path →
    `dispatch_reply` is invoked, and its `outbox_root` kwarg equals
    `outbox_dir(settings.data_dir)`.
  * Factory signature introspection: `inspect.signature` contains
    `dedup_ledger` but NOT `outbox_root`.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger
from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.media.paths import outbox_dir
from assistant.state.db import apply_schema, connect
from assistant.subagent import hooks as hooks_module
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.store import SubagentStore


class _FakeAdapter(MessengerAdapter):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> None: ...

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(notify_throttle_ms=1),
    )


async def _mkstore(tmp_path: Path) -> SubagentStore:
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    return SubagentStore(conn, lock=asyncio.Lock())


def test_factory_signature_drops_outbox_root() -> None:
    """H-11: factory gains ONLY `dedup_ledger`; `outbox_root` MUST NOT appear."""
    sig = inspect.signature(make_subagent_hooks)
    params = sig.parameters
    assert "dedup_ledger" in params, (
        f"dedup_ledger missing from make_subagent_hooks signature: {params!r}"
    )
    assert "outbox_root" not in params, (
        "H-11 regression: `outbox_root` must be derived inside the hook "
        f"closure, not threaded through the factory. Got params: {list(params)!r}"
    )


async def test_stop_hook_invokes_dispatch_reply_with_derived_outbox_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """SubagentStop → `dispatch_reply(outbox_root=outbox_dir(data_dir), ...)`."""
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[Any]] = set()
    settings = _settings(tmp_path)
    dedup = _DedupLedger()

    captured: dict[str, Any] = {}

    async def _fake_dispatch(
        adapter_arg: MessengerAdapter,
        chat_id: int,
        text: str,
        *,
        outbox_root: Path,
        dedup: _DedupLedger,
        log_ctx: dict[str, Any] | None = None,
    ) -> None:
        captured["adapter"] = adapter_arg
        captured["chat_id"] = chat_id
        captured["text"] = text
        captured["outbox_root"] = outbox_root
        captured["dedup"] = dedup
        captured["log_ctx"] = log_ctx

    # Patch where the symbol is USED (subagent.hooks), not where it's defined.
    monkeypatch.setattr(hooks_module, "dispatch_reply", _fake_dispatch)

    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
        dedup_ledger=dedup,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-dr-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )
    await stop_cb(
        {
            "agent_id": "agent-dr-1",
            "agent_transcript_path": None,
            "session_id": "s",
            # Body with a path-looking token; dispatch_reply is mocked
            # so we don't need the file to actually exist on disk.
            "last_assistant_message": "done: /tmp/outbox/x.png",
        },
        None,
        None,
    )
    # Drain the scheduled `_deliver` task.
    await asyncio.gather(*list(pending), return_exceptions=True)

    assert captured, "dispatch_reply was never invoked"
    assert captured["chat_id"] == settings.owner_chat_id
    # H-11: derived inside the closure, identical to outbox_dir(data_dir).
    assert captured["outbox_root"] == outbox_dir(settings.data_dir)
    assert captured["dedup"] is dedup
    assert captured["log_ctx"] == {"job_id": 1}
    assert "done" in captured["text"]

    await store._conn.close()
