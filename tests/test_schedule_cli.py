"""Phase 5 / commit 6 — tools/schedule/main.py CLI coverage.

Subprocess-based: the CLI is stdlib-only and must stay importable without
side-effects. Each test runs it against a fresh tmp `ASSISTANT_DATA_DIR`,
pre-creating `assistant.db` via the aiosqlite migration path so subcommands
that SELECT actually find a DB.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assistant.state.db import apply_schema, connect

_CLI = Path(__file__).resolve().parents[1] / "tools" / "schedule" / "main.py"


async def _init_db(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = await connect(data_dir / "assistant.db")
    await apply_schema(conn)
    await conn.close()


def _run(
    data_dir: Path, *args: str, env_extra: dict[str, str] | None = None
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
        timeout=20,
    )


# ---------------------------------------------------------------- add


def test_add_happy_path(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "add",
        "--cron",
        "0 9 * * *",
        "--prompt",
        "ping",
        "--tz",
        "UTC",
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["data"]["cron"] == "0 9 * * *"
    assert payload["data"]["id"] == 1


def test_add_defaults_tz_to_utc(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "*/15 * * * *", "--prompt", "x")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["data"]["tz"] == "UTC"


def test_add_rejects_bad_cron(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "not a cron", "--prompt", "x")
    assert r.returncode == 3, r.stderr
    err = json.loads(r.stderr)
    assert err["ok"] is False
    assert "cron parse" in err["error"]


def test_add_accepts_etc_gmt_plus(tmp_path: Path) -> None:
    """Wave-2 B-W2-4: `Etc/GMT+3` is a legitimate IANA name (POSIX sign
    inversion). The dropped `_TZ_RE` would have rejected it."""
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "x", "--tz", "Etc/GMT+3")
    assert r.returncode == 0, r.stderr


def test_add_rejects_injection_tz(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "x", "--tz", "../../etc/passwd")
    assert r.returncode == 3, r.stderr
    err = json.loads(r.stderr)
    assert "tz" in err["error"]


def test_add_rejects_fake_tz(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "x", "--tz", "Europe/NotACity")
    assert r.returncode == 3, r.stderr


def test_add_rejects_empty_prompt(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "   ")
    assert r.returncode == 3, r.stderr


def test_add_rejects_oversized_prompt(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "x" * 2049)
    assert r.returncode == 3, r.stderr


def test_add_rejects_control_char_prompt(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "bad\x01chars")
    assert r.returncode == 3, r.stderr


def test_add_cap_reached(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(
        tmp_path,
        "add",
        "--cron",
        "0 9 * * *",
        "--prompt",
        "x",
        env_extra={"SCHEDULER_MAX_SCHEDULES": "2"},
    )
    assert r.returncode == 0
    r = _run(
        tmp_path,
        "add",
        "--cron",
        "0 10 * * *",
        "--prompt",
        "y",
        env_extra={"SCHEDULER_MAX_SCHEDULES": "2"},
    )
    assert r.returncode == 0
    # Third add — cap reached.
    r = _run(
        tmp_path,
        "add",
        "--cron",
        "0 11 * * *",
        "--prompt",
        "z",
        env_extra={"SCHEDULER_MAX_SCHEDULES": "2"},
    )
    assert r.returncode == 3
    err = json.loads(r.stderr)
    assert err["error"] == "scheduler_schedule_cap_reached"
    assert err["cap"] == 2


# ---------------------------------------------------------------- list + lifecycle


def test_list_and_lifecycle(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    _run(tmp_path, "add", "--cron", "0 9 * * *", "--prompt", "x")
    _run(tmp_path, "add", "--cron", "0 10 * * *", "--prompt", "y")
    r = _run(tmp_path, "list")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)["data"]
    assert len(data) == 2

    # Disable #1
    r = _run(tmp_path, "disable", "1")
    assert r.returncode == 0

    r = _run(tmp_path, "list", "--enabled-only")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 1
    assert data[0]["id"] == 2

    # Re-enable
    r = _run(tmp_path, "enable", "1")
    assert r.returncode == 0
    r = _run(tmp_path, "list", "--enabled-only")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 2

    # rm
    r = _run(tmp_path, "rm", "1")
    assert r.returncode == 0
    r = _run(tmp_path, "list", "--enabled-only")
    data = json.loads(r.stdout)["data"]
    assert len(data) == 1


def test_rm_not_found(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "rm", "999")
    assert r.returncode == 7, r.stderr


def test_enable_not_found(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "enable", "999")
    assert r.returncode == 7


def test_history_empty(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "history")
    assert r.returncode == 0
    assert json.loads(r.stdout)["data"] == []


def test_db_not_found_exit_4(tmp_path: Path) -> None:
    """No `_init_db` call — DB doesn't exist."""
    r = _run(tmp_path, "list")
    assert r.returncode == 4, r.stderr


# ---------------------------------------------------------------- argparse


def test_missing_subcommand_exit_2(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path)
    assert r.returncode == 2


def test_add_missing_required_flag_exit_2(tmp_path: Path) -> None:
    asyncio.run(_init_db(tmp_path))
    r = _run(tmp_path, "add", "--cron", "0 9 * * *")
    assert r.returncode == 2


_ = pytest
