"""Phase 7 regression — `task spawn --kind worker` for a media producer.

Phase-6 shipped the CLI spawn → picker → Start-hook lifecycle. Phase 7
keeps that contract intact but rewires the `SubagentStop` hook to run
through `dispatch_reply` (commit 15, H-11). This regression asserts
the media-worker flow specifically:

  1. CLI `spawn --kind worker --task "render <out>.png"` inserts a
     `requested` row with `spawned_by_kind='cli'` (phase-6 contract).
  2. The picker surfaces the row via `list_pending_requests` — the
     phase-6 surface area remains identical even though the worker
     will eventually drop an outbox-path in its final text.
  3. When the Stop hook fires with a body that includes an outbox
     artefact path, `dispatch_reply` routes the artefact through
     `send_photo` (phase-7 switch) — NOT raw `send_text` (the
     phase-6 behaviour).

The test intentionally drives each piece in isolation rather than
running the full SDK; the "worker" here is an abstract row. The
lifecycle contract is what's being defended against regression.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.adapters.dispatch_reply import _DedupLedger
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
from assistant.subagent.hooks import make_subagent_hooks
from assistant.subagent.store import SubagentStore

_TASK_CLI = Path(__file__).resolve().parents[1] / "tools" / "task" / "main.py"


class _RecordingAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.photos: list[tuple[int, Path]] = []
        self.documents: list[tuple[int, Path]] = []
        self.audios: list[tuple[int, Path]] = []

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


async def _init_db(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = await connect(data_dir / "assistant.db")
    await apply_schema(conn)
    await conn.close()


def _run_spawn(
    data_dir: Path,
    *extra: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ASSISTANT_DATA_DIR": str(data_dir)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_TASK_CLI), "spawn", *extra],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


async def test_spawn_worker_for_media_task_inserts_requested_row(
    tmp_path: Path,
) -> None:
    """CLI `spawn --kind worker --task "render photo"` writes one row.

    Contract (phase-6 + phase-7 stable):
      * `status == 'requested'`
      * `sdk_agent_id IS NULL` (the Start hook patches it later)
      * `spawned_by_kind == 'cli'`
      * `agent_type == 'worker'`
      * `callback_chat_id == OWNER_CHAT_ID` env default
      * The task text is preserved verbatim (no truncation at 4 KB).
    """
    data_dir = tmp_path / "data"
    await _init_db(data_dir)

    task_text = (
        "генерируй картинку 'закат над морем' через tools/genimage/main.py "
        f"--out {data_dir / 'media' / 'outbox' / 'uuid.png'}"
    )

    proc = _run_spawn(
        data_dir,
        "--kind",
        "worker",
        "--task",
        task_text,
        env_extra={"OWNER_CHAT_ID": "42"},
    )
    assert proc.returncode == 0, (
        f"spawn exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True
    # `_ok(dict)` wraps the inner payload under `"data"`.
    inner = payload["data"]
    assert inner["status"] == "requested"
    job_id = inner["job_id"]
    assert isinstance(job_id, int) and job_id >= 1

    # Verify the row shape via the real SubagentStore — this is what
    # the picker reads from.
    conn = await connect(data_dir / "assistant.db")
    try:
        store = SubagentStore(conn, lock=asyncio.Lock())
        pending = await store.list_pending_requests(limit=10)
        assert len(pending) == 1
        row = pending[0]
        assert row.id == job_id
        assert row.agent_type == "worker"
        assert row.status == "requested"
        assert row.sdk_agent_id is None
        assert row.spawned_by_kind == "cli"
        assert row.callback_chat_id == 42
        assert row.task_text == task_text
    finally:
        await conn.close()


async def test_worker_stop_hook_routes_artefact_through_dispatch_reply(
    tmp_path: Path,
) -> None:
    """Phase-7 swap validated end-to-end on the worker path.

    Pre-condition (phase-6): the picker consumed the `requested` row,
    the Start hook promoted it to `started` and patched `sdk_agent_id`.
    Here we simulate the worker's final text containing an outbox
    path; the Stop hook body must route via `dispatch_reply` (phase-7
    switch) rather than raw `send_text` (the phase-6 behaviour). The
    send_photo call is the observable difference between the two.
    """
    settings = _settings(tmp_path)
    outbox = outbox_dir(settings.data_dir)
    outbox.mkdir(parents=True)
    photo = outbox / "worker.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")

    conn = await connect(tmp_path / "worker.db")
    await apply_schema(conn)
    store = SubagentStore(conn, lock=asyncio.Lock())

    # Seed a row in the same shape the picker + Start-hook produce.
    job_id = await store.record_started(
        sdk_agent_id="agent-worker-1",
        agent_type="worker",
        parent_session_id=None,
        callback_chat_id=settings.owner_chat_id,
        spawned_by_kind="cli",
        spawned_by_ref=None,
    )
    assert isinstance(job_id, int)

    adapter = _RecordingAdapter()
    ledger = _DedupLedger()
    pending: set[asyncio.Task[Any]] = set()

    hooks = make_subagent_hooks(
        store=store,
        adapter=adapter,
        settings=settings,
        pending_updates=pending,
        dedup_ledger=ledger,
    )
    stop_cb = hooks["SubagentStop"][0].hooks[0]

    # Worker's final text references the produced artefact. In
    # production this is the text the model emits before the Stop hook
    # fires — the whole point of the phase-7 switch is for this path
    # to reach the owner as a proper Telegram photo.
    body_text = f"готово: {photo}"

    await stop_cb(
        {
            "agent_id": "agent-worker-1",
            "agent_transcript_path": None,
            "session_id": "s",
            "last_assistant_message": body_text,
        },
        None,
        None,
    )
    # Drain the shielded _deliver task.
    await asyncio.gather(*list(pending), return_exceptions=True)

    # Phase-7: the photo went out through send_photo + the cleaned
    # text through send_text. Phase-6 would have dumped the raw path
    # via send_text only (no send_photo call). That asymmetry is the
    # regression guard.
    assert adapter.photos == [(settings.owner_chat_id, photo.resolve())]
    assert len(adapter.texts) == 1
    sent_chat, sent_text = adapter.texts[0]
    assert sent_chat == settings.owner_chat_id
    # Cleaned text: the raw path is stripped; surrounding text
    # (including the "готово:" prefix) survives.
    assert str(photo) not in sent_text
    assert "готово" in sent_text

    await conn.close()
