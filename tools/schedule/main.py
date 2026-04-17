"""schedule CLI — 0xone-assistant scheduler management (phase 5).

Stdlib-only (argparse + sqlite3 + zoneinfo). All subcommands print one
JSON line on stdout; errors go to stderr as `{"ok": false, "error": ...}`.

Exit codes:
  0  ok
  2  usage (argparse)
  3  validation / cap-reached
  4  I/O (DB path missing, permissions)
  7  not-found (schedule ID)

Import discipline: the cron parser is the single source of truth in
`src/assistant/scheduler/cron.py`. We append `<project_root>/src` to
`sys.path` (NOT insert at index 0 — phase-4 lesson) and import from
the `assistant.scheduler.cron` module. Full `_memlib`-style
consolidation is deferred to phase 6 (detailed-plan §13 item 4).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---- sys.path shim ---------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
# Wave-2 G-W2-9: .append, NOT .insert(0), to avoid shadowing (phase-4
# memory CLI uses the same pattern). Deferred consolidation → phase 6.
if str(_ROOT / "src") not in sys.path:
    sys.path.append(str(_ROOT / "src"))

from assistant.scheduler.cron import CronParseError, parse_cron  # noqa: E402

# ---- constants -------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VAL = 3
EXIT_IO = 4
EXIT_NOT_FOUND = 7

_DEFAULT_MAX = 64
_MAX_PROMPT_BYTES = 2048

# Wave-2 B-W2-4: `_TZ_RE` REMOVED. `ZoneInfo(name)` is the sole authority
# (spike S-10: ZoneInfoNotFoundError for unknown names, ValueError for
# injection / empty string). Both catches are required.


# ---- helpers ---------------------------------------------------------------


def _data_dir() -> Path:
    """Same logic as `assistant.config._default_data_dir`; kept here so the
    CLI stays stdlib-only and does NOT instantiate the full Settings model
    (which would fail on missing TELEGRAM_BOT_TOKEN env var)."""
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _db_path() -> Path:
    return _data_dir() / "assistant.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        # A freshly-installed host has no DB yet — we could initialise it
        # but the CLI shouldn't silently create schema; user should run the
        # daemon first. Hard EXIT_IO with a clear error keeps the contract
        # simple.
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
    return dict(zip(cols, row, strict=True))


# ---- validation ------------------------------------------------------------


def _validate_prompt(prompt: str) -> str | None:
    if not prompt.strip():
        return "prompt must be non-empty"
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return f"prompt exceeds {_MAX_PROMPT_BYTES} bytes"
    for ch in prompt:
        if ord(ch) < 0x20 and ch not in "\t\n":
            return f"control char U+{ord(ch):04X} not allowed"
    return None


# ---- subcommands -----------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    # 1. cron.
    try:
        parse_cron(args.cron)
    except CronParseError as exc:
        return _fail(EXIT_VAL, f"cron parse: {exc}")

    # 2. tz — stdlib is sole authority (wave-2 B-W2-4, spike S-10).
    try:
        ZoneInfo(args.tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return _fail(EXIT_VAL, f"unknown tz: {args.tz!r} ({exc})")

    # 3. prompt.
    prompt_err = _validate_prompt(args.prompt)
    if prompt_err is not None:
        return _fail(EXIT_VAL, prompt_err)

    # 4. cap check (GAP #11).
    cap_env = os.environ.get("SCHEDULER_MAX_SCHEDULES") or str(_DEFAULT_MAX)
    try:
        cap = int(cap_env)
    except ValueError:
        cap = _DEFAULT_MAX

    conn = _connect()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled=1")
        (n,) = cur.fetchone()
        if n >= cap:
            return _fail(EXIT_VAL, "scheduler_schedule_cap_reached", cap=cap)
        cur = conn.execute(
            "INSERT INTO schedules(cron, prompt, tz, enabled) VALUES (?, ?, ?, 1)",
            (args.cron, args.prompt, args.tz),
        )
        conn.commit()
        return _ok(
            {
                "id": cur.lastrowid,
                "cron": args.cron,
                "prompt": args.prompt,
                "tz": args.tz,
            }
        )
    finally:
        conn.close()


def cmd_list(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        sql = (
            "SELECT id, cron, prompt, tz, enabled, created_at, last_fire_at "
            "FROM schedules"
        )
        if args.enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id ASC"
        cur = conn.execute(sql)
        rows = cur.fetchall()
        data = [
            {
                "id": r[0],
                "cron": r[1],
                "prompt": r[2],
                "tz": r[3],
                "enabled": bool(r[4]),
                "created_at": r[5],
                "last_fire_at": r[6],
            }
            for r in rows
        ]
        return _ok(data)
    finally:
        conn.close()


def cmd_rm(args: argparse.Namespace) -> int:
    """Soft-delete via enabled=0. Hard delete would drop trigger history,
    which phase-7 observability will want to keep around."""
    conn = _connect()
    try:
        cur = conn.execute("SELECT id FROM schedules WHERE id=?", (args.id,))
        if cur.fetchone() is None:
            return _fail(EXIT_NOT_FOUND, f"schedule {args.id} not found")
        conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (args.id,))
        conn.commit()
        return _ok({"id": args.id, "deleted": True})
    finally:
        conn.close()


def cmd_enable(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute("SELECT id FROM schedules WHERE id=?", (args.id,))
        if cur.fetchone() is None:
            return _fail(EXIT_NOT_FOUND, f"schedule {args.id} not found")
        conn.execute("UPDATE schedules SET enabled=1 WHERE id=?", (args.id,))
        conn.commit()
        return _ok({"id": args.id, "enabled": True})
    finally:
        conn.close()


def cmd_disable(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        cur = conn.execute("SELECT id FROM schedules WHERE id=?", (args.id,))
        if cur.fetchone() is None:
            return _fail(EXIT_NOT_FOUND, f"schedule {args.id} not found")
        conn.execute("UPDATE schedules SET enabled=0 WHERE id=?", (args.id,))
        conn.commit()
        return _ok({"id": args.id, "enabled": False})
    finally:
        conn.close()


def cmd_history(args: argparse.Namespace) -> int:
    conn = _connect()
    try:
        limit = args.limit if args.limit is not None else 20
        if args.schedule_id is not None:
            cur = conn.execute(
                "SELECT id, schedule_id, prompt, scheduled_for, status, attempts, "
                "last_error, created_at, sent_at, acked_at "
                "FROM triggers WHERE schedule_id=? ORDER BY id DESC LIMIT ?",
                (args.schedule_id, limit),
            )
        else:
            cur = conn.execute(
                "SELECT id, schedule_id, prompt, scheduled_for, status, attempts, "
                "last_error, created_at, sent_at, acked_at "
                "FROM triggers ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = cur.fetchall()
        data = [_row_to_dict(cur, r) for r in rows]
        return _ok(data)
    finally:
        conn.close()


# ---- argparse wiring -------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="schedule",
        description="Manage 0xone-assistant scheduled triggers (5-field POSIX cron).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Add a new schedule")
    p_add.add_argument("--cron", required=True, help="5-field POSIX cron expression")
    p_add.add_argument("--prompt", required=True, help="Prompt to run (max 2048 bytes)")
    p_add.add_argument(
        "--tz",
        default="UTC",
        help="IANA tz name (default UTC). Validated by zoneinfo.",
    )
    p_add.set_defaults(func=cmd_add)

    # list
    p_list = sub.add_parser("list", help="List schedules")
    p_list.add_argument(
        "--enabled-only",
        action="store_true",
        help="Show only enabled schedules",
    )
    p_list.set_defaults(func=cmd_list)

    # rm / enable / disable — share the positional-ID shape
    for name, func, help_ in (
        ("rm", cmd_rm, "Soft-delete a schedule (set enabled=0)"),
        ("enable", cmd_enable, "Enable a schedule"),
        ("disable", cmd_disable, "Disable a schedule"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("id", type=int)
        sp.set_defaults(func=func)

    # history
    p_hist = sub.add_parser("history", help="Show recent trigger rows")
    p_hist.add_argument("--schedule-id", type=int, default=None)
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
