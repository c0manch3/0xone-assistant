"""task CLI — 0xone-assistant background subagent management (phase 6).

Stdlib-only (argparse + sqlite3 + time). All subcommands print one JSON
line on stdout; errors go to stderr as `{"ok": false, "error": ...}`.

Exit codes:
  0  ok / subagent completed successfully (`wait`)
  2  usage (argparse)
  3  validation / cap-reached
  4  I/O (DB path missing, permissions)
  5  `wait` terminated with a non-'completed' status
  6  `wait` timeout
  7  not-found (job ID)

Import discipline mirrors `tools/schedule/main.py`: we `sys.path.append`
(NOT `insert(0)`) the project `src/` dir and import from the
`assistant.*` package where that is useful. The CLI, however, avoids
instantiating the full `Settings` model because that depends on
`TELEGRAM_BOT_TOKEN` (not set on an operator's shell); we talk to
SQLite directly and resolve `OWNER_CHAT_ID` / `ASSISTANT_DATA_DIR`
from env-vars the same way the schedule CLI does.

Wave-2: `spawn` writes a row with `status='requested'` and
`sdk_agent_id IS NULL` (partial UNIQUE tolerates). The picker
(`SubagentRequestPicker`) running inside the Daemon consumes these
rows and dispatches them through the dedicated picker bridge; the
Start hook patches `sdk_agent_id` via the CURRENT_REQUEST_ID
ContextVar.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# ---- sys.path shim (phase-4 `.append`, not `.insert(0)`) -------------------

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.append(str(_ROOT / "src"))

# ---- constants -------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VAL = 3
EXIT_IO = 4
EXIT_WAIT_NON_COMPLETED = 5
EXIT_WAIT_TIMEOUT = 6
EXIT_NOT_FOUND = 7

_ALLOWED_KINDS = frozenset({"general", "worker", "researcher"})
_MAX_TASK_BYTES = 4096
_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100
_DEFAULT_WAIT_TIMEOUT_S = 60
_MIN_WAIT_TIMEOUT_S = 1
_MAX_WAIT_TIMEOUT_S = 600
_TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stopped", "interrupted", "error", "dropped"}
)


# ---- helpers ---------------------------------------------------------------


def _data_dir() -> Path:
    """Same logic as `assistant.config._default_data_dir`; avoids pulling in
    pydantic-settings (which requires env vars the CLI shouldn't need)."""
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _db_path() -> Path:
    return _data_dir() / "assistant.db"


def _owner_chat_id() -> int | None:
    """Read OWNER_CHAT_ID from env; returns None if unset. `spawn` uses this
    as the default for `--callback-chat-id` so operators don't have to
    look up the value before every CLI run."""
    raw = os.environ.get("OWNER_CHAT_ID") or os.environ.get("ASSISTANT_OWNER_CHAT_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "assistant database not found",
                    "db_path": str(path),
                    "hint": "start the daemon once so it can apply the schema",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(EXIT_IO)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ok(data: Any) -> int:
    sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    sys.stdout.write("\n")
    return EXIT_OK


def _fail(code: int, error: str, **extra: Any) -> int:
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False))
    sys.stderr.write("\n")
    return code


def _row_to_dict(cur: sqlite3.Cursor, row: tuple[Any, ...]) -> dict[str, Any]:
    cols = [d[0] for d in cur.description]
    out: dict[str, Any] = dict(zip(cols, row, strict=True))
    # Cast SQLite-INTEGER booleans to Python bool.
    if "cancel_requested" in out:
        out["cancel_requested"] = bool(out["cancel_requested"])
    return out


_SELECT_COLS = (
    "id, sdk_agent_id, sdk_session_id, parent_session_id, agent_type, "
    "task_text, transcript_path, status, cancel_requested, result_summary, "
    "cost_usd, callback_chat_id, spawned_by_kind, spawned_by_ref, depth, "
    "created_at, started_at, finished_at"
)


# ---- validation ------------------------------------------------------------


def _validate_task(task: str) -> str | None:
    if not task.strip():
        return "task must be non-empty"
    if len(task.encode("utf-8")) > _MAX_TASK_BYTES:
        return f"task exceeds {_MAX_TASK_BYTES} bytes"
    for ch in task:
        if ord(ch) < 0x20 and ch not in "\t\n":
            return f"control char U+{ord(ch):04X} not allowed"
    return None


# ---- subcommands -----------------------------------------------------------


def cmd_spawn(args: argparse.Namespace) -> int:
    if args.kind not in _ALLOWED_KINDS:
        return _fail(
            EXIT_VAL,
            f"kind {args.kind!r} not allowed",
            allowed=sorted(_ALLOWED_KINDS),
        )

    task_err = _validate_task(args.task)
    if task_err is not None:
        return _fail(EXIT_VAL, task_err)

    callback_chat_id = args.callback_chat_id
    if callback_chat_id is None:
        callback_chat_id = _owner_chat_id()
        if callback_chat_id is None:
            return _fail(
                EXIT_VAL,
                "callback-chat-id not provided and OWNER_CHAT_ID env is unset",
            )

    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO subagent_jobs("
            "agent_type, task_text, status, callback_chat_id, spawned_by_kind) "
            "VALUES (?, ?, 'requested', ?, 'cli')",
            (args.kind, args.task, callback_chat_id),
        )
        conn.commit()
        return _ok({"job_id": cur.lastrowid, "status": "requested"})
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if args.status is not None:
            clauses.append("status=?")
            params.append(args.status)
        if args.kind is not None:
            clauses.append("agent_type=?")
            params.append(args.kind)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        limit = args.limit if args.limit is not None else _DEFAULT_LIST_LIMIT
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            return _fail(EXIT_VAL, f"limit must be 1..{_MAX_LIST_LIMIT}")
        params.append(limit)
        sql = f"SELECT {_SELECT_COLS} FROM subagent_jobs {where}ORDER BY id DESC LIMIT ?"
        cur = conn.execute(sql, tuple(params))
        rows = cur.fetchall()
        data = [_row_to_dict(cur, r) for r in rows]
        return _ok(data)
    finally:
        conn.close()


