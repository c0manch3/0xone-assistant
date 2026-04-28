"""Phase 6: SubagentStart / SubagentStop / cancel-gate PreToolUse hooks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from assistant.adapters.base import MessengerAdapter
from assistant.config import Settings
from assistant.state.db import apply_schema, connect
from assistant.subagent.hooks import (
    CURRENT_REQUEST_ID,
    _extract_assistant_text,
    _read_last_assistant_from_transcript,
    make_subagent_hooks,
)
from assistant.subagent.store import SubagentStore


class _FakeAdapter(MessengerAdapter):
    """Minimal adapter capturing send_text calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def start(self) -> None:  # pragma: no cover - unused
        return

    async def stop(self) -> None:  # pragma: no cover - unused
        return

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


async def _settings(tmp_path: Path) -> Settings:
    return cast(
        Settings,
        Settings(
            telegram_bot_token="x" * 50,  # type: ignore[arg-type]
            owner_chat_id=42,  # type: ignore[arg-type]
            project_root=tmp_path,
            data_dir=tmp_path / "data",
        ),
    )


async def _store_and_settings(
    tmp_path: Path,
) -> tuple[SubagentStore, Settings, _FakeAdapter, set[asyncio.Task[Any]]]:
    db = tmp_path / "sub.db"
    conn = await connect(db)
    await apply_schema(conn)
    store = SubagentStore(conn)
    settings = await _settings(tmp_path)
    adapter = _FakeAdapter()
    pending: set[asyncio.Task[Any]] = set()
    return store, settings, adapter, pending


async def test_on_subagent_start_records_native_spawn(tmp_path: Path) -> None:
    """No ContextVar set → on_subagent_start INSERTs a fresh 'started' row."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_start = hooks["SubagentStart"][0].hooks[0]
    payload = {
        "agent_id": "agent-1",
        "agent_type": "general",
        "session_id": "parent-sess",
    }
    out = await on_start(payload, None, None)
    assert out == {}
    job = await store.get_by_agent_id("agent-1")
    assert job is not None
    assert job.status == "started"
    assert job.parent_session_id == "parent-sess"


async def test_on_subagent_start_picker_claims_pending(tmp_path: Path) -> None:
    """ContextVar set → patches the existing 'requested' row."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    job_id = await store.record_pending_request(
        agent_type="general",
        task_text="t",
        callback_chat_id=42,
        spawned_by_kind="tool",
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_start = hooks["SubagentStart"][0].hooks[0]
    token = CURRENT_REQUEST_ID.set(job_id)
    try:
        await on_start(
            {
                "agent_id": "agent-claimed",
                "agent_type": "general",
                "session_id": "parent",
            },
            None,
            None,
        )
    finally:
        CURRENT_REQUEST_ID.reset(token)
    job = await store.get_by_id(job_id)
    assert job is not None
    assert job.status == "started"
    assert job.sdk_agent_id == "agent-claimed"


async def test_on_subagent_start_missing_agent_id_no_op(tmp_path: Path) -> None:
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_start = hooks["SubagentStart"][0].hooks[0]
    out = await on_start({"agent_type": "general"}, None, None)
    assert out == {}
    rows = await store.list_jobs(limit=10)
    assert rows == []


