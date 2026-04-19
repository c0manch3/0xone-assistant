"""Phase 7 / commit 17 — E2E double-delivery race → `_DedupLedger` wins.

The two-level mitigation (pitfall #9) protects the owner from seeing
the same photo twice when:

  1. The main turn's final assistant text mentions an outbox path.
     `dispatch_reply` sends the photo + cleaned text.
  2. A worker subagent (spawned via `task spawn --kind worker`)
     wraps up within the 300 s TTL. Its `SubagentStop` hook also
     runs through `dispatch_reply` with the same path.

The model's SKILL.md guidance keeps case (1) paths out of the main
turn's final text, but the model is not 100% reliable. Invariant
I-7.5 gives us a belt-and-braces: a SHARED `_DedupLedger` instance
across the three call-sites (handler, scheduler, subagent hook)
means the second send-attempt for `(path, chat_id)` within 300 s is
dropped.

Scenarios:

  * **Main turn + subagent stop hit the same path → one send.**
    Concretely: invoke `dispatch_reply` twice with the same
    `(chat_id, path)`, sharing ONE `_DedupLedger`. The first call
    sends; the second is a noop.
  * **Different chats (path reused) → two sends.** Dedup key is
    `(path, chat_id)`, so two owners who happen to share an outbox
    artefact each get their copy.
  * **TTL expiry resurfaces the send.** With a mock clock, we push
    `time.monotonic()` past the TTL and verify the second call fires
    again (documents the 300 s window as a soft race guard, not a
    permanent suppression).
  * **Across the three call-sites.** A stitched mini-daemon runs
    scheduler + subagent-hook paths against one shared ledger and
    asserts the artefact is delivered exactly once in total.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import (
    _DEDUP_TTL_S,
    _DedupLedger,
    dispatch_reply,
)
from assistant.config import (
    ClaudeSettings,
    MediaSettings,
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


class _RecordingAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.photos: list[tuple[int, Path]] = []
        self.documents: list[tuple[int, Path]] = []
        self.audios: list[tuple[int, Path]] = []
        self.texts: list[tuple[int, str]] = []

    async def start(self) -> None:  # pragma: no cover
        return None

    async def stop(self) -> None:  # pragma: no cover
        return None

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.photos.append((chat_id, path))

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.documents.append((chat_id, path))

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        del caption
        self.audios.append((chat_id, path))


async def test_main_turn_plus_subagent_on_same_path_sends_once(tmp_path: Path) -> None:
    """Two back-to-back `dispatch_reply` calls, same path, same ledger.

    Mirrors the production race where the main turn emits the path
    (handler call-site) and the subagent's Stop hook emits the same
    path inside the 300 s TTL.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    photo = outbox / "shared.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")

    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    # Call #1 — mimics the main turn's dispatch_reply call-site.
    await dispatch_reply(
        adapter,
        chat_id=42,
        text=f"done: {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "main_turn"},
    )
    # Call #2 — mimics the subagent Stop-hook call-site, same path,
    # same chat, same ledger, well within 300 s.
    await dispatch_reply(
        adapter,
        chat_id=42,
        text=f"worker finished: {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "subagent_stop", "job_id": 123},
    )

    # Exactly one send.
    assert adapter.photos == [(42, photo.resolve())]
    # Both texts still reach Telegram — just the raw path stripped
    # from each.
    assert len(adapter.texts) == 2
    first_text = adapter.texts[0][1]
    second_text = adapter.texts[1][1]
    assert str(photo) not in first_text
    assert str(photo) not in second_text
    assert "done" in first_text
    assert "worker finished" in second_text


async def test_same_path_different_chats_each_get_their_send(tmp_path: Path) -> None:
    """Dedup key is `(path, chat_id)` — not globally path-scoped.

    Safety net for a future multi-chat deployment: a scheduler for
    chat A and a scheduler for chat B each produce their own
    artefact-send even if the outbox path happens to coincide.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    doc = outbox / "report.pdf"
    doc.write_bytes(b"%PDF-1.4\n")

    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    await dispatch_reply(
        adapter, chat_id=1, text=f"ready {doc}",
        outbox_root=outbox, dedup=ledger,
    )
    await dispatch_reply(
        adapter, chat_id=2, text=f"ready {doc}",
        outbox_root=outbox, dedup=ledger,
    )

    assert adapter.documents == [
        (1, doc.resolve()),
        (2, doc.resolve()),
    ]


async def test_ttl_expiry_allows_resend(tmp_path: Path) -> None:
    """Past the TTL window, the ledger stops suppressing the path.

    Documents the dedup as a SOFT race guard — it is NOT a permanent
    "never send this path to this chat again" block. Users who
    genuinely ask for the same artefact hours apart will see it.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    audio = outbox / "voice-sum.mp3"
    audio.write_bytes(b"ID3\x04")

    # Small TTL so we don't twiddle wall clock at all — inject `now`
    # via the ledger's `mark_and_check(..., now=...)` plumbing.
    ledger = _DedupLedger(ttl_s=10.0)

    # First dispatch at t=0 — hits the send path.
    # (dispatch_reply uses time.monotonic() internally; to drive the
    # ledger past the TTL deterministically we mark the key manually.)
    key = (str(audio.resolve()), 99)
    assert ledger.mark_and_check(key, now=0.0) is False
    # Same key within TTL → caller should SKIP.
    assert ledger.mark_and_check(key, now=5.0) is True
    # Past TTL → caller proceeds.
    assert ledger.mark_and_check(key, now=100.0) is False


