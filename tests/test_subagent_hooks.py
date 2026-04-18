"""Phase 6 / commit 4 — subagent hook behaviour.

Covers:
  * `on_subagent_start` with `CURRENT_REQUEST_ID` set → calls
    `update_sdk_agent_id_for_claimed_request` (B-W2-4 picker path).
  * `on_subagent_start` with var unset → calls `record_started`
    (native-Task main-turn path).
  * `on_subagent_stop` returns `{}` IMMEDIATELY; the delivery task is
    registered on the `pending_updates` set (GAP #12).
  * Stop primary path reads `last_assistant_message`; empty → JSONL
    transcript fallback with 250 ms sleep retry.
  * Stop empty body still writes the row + notifies a placeholder.
  * `on_pretool_cancel_gate`: returns deny iff `agent_id` is present
    AND the ledger flag is set; no-ops otherwise.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from assistant.adapters.base import MessengerAdapter
from assistant.config import (
    ClaudeSettings,
    MemorySettings,
    SchedulerSettings,
    Settings,
    SubagentSettings,
)
from assistant.state.db import apply_schema, connect
from assistant.subagent.context import CURRENT_REQUEST_ID
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.store import SubagentStore


class _FakeAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._send_event = asyncio.Event()

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
        self._send_event.set()

    # Phase 7 (commit 4) abstract-compliance stubs. These hook tests
    # exercise `send_text` only; full media fakes arrive with the
    # Wave B / Wave 7A test matrix that actually invokes dispatch_reply.
    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError

    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError

    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None:
        raise NotImplementedError


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=42,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        memory=MemorySettings(),
        scheduler=SchedulerSettings(),
        subagent=SubagentSettings(
            # Short throttle for tests so we don't sit there waiting.
            notify_throttle_ms=1,
        ),
    )


async def _mkstore(tmp_path: Path) -> SubagentStore:
    conn = await connect(tmp_path / "h.db")
    await apply_schema(conn)
    lock = asyncio.Lock()
    return SubagentStore(conn, lock=lock)


async def _drain(pending: set[asyncio.Task[object]]) -> None:
    if not pending:
        return
    await asyncio.gather(*list(pending), return_exceptions=True)


# ----------------------------------------------------------------- start hook


async def test_start_hook_picker_path_patches_row(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]

    request_id = await store.record_pending_request(
        agent_type="researcher",
        task_text="find cves",
        callback_chat_id=42,
        spawned_by_kind="cli",
    )

    token = CURRENT_REQUEST_ID.set(request_id)
    try:
        out = await start_cb(
            {
                "agent_id": "agent-picker-1",
                "agent_type": "researcher",
                "session_id": "sess-parent",
            },
            None,
            None,
        )
    finally:
        CURRENT_REQUEST_ID.reset(token)

    assert out == {}
    job = await store.get_by_id(request_id)
    assert job is not None
    assert job.sdk_agent_id == "agent-picker-1"
    assert job.status == "started"
    assert job.parent_session_id == "sess-parent"
    await store._conn.close()


async def test_start_hook_native_task_path_inserts_row(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]

    # No ContextVar set → native-Task path.
    out = await start_cb(
        {
            "agent_id": "agent-native-1",
            "agent_type": "general",
            "session_id": "sess-parent",
        },
        None,
        None,
    )
    assert out == {}
    job = await store.get_by_agent_id("agent-native-1")
    assert job is not None
    assert job.status == "started"
    assert job.agent_type == "general"
    assert job.spawned_by_kind == "user"
    await store._conn.close()


async def test_start_hook_fallthrough_preserves_cli_attribution(
    tmp_path: Path,
) -> None:
    """Fix-pack HIGH #4 (CR I-5): if
    `update_sdk_agent_id_for_claimed_request` fails (e.g. the row
    was already transitioned by a racy recover_orphans run, or a
    status-precondition skew) the Start hook previously fell
    through to `record_started` with a hard-coded
    `spawned_by_kind='user'`. A CLI spawn would silently lose its
    'cli' attribution and show up in list output as if the owner
    had typed "use Task" inline. This test seeds a CLI-pending
    row, simulates the claim UPDATE failing (we set its status to
    `started` so the precondition `WHERE status='requested'`
    doesn't fire), then calls the Start hook with the ContextVar
    set to that row's id. The fallback INSERT must preserve
    `spawned_by_kind='cli'` from the original row.
    """
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]

    request_id = await store.record_pending_request(
        agent_type="researcher",
        task_text="find stuff",
        callback_chat_id=999,  # distinct from owner_chat_id (42)
        spawned_by_kind="cli",
        spawned_by_ref="test-ref",
    )
    # Force the claim UPDATE to fail by pre-transitioning the row
    # to `started` via raw SQL — the `WHERE status='requested'`
    # precondition will now see no rows to update.
    async with store._lock:
        await store._conn.execute(
            "UPDATE subagent_jobs SET status='started' WHERE id=?",
            (request_id,),
        )
        await store._conn.commit()

    token = CURRENT_REQUEST_ID.set(request_id)
    try:
        out = await start_cb(
            {
                "agent_id": "agent-fallthrough-1",
                "agent_type": "researcher",
                "session_id": "sess-parent",
            },
            None,
            None,
        )
    finally:
        CURRENT_REQUEST_ID.reset(token)

    assert out == {}
    # Fallthrough INSERT landed a NEW row with the ORIGINAL CLI
    # attribution — not `'user'`.
    inserted = await store.get_by_agent_id("agent-fallthrough-1")
    assert inserted is not None
    assert inserted.spawned_by_kind == "cli", inserted
    assert inserted.spawned_by_ref == "test-ref", inserted
    # Callback chat id also preserved from the original row.
    assert inserted.callback_chat_id == 999
    await store._conn.close()


async def test_start_hook_fallthrough_vanished_row_uses_unknown_marker(
    tmp_path: Path,
) -> None:
    """Fix-pack HIGH #4 (CR I-5) edge case: if the original row has
    vanished between CLI insert and Start hook (a DELETE truly out
    of nowhere), the fallback must use `'unknown'` — a distinct
    marker that observability can flag, rather than silently
    mis-attributing to 'user'.
    """
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]

    # Set the ContextVar to a row id that doesn't exist anywhere.
    token = CURRENT_REQUEST_ID.set(99999)
    try:
        await start_cb(
            {
                "agent_id": "agent-vanished-1",
                "agent_type": "general",
                "session_id": "s",
            },
            None,
            None,
        )
    finally:
        CURRENT_REQUEST_ID.reset(token)

    inserted = await store.get_by_agent_id("agent-vanished-1")
    assert inserted is not None
    assert inserted.spawned_by_kind == "unknown", inserted
    await store._conn.close()


async def test_start_hook_no_agent_id_is_noop(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=_settings(tmp_path),
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    out = await start_cb({"agent_type": "general"}, None, None)
    assert out == {}
    await store._conn.close()


# ----------------------------------------------------------------- stop hook


async def test_stop_hook_returns_empty_and_registers_pending(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-stop-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )

    out = await stop_cb(
        {
            "agent_id": "agent-stop-1",
            "agent_transcript_path": None,
            "session_id": "s-child",
            "last_assistant_message": "hello owner",
        },
        None,
        None,
    )
    # GAP #12: hook returns immediately.
    assert out == {}
    # Delivery task registered.
    assert len(pending) == 1

    # Drain — the delivery uses asyncio.shield + a faked adapter.
    await _drain(pending)

    job = await store.get_by_agent_id("agent-stop-1")
    assert job is not None
    assert job.status == "completed"
    assert job.result_summary == "hello owner"

    # Notify was sent.
    assert len(adapter.sent) == 1
    chat_id, body = adapter.sent[0]
    assert chat_id == 42
    assert "hello owner" in body
    await store._conn.close()


async def test_stop_hook_cancelled_status_stopped(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-cancel-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )
    # Pre-set the cancel flag.
    job = await store.get_by_agent_id("agent-cancel-1")
    assert job is not None
    await store.set_cancel_requested(job.id)

    await stop_cb(
        {
            "agent_id": "agent-cancel-1",
            "agent_transcript_path": None,
            "session_id": "s-child",
            "last_assistant_message": "partial",
        },
        None,
        None,
    )
    await _drain(pending)

    after = await store.get_by_agent_id("agent-cancel-1")
    assert after is not None
    assert after.status == "stopped"
    await store._conn.close()


async def test_stop_hook_large_transcript_read_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    """Fix-pack CRITICAL #4 (CR I-6 pitfall #12).

    Hook pitfall #12: any blocking call >500 ms inside an async hook
    stalls the SDK iterator. A subagent that streamed many assistant
    messages can leave a multi-MB JSONL transcript; the pre-fix code
    ran `path.read_text()` directly, which blocks the event loop.

    This test builds a ~2 MB transcript with many assistant entries
    and asserts that a concurrent ticker coroutine ticks at least
    once every 200 ms while the hook is running. On the regression
    path (direct sync read), the ticker would miss ticks because the
    loop is blocked inside `read_text`. With `asyncio.to_thread` the
    file I/O runs on a worker thread and the loop stays cooperative.
    """
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-big-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )

    # Build a sizable JSONL transcript. Each assistant entry is ~2 KB
    # of padded text; 1500 entries ≈ 3 MB — big enough that a
    # synchronous read on disk takes measurable time on any machine.
    transcript = tmp_path / "big.jsonl"
    lines: list[str] = []
    padding = "x" * 2000
    for i in range(1500):
        lines.append(
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": f"entry-{i} {padding}"},
                        ],
                    }
                }
            )
        )
    transcript.write_text("\n".join(lines), encoding="utf-8")

    # Concurrent ticker: records the timestamp of each tick. If the
    # event loop is blocked inside the hook, ticks will cluster
    # around the block-end rather than spacing evenly at ~50 ms.
    ticks: list[float] = []
    ticker_stop = asyncio.Event()

    async def _ticker() -> None:
        while not ticker_stop.is_set():
            ticks.append(asyncio.get_event_loop().time())
            try:
                await asyncio.wait_for(ticker_stop.wait(), timeout=0.05)
            except TimeoutError:
                continue

    ticker_task = asyncio.create_task(_ticker())
    try:
        await stop_cb(
            {
                "agent_id": "agent-big-1",
                "agent_transcript_path": str(transcript),
                "session_id": "s-child",
                # Empty → forces the transcript fallback path (the
                # code under test).
                "last_assistant_message": "",
            },
            None,
            None,
        )
    finally:
        ticker_stop.set()
        await ticker_task
    await _drain(pending)

    # Assertion: no gap between consecutive ticks exceeds 500 ms
    # (pitfall #12 threshold). The 250 ms retry-sleep inside the hook
    # yields to the loop, and `asyncio.to_thread` off-loads the read,
    # so ticks should stay spaced around 50 ms + scheduler jitter.
    gaps = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
    assert gaps, "ticker never ran"
    max_gap = max(gaps)
    assert max_gap < 0.5, (
        f"event loop blocked for {max_gap:.3f}s inside subagent_stop hook; "
        "large transcript read must be off-loaded to asyncio.to_thread "
        "(pitfall #12)."
    )

    # Secondary: the hook still produced the correct result.
    job = await store.get_by_agent_id("agent-big-1")
    assert job is not None
    # The last entry's text (entry-1499) should be captured.
    assert job.result_summary is not None
    assert "entry-1499" in job.result_summary
    await store._conn.close()


async def test_stop_hook_fallback_jsonl_reads_last_assistant_text(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-fb-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )

    # Build a JSONL transcript with 2 assistant entries — last wins.
    transcript = tmp_path / "child.jsonl"
    entries = [
        {"message": {"role": "user", "content": [{"type": "text", "text": "q"}]}},
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "first draft"}],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "FINAL ANSWER"}],
            }
        },
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")

    # Empty last_assistant_message → fallback path.
    await stop_cb(
        {
            "agent_id": "agent-fb-1",
            "agent_transcript_path": str(transcript),
            "session_id": "s-child",
            "last_assistant_message": "",
        },
        None,
        None,
    )
    await _drain(pending)

    job = await store.get_by_agent_id("agent-fb-1")
    assert job is not None
    assert job.result_summary == "FINAL ANSWER"
    assert len(adapter.sent) == 1
    assert "FINAL ANSWER" in adapter.sent[0][1]
    await store._conn.close()


async def test_stop_hook_empty_body_notifies_placeholder(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    settings = _settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-empty-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )

    # Missing transcript and empty last_assistant_message — notify
    # placeholder so the operator still learns the subagent finished.
    await stop_cb(
        {
            "agent_id": "agent-empty-1",
            "agent_transcript_path": None,
            "session_id": "s",
            "last_assistant_message": "",
        },
        None,
        None,
    )
    await _drain(pending)

    assert len(adapter.sent) == 1
    assert "subagent produced no final message" in adapter.sent[0][1]
    await store._conn.close()


# ----------------------------------------------------------------- cancel gate


async def test_pretool_cancel_gate_denies_when_flag_set(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=_settings(tmp_path),
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    gate_cb = hooks["PreToolUse"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-g-1", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )
    job = await store.get_by_agent_id("agent-g-1")
    assert job is not None
    await store.set_cancel_requested(job.id)

    out = await gate_cb(
        {"agent_id": "agent-g-1", "tool_name": "Bash"},
        None,
        None,
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "subagent cancelled by owner",
        }
    }
    await store._conn.close()


async def test_pretool_cancel_gate_allows_when_flag_clear(tmp_path: Path) -> None:
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=_settings(tmp_path),
        pending_updates=pending,
    )
    start_cb = hooks["SubagentStart"][0].hooks[0]
    gate_cb = hooks["PreToolUse"][0].hooks[0]

    await start_cb(
        {"agent_id": "agent-g-2", "agent_type": "general", "session_id": "p"},
        None,
        None,
    )
    out = await gate_cb(
        {"agent_id": "agent-g-2", "tool_name": "Bash"},
        None,
        None,
    )
    assert out == {}
    await store._conn.close()


async def test_pretool_cancel_gate_noop_without_agent_id(tmp_path: Path) -> None:
    """Main-turn tool calls have no `agent_id` on input_data — the gate
    must be a no-op so the parent's phase-3 hooks are the sole authority."""
    store = await _mkstore(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[object]] = set()
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=_settings(tmp_path),
        pending_updates=pending,
    )
    gate_cb = hooks["PreToolUse"][0].hooks[0]
    out = await gate_cb({"tool_name": "Bash"}, None, None)
    assert out == {}
    await store._conn.close()