async def test_on_subagent_stop_uses_runtime_field(tmp_path: Path) -> None:
    """Primary path: ``last_assistant_message`` runtime field used directly."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="agent-99",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    out = await on_stop(
        {
            "agent_id": "agent-99",
            "last_assistant_message": "the result",
            "agent_transcript_path": "/nonexistent",
            "session_id": "child-sess",
        },
        None,
        None,
    )
    assert out == {}
    # Drain the shielded delivery task.
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert len(adapter.sent) == 1
    chat_id, body = adapter.sent[0]
    assert chat_id == 42
    assert "the result" in body
    job = await store.get_by_agent_id("agent-99")
    assert job is not None
    assert job.status == "completed"


async def test_on_subagent_stop_falls_back_to_jsonl(tmp_path: Path) -> None:
    """When ``last_assistant_message`` is empty, fallback reads JSONL."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="agent-fb",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    transcript = tmp_path / "agent-fb.jsonl"
    transcript.write_text(
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "from jsonl"}],
            },
        }) + "\n",
        encoding="utf-8",
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await on_stop(
        {
            "agent_id": "agent-fb",
            "agent_transcript_path": str(transcript),
            "last_assistant_message": "",
        },
        None,
        None,
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert any("from jsonl" in body for _, body in adapter.sent)


async def test_on_subagent_start_uses_turn_origin_scheduler(
    tmp_path: Path,
) -> None:
    """Fix-pack F2 (QA HIGH-1): when the originating turn is
    scheduler-origin, the native Task Start hook tags the row
    ``spawned_by_kind='scheduler'`` instead of the static ``'user'``."""
    from assistant.subagent.hooks import CURRENT_TURN_ORIGIN

    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_start = hooks["SubagentStart"][0].hooks[0]
    token = CURRENT_TURN_ORIGIN.set("scheduler")
    try:
        await on_start(
            {
                "agent_id": "agent-sched",
                "agent_type": "general",
                "session_id": "p",
            },
            None,
            None,
        )
    finally:
        CURRENT_TURN_ORIGIN.reset(token)
    job = await store.get_by_agent_id("agent-sched")
    assert job is not None
    assert job.spawned_by_kind == "scheduler"


async def test_on_subagent_start_default_origin_user(tmp_path: Path) -> None:
    """Default ContextVar value 'telegram' → spawned_by_kind='user'
    (the legacy native-Task contract)."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_start = hooks["SubagentStart"][0].hooks[0]
    await on_start(
        {
            "agent_id": "agent-tg",
            "agent_type": "general",
            "session_id": "p",
        },
        None,
        None,
    )
    job = await store.get_by_agent_id("agent-tg")
    assert job is not None
    assert job.spawned_by_kind == "user"


async def test_subagent_spawn_tool_tags_scheduler_origin(
    tmp_path: Path,
) -> None:
    """Fix-pack F2: the @tool surface respects CURRENT_TURN_ORIGIN — a
    scheduler-fired turn that delegates via subagent_spawn produces a
    ``spawned_by_kind='scheduler'`` row."""
    from assistant.subagent.hooks import CURRENT_TURN_ORIGIN
    from assistant.tools_sdk.subagent import (
        configure_subagent,
        subagent_spawn,
    )

    store, settings, _adapter, _pending = await _store_and_settings(tmp_path)
    configure_subagent(
        store=store,
        owner_chat_id=42,
        settings=settings,
    )
    token = CURRENT_TURN_ORIGIN.set("scheduler")
    try:
        res = await subagent_spawn.handler(  # type: ignore[attr-defined]
            {"kind": "general", "task": "t"}
        )
    finally:
        CURRENT_TURN_ORIGIN.reset(token)
    assert res.get("status") == "requested"
    job = await store.get_by_id(int(res["job_id"]))
    assert job is not None
    assert job.spawned_by_kind == "scheduler"


async def test_on_subagent_stop_handles_failed_status(tmp_path: Path) -> None:
    """Fix-pack F3 (QA HIGH-2): SDK emits status='failed' → we record
    terminal 'failed' and persist the SDK error text into last_error."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="agent-fail",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await on_stop(
        {
            "agent_id": "agent-fail",
            "status": "failed",
            "error": "max turns exceeded",
            "last_assistant_message": "partial",
        },
        None,
        None,
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    job = await store.get_by_agent_id("agent-fail")
    assert job is not None
    assert job.status == "failed"
    assert job.last_error == "max turns exceeded"


async def test_on_subagent_stop_handles_alternate_task_status_shape(
    tmp_path: Path,
) -> None:
    """F3 reads both ``status`` and ``task_status`` so different SDK
    bundled CLI shapes are tolerated."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="agent-fail-alt",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await on_stop(
        {
            "agent_id": "agent-fail-alt",
            "task_status": "failed",
            "error_message": "tool deny",
            "last_assistant_message": "p",
        },
        None,
        None,
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    job = await store.get_by_agent_id("agent-fail-alt")
    assert job is not None
    assert job.status == "failed"


async def test_throttle_serializes_concurrent_calls(tmp_path: Path) -> None:
    """Fix-pack F4 (code H2): two SubagentStop hooks for the same chat
    fire back-to-back; the throttle lock must serialise them so the
    second send waits at least ``notify_throttle_ms`` after the first.
    """
    import time as _time

    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    # Bump throttle to a measurable value.
    settings.subagent.notify_throttle_ms = 250
    await store.record_started(
        sdk_agent_id="t-1",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    await store.record_started(
        sdk_agent_id="t-2",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )

    timestamps: list[float] = []

    async def captured_send(chat_id: int, text: str) -> None:
        timestamps.append(_time.monotonic())
        adapter.sent.append((chat_id, text))

    adapter.send_text = captured_send  # type: ignore[method-assign]
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await asyncio.gather(
        on_stop(
            {"agent_id": "t-1", "last_assistant_message": "first"},
            None,
            None,
        ),
        on_stop(
            {"agent_id": "t-2", "last_assistant_message": "second"},
            None,
            None,
        ),
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert len(timestamps) == 2
    delta_ms = (timestamps[1] - timestamps[0]) * 1000.0
    # Allow a small tolerance for asyncio scheduler jitter; the lock
    # guarantees AT LEAST ``notify_throttle_ms - 5ms`` between sends.
    assert delta_ms >= 200.0, (
        f"throttle did not serialise sends: delta_ms={delta_ms:.1f}"
    )


async def test_on_subagent_stop_marks_stopped_when_cancelled(tmp_path: Path) -> None:
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="cancel-ag",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    job = await store.get_by_agent_id("cancel-ag")
    assert job is not None
    await store.set_cancel_requested(job.id)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await on_stop(
        {
            "agent_id": "cancel-ag",
            "last_assistant_message": "stopped midway",
        },
        None,
        None,
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    job = await store.get_by_agent_id("cancel-ag")
    assert job is not None
    assert job.status == "stopped"


async def test_on_subagent_stop_unknown_agent_id_no_send(tmp_path: Path) -> None:
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    await on_stop(
        {"agent_id": "ghost", "last_assistant_message": "x"},
        None,
        None,
    )
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert adapter.sent == []


async def test_on_subagent_stop_returns_immediately(tmp_path: Path) -> None:
    """GAP #12: hook does NOT await delivery — returns {} fast."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="quick",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_stop = hooks["SubagentStop"][0].hooks[0]
    # Use a slow adapter to verify the hook does not wait on it.
    slow_done = asyncio.Event()

    async def slow_send(chat_id: int, text: str) -> None:
        await slow_done.wait()
        adapter.sent.append((chat_id, text))

    adapter.send_text = slow_send  # type: ignore[method-assign]
    out = await asyncio.wait_for(
        on_stop(
            {"agent_id": "quick", "last_assistant_message": "hi"},
            None,
            None,
        ),
        timeout=1.0,
    )
    assert out == {}
    # delivery task is anchored on pending_updates and is still pending.
    assert len(pending) == 1
    slow_done.set()
    await asyncio.gather(*pending, return_exceptions=True)


async def test_on_pretool_cancel_gate_subagent_origin(tmp_path: Path) -> None:
    """When agent_id is set on PreToolUse + cancel_requested, deny."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    await store.record_started(
        sdk_agent_id="cancel-ag2",
        agent_type="general",
        parent_session_id=None,
        callback_chat_id=42,
        spawned_by_kind="user",
        spawned_by_ref=None,
    )
    job = await store.get_by_agent_id("cancel-ag2")
    assert job is not None
    await store.set_cancel_requested(job.id)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_pre = hooks["PreToolUse"][0].hooks[0]
    out = await on_pre(
        {
            "agent_id": "cancel-ag2",
            "tool_name": "Bash",
            "tool_input": {},
            "tool_use_id": "abc",
        },
        None,
        None,
    )
    spec = out.get("hookSpecificOutput", {})
    assert spec.get("permissionDecision") == "deny"
    assert "cancelled" in spec.get("permissionDecisionReason", "")


async def test_on_pretool_cancel_gate_main_turn_passthrough(
    tmp_path: Path,
) -> None:
    """No agent_id (main turn) → no-op even if some other row has cancel=1."""
    store, settings, adapter, pending = await _store_and_settings(tmp_path)
    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
    )
    on_pre = hooks["PreToolUse"][0].hooks[0]
    out = await on_pre(
        {"tool_name": "Bash", "tool_input": {}, "tool_use_id": "x"},
        None,
        None,
    )
    assert out == {}


# ---------- JSONL streaming-read fallback ----------


def test_jsonl_reads_last_assistant_block(tmp_path: Path) -> None:
    p = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "first"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "second"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "third"}]}}),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = _read_last_assistant_from_transcript(p)
    assert out == "third"


def test_jsonl_drops_partial_last_line(tmp_path: Path) -> None:
    """File ending without \\n loses the final (partial) entry."""
    p = tmp_path / "transcript.jsonl"
    line_complete = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "complete"}]},
    })
    # Write a full line + a deliberately partial JSON without trailing newline.
    p.write_text(
        line_complete + "\n" + '{"type": "assist',  # no \n
        encoding="utf-8",
    )
    out = _read_last_assistant_from_transcript(p)
    assert out == "complete"


def test_jsonl_missing_returns_empty(tmp_path: Path) -> None:
    out = _read_last_assistant_from_transcript(tmp_path / "missing.jsonl")
    assert out == ""


def test_jsonl_malformed_lines_skipped(tmp_path: Path) -> None:
    p = tmp_path / "transcript.jsonl"
    valid = json.dumps({
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "hello"}]},
    })
    p.write_text("garbage\n" + valid + "\n", encoding="utf-8")
    assert _read_last_assistant_from_transcript(p) == "hello"


def test_jsonl_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "transcript.jsonl"
    p.write_text("", encoding="utf-8")
    assert _read_last_assistant_from_transcript(p) == ""


def test_jsonl_extract_text_handles_inline_shape(tmp_path: Path) -> None:
    """Tolerate an envelope without nested "message" if it inlines content."""
    line = json.dumps({
        "type": "assistant",
        "content": [{"type": "text", "text": "inline"}],
    })
    out = _extract_assistant_text(line)
    assert out == "inline"


def test_jsonl_extract_text_returns_none_on_user_role(tmp_path: Path) -> None:
    line = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "x"}]},
    })
    assert _extract_assistant_text(line) is None