def cmd_status(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            f"SELECT {_SELECT_COLS} FROM subagent_jobs WHERE id=?",
            (args.id,),
        )
        row = cur.fetchone()
        if row is None:
            return _fail(EXIT_NOT_FOUND, f"job {args.id} not found")
        return _ok(_row_to_dict(cur, row))
    finally:
        conn.close()


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel a subagent job.

    Fix-pack HIGH #3 (CR I-4): delegates the SQL to the shared
    `assistant.subagent.store.cancel_sync` helper so the CLI and
    the async `SubagentStore.set_cancel_requested` path cannot
    drift. The helper wraps the SELECT+UPDATE in `BEGIN IMMEDIATE`
    — the pre-fix CLI ran the two statements without a transaction,
    letting a concurrent daemon writer slip between them and
    produce an inconsistent `previous_status` report.

    CLI-specific wrapping: we first check for a missing row and
    return `EXIT_NOT_FOUND` (7) so the operator sees the distinct
    exit code rather than the shared helper's
    `{"already_terminal": "missing"}`.
    """
    from assistant.subagent.store import cancel_sync

    conn = _connect()
    try:
        # Distinct "not found" exit code: probe first so we can
        # surface EXIT_NOT_FOUND before the cancel_sync helper maps
        # the missing row to `already_terminal=missing`.
        probe = conn.execute(
            "SELECT 1 FROM subagent_jobs WHERE id=?",
            (args.id,),
        )
        if probe.fetchone() is None:
            return _fail(EXIT_NOT_FOUND, f"job {args.id} not found")
        result = cancel_sync(conn, args.id)
        return _ok(result)
    finally:
        conn.close()


def cmd_wait(args: argparse.Namespace) -> int:
    """Poll the ledger until the job lands in a terminal status.

    Exit 0 on `completed`, 5 on any other terminal status, 6 on timeout.
    Prints the final row as JSON regardless of exit code so the operator
    can inspect `result_summary` / `status` without a second `status`
    call.
    """
    timeout_s = args.timeout_s if args.timeout_s is not None else _DEFAULT_WAIT_TIMEOUT_S
    if timeout_s < _MIN_WAIT_TIMEOUT_S or timeout_s > _MAX_WAIT_TIMEOUT_S:
        return _fail(
            EXIT_VAL,
            f"timeout-s must be {_MIN_WAIT_TIMEOUT_S}..{_MAX_WAIT_TIMEOUT_S}",
        )

    conn = _connect()
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            cur = conn.execute(
                f"SELECT {_SELECT_COLS} FROM subagent_jobs WHERE id=?",
                (args.id,),
            )
            row = cur.fetchone()
            if row is None:
                return _fail(EXIT_NOT_FOUND, f"job {args.id} not found")
            data = _row_to_dict(cur, row)
            status = str(data.get("status") or "")
            if status in _TERMINAL_STATUSES:
                _ok(data)
                return EXIT_OK if status == "completed" else EXIT_WAIT_NON_COMPLETED
            if time.monotonic() >= deadline:
                _ok(data)
                return EXIT_WAIT_TIMEOUT
            time.sleep(0.5)
    finally:
        conn.close()


# ---- argparse wiring -------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="task",
        description=(
            "Manage 0xone-assistant background subagent jobs (SDK-native Task delegation)."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # spawn
    p_spawn = sub.add_parser("spawn", help="Queue a new subagent request")
    p_spawn.add_argument(
        "--kind",
        required=True,
        choices=sorted(_ALLOWED_KINDS),
        help="Subagent kind (general | worker | researcher)",
    )
    p_spawn.add_argument(
        "--task",
        required=True,
        help=f"Task text for the subagent (max {_MAX_TASK_BYTES} bytes)",
    )
    p_spawn.add_argument(
        "--callback-chat-id",
        type=int,
        default=None,
        help="Target chat_id (default: OWNER_CHAT_ID env)",
    )
    p_spawn.set_defaults(func=cmd_spawn)

    # list
    p_list = sub.add_parser("list", help="List subagent jobs")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser("status", help="Show one job by id")
    p_status.add_argument("id", type=int)
    p_status.set_defaults(func=cmd_status)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Request cancellation of a job")
    p_cancel.add_argument("id", type=int)
    p_cancel.set_defaults(func=cmd_cancel)

    # wait
    p_wait = sub.add_parser("wait", help="Block until a job reaches terminal status")
    p_wait.add_argument("id", type=int)
    p_wait.add_argument("--timeout-s", type=int, default=None)
    p_wait.set_defaults(func=cmd_wait)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