def test_dedup_ttl_is_300s_invariant() -> None:
    """Sanity check: the plan-documented 300 s window is the default.

    Phase-7 plan §2.6 + pitfall #9 both anchor on a 5-minute race
    window. A silent drift here would break the subagent ↔ main-turn
    mitigation window.
    """
    assert _DEDUP_TTL_S == 300.0
    default_ledger = _DedupLedger()
    # Probe it — first mark returns False, same key at t=299 returns True.
    assert default_ledger.mark_and_check(("/a/b.png", 1), now=0.0) is False
    assert default_ledger.mark_and_check(("/a/b.png", 1), now=299.0) is True
    assert default_ledger.mark_and_check(("/a/b.png", 1), now=301.0) is False


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
        media=MediaSettings(),
    )


async def test_three_call_sites_share_one_ledger_and_dedup_across(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The "H-11 shape" in practice: scheduler + subagent-hook share one
    ledger, and two near-simultaneous dispatches for the same path
    produce exactly ONE send across the three legs.

    We simulate the scheduler leg by calling `dispatch_reply` directly
    (same code-path as the commit-14 refactor), and we simulate the
    subagent leg by firing the real Stop hook with a body that carries
    the same outbox path. Assertion: one photo delivered, one text
    per call-site (cleaned of the path).
    """
    settings = _settings(tmp_path)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = outbox / "morning.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")

    adapter = _RecordingAdapter()
    ledger = _DedupLedger()

    conn = await connect(tmp_path / "race.db")
    await apply_schema(conn)
    store = SubagentStore(conn, lock=asyncio.Lock())
    # Seed a subagent row so the Stop hook has something to latch onto.
    job_id = await store.record_started(
        sdk_agent_id="agent-race",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=settings.owner_chat_id,
        spawned_by_kind="cli",
        spawned_by_ref=None,
    )
    assert isinstance(job_id, int)

    # Scheduler leg — dispatch_reply with the same ledger.
    await dispatch_reply(
        adapter,
        chat_id=settings.owner_chat_id,
        text=f"готово: {photo}",
        outbox_root=outbox,
        dedup=ledger,
        log_ctx={"origin": "scheduler", "schedule_id": 1},
    )

    # Subagent Stop leg — fires the real hook. `make_subagent_hooks`
    # threads the SAME ledger via the factory, so the closure's
    # `dispatch_reply` call goes through the same dedup key.
    pending: set[asyncio.Task[Any]] = set()
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
        dedup_ledger=ledger,
    )
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await stop_cb(
        {
            "agent_id": "agent-race",
            "agent_transcript_path": None,
            "session_id": "s",
            "last_assistant_message": f"picture ready: {photo}",
        },
        None,
        None,
    )
    # Drain the shielded `_deliver` task.
    await asyncio.gather(*list(pending), return_exceptions=True)

    # Shared ledger → exactly one photo send total.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())], (
        f"expected dedup to suppress the second send, got: {adapter.photos!r}"
    )

    # Each leg still produced its own cleaned-text send; both
    # omit the raw path.
    assert len(adapter.texts) >= 1
    for chat_id, text in adapter.texts:
        assert chat_id == settings.owner_chat_id
        assert str(photo) not in text, (
            f"path leaked into send_text output: {text!r}"
        )

    await conn.close()


def test_subagent_hook_factory_accepts_dedup_ledger_kwarg() -> None:
    """Regression guard for H-11: the factory surface includes
    `dedup_ledger` (so shared-ledger wiring is possible) and does NOT
    include `outbox_root` (which is derived from `settings.data_dir`
    inside the hook closure)."""
    sig = inspect.signature(make_subagent_hooks)
    assert "dedup_ledger" in sig.parameters
    assert "outbox_root" not in sig.parameters, (
        "H-11 regression: outbox_root must be derived from settings, "
        "not threaded through the factory."
    )
    # Sanity: the dispatch_reply symbol imported by the hook module is
    # the one we're exercising (not a shadowed fake left over from a
    # prior test monkeypatch).
    assert hooks_module.dispatch_reply is dispatch_reply
