"""Phase 6 / commit 5 — tools/task/main.py CLI coverage.

Subprocess-based: the CLI is stdlib-only. Each test runs it against a
fresh `ASSISTANT_DATA_DIR`, with the schema pre-applied via aiosqlite.

Covers:
  * `spawn` happy path + validation (kind enum, missing flags, size cap).
  * `list` (filters, limit bounds).
  * `status` (happy + not-found).
  * `cancel` (requested + started + already-terminal).
  * `wait` (timeout, completed, non-completed terminal).
  * `OWNER_CHAT_ID` env defaulting on `--callback-chat-id` omission.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from assistant.state.db import apply_schema, connect

_CLI = Path(__file__).resolve().parents[1] / "tools" / "task" / "main.py"


async def _init_db(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = await connect(data_dir / "assistant.db")
    await apply_schema(conn)
    await conn.close()


def _run(
    data_dir: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "ASSISTANT_DATA_DIR": str(data_dir)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _insert_job(
    data_dir: Path,
    *,
    agent_type: str = "general",
    task_text: str = "hello",
    status: str = "requested",
    sdk_agent_id: str | None = None,
    spawned_by_kind: str = "cli",
    callback_chat_id: int = 42,
) -> int:
    """Direct DB insert helper for setting up non-default states."""
    import sqlite3

    path = data_dir / "assistant.db"
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.execute(
            "INSERT INTO subagent_jobs("
            "agent_type, task_text, status, sdk_agent_id, callback_chat_id, "
            "spawned_by_kind) VALUES (?, ?, ?, ?, ?, ?)",
            (
                agent_type,
                task_text,
                status,
                sdk_agent_id,
                callback_chat_id,
                spawned_by_kind,
            ),
        )
        conn.commit()
        assert cur.lastrowid is not None
        return int(cur.lastrowid)
    finally:
        conn.close()


def _set_status(data_dir: Path, job_id: int, status: str) -> None:
    import sqlite3

    path = data_dir / "assistant.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE subagent_jobs SET status=? WHERE id=?", (status, job_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- spawn


def test_spawn_happy_path_with_explicit_callback(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "spawn",
        "--kind",
        "general",
        "--task",
        "write a poem",
        "--callback-chat-id",
        "42",
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["data"]["job_id"] >= 1
    assert payload["data"]["status"] == "requested"


def test_spawn_defaults_callback_to_owner_chat_id_env(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "spawn",
        "--kind",
        "researcher",
        "--task",
        "look up x",
        env_extra={"OWNER_CHAT_ID": "7777"},
    )
    assert r.returncode == 0, r.stderr
    # Confirm the row landed with the env-sourced chat_id.
    jid = json.loads(r.stdout)["data"]["job_id"]
    r2 = _run(tmp_path, "status", str(jid), env_extra={"OWNER_CHAT_ID": "7777"})
    assert r2.returncode == 0, r2.stderr
    assert json.loads(r2.stdout)["data"]["callback_chat_id"] == 7777


def test_spawn_without_owner_env_and_no_flag_fails(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    env = {k: v for k, v in os.environ.items() if k not in ("OWNER_CHAT_ID",)}
    env["ASSISTANT_DATA_DIR"] = str(tmp_path)
    # Forcibly clear any owner env.
    r = subprocess.run(
        [sys.executable, str(_CLI), "spawn", "--kind", "general", "--task", "x"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert r.returncode == 3
    assert "callback-chat-id" in json.loads(r.stderr)["error"]


def test_spawn_rejects_unknown_kind(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    # argparse choices gate this — exit 2.
    r = _run(tmp_path, "spawn", "--kind", "ninja", "--task", "x", "--callback-chat-id", "42")
    assert r.returncode == 2


def test_spawn_rejects_empty_task(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "spawn",
        "--kind",
        "general",
        "--task",
        "   ",
        "--callback-chat-id",
        "42",
    )
    assert r.returncode == 3


def test_spawn_rejects_oversized_task(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "spawn",
        "--kind",
        "general",
        "--task",
        "x" * 4097,
        "--callback-chat-id",
        "42",
    )
    assert r.returncode == 3


# ---------------------------------------------------------------- list


def test_list_empty(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "list")
    assert r.returncode == 0
    assert json.loads(r.stdout)["data"] == []


def test_list_returns_rows_newest_first(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    _insert_job(tmp_path, task_text="a")
    _insert_job(tmp_path, task_text="b")
    _insert_job(tmp_path, task_text="c")
    r = _run(tmp_path, "list")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 3
    assert data[0]["task_text"] == "c"
    assert data[-1]["task_text"] == "a"


def test_list_status_filter(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid1 = _insert_job(tmp_path, task_text="a", status="started", sdk_agent_id="ag-1")
    _insert_job(tmp_path, task_text="b", status="requested")
    r = _run(tmp_path, "list", "--status", "started")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 1
    assert data[0]["id"] == jid1


def test_list_kind_filter(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    _insert_job(tmp_path, agent_type="general")
    _insert_job(tmp_path, agent_type="researcher")
    r = _run(tmp_path, "list", "--kind", "researcher")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 1
    assert data[0]["agent_type"] == "researcher"


def test_list_limit_respected(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    for i in range(5):
        _insert_job(tmp_path, task_text=f"t{i}")
    r = _run(tmp_path, "list", "--limit", "2")
    assert json.loads(r.stdout)["data"].__len__() == 2


def test_list_rejects_limit_out_of_range(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "list", "--limit", "0")
    assert r.returncode == 3
    r = _run(tmp_path, "list", "--limit", "1000")
    assert r.returncode == 3


# ---------------------------------------------------------------- status


def test_status_not_found(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "status", "999")
    assert r.returncode == 7


def test_status_returns_row(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, task_text="hello")
    r = _run(tmp_path, "status", str(jid))
    assert r.returncode == 0
    row = json.loads(r.stdout)["data"]
    assert row["id"] == jid
    assert row["task_text"] == "hello"
    assert row["status"] == "requested"


# ---------------------------------------------------------------- cancel


def test_cancel_requested_row_sets_flag(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, task_text="x")
    r = _run(tmp_path, "cancel", str(jid))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)["data"]
    assert payload["cancel_requested"] is True
    assert payload["previous_status"] == "requested"
    # Row still requested, but cancel_requested=1.
    s = _run(tmp_path, "status", str(jid))
    assert json.loads(s.stdout)["data"]["cancel_requested"] is True


def test_cancel_already_terminal_is_noop(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, status="completed", sdk_agent_id="ag-done")
    r = _run(tmp_path, "cancel", str(jid))
    assert r.returncode == 0
    payload = json.loads(r.stdout)["data"]
    assert payload["already_terminal"] == "completed"


def test_cancel_not_found(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "cancel", "9999")
    assert r.returncode == 7


# ---------------------------------------------------------------- wait


def test_wait_completed_exit_0(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, status="completed", sdk_agent_id="ag-c")
    r = _run(tmp_path, "wait", str(jid), "--timeout-s", "1")
    assert r.returncode == 0
    assert json.loads(r.stdout)["data"]["status"] == "completed"


def test_wait_non_completed_terminal_exit_5(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, status="failed", sdk_agent_id="ag-f")
    r = _run(tmp_path, "wait", str(jid), "--timeout-s", "1")
    assert r.returncode == 5
    assert json.loads(r.stdout)["data"]["status"] == "failed"


def test_wait_timeout_exit_6(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, status="requested")
    # 1 second is tight, but `_run` uses subprocess timeout 20 s.
    r = _run(tmp_path, "wait", str(jid), "--timeout-s", "1", timeout=10)
    assert r.returncode == 6
    assert json.loads(r.stdout)["data"]["status"] == "requested"


def test_wait_rejects_timeout_out_of_range(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    jid = _insert_job(tmp_path, status="requested")
    r = _run(tmp_path, "wait", str(jid), "--timeout-s", "10000")
    assert r.returncode == 3


def test_wait_not_found(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "wait", "9999", "--timeout-s", "1")
    assert r.returncode == 7


# ---------------------------------------------------------------- argparse / io


def test_missing_subcommand_exit_2(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path)
    assert r.returncode == 2


def test_db_not_found_exit_4(tmp_path: Path) -> None:
    r = _run(tmp_path, "list")
    assert r.returncode == 4
