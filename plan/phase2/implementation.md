---
version: v2.1 (post-wipe rebuild, fix-pack after devil wave 2)
date: 2026-04-20
supersedes: v2 (2026-04-20, pre-fix-pack) + v2 (2026-04-15, pre-wipe)
baseline: spike-findings.md §R1–R5 (frozen) + §R7–R13 (wave-2 + fix-pack live probes)
sdk: claude-agent-sdk>=0.1.59,<0.2 (tested on 0.1.59; CLI 2.1.114)
auth: OAuth via ~/.claude/ — **no `ANTHROPIC_API_KEY` anywhere**
changelog_v2.1:
  - B1: _apply_0002 early-exits when PRAGMA user_version >= 2 (protects direct test calls)
  - B2: migration rewritten statement-by-statement (executescript + BEGIN EXCLUSIVE is unsafe — implicit COMMIT)
  - B3: main() preserves phase-1 signal-handling supervision (stop_event, polling_exc, SIGTERM/SIGINT)
  - B4: IncomingMessage keeps message_id; MessengerAdapter(ABC) + Handler(Protocol) both retained
  - B5: ToolResultBlock classified as role='user' block_type='tool_result' (SDK contract)
  - B6: load_recent reworked to turn-LIMIT (not row-LIMIT) — prevents partial-turn truncation
  - B7: cat bash-allowlist validates ALL positional args
  - B8: file_hook ALWAYS resolves relative paths against project_root
  - B9: empty file-tool candidate → explicit deny (Read/Write/Edit); path optional for Grep only
  - B10: WebFetch DNS lookup moved to asyncio.to_thread; catches OSError/timeout too
  - S2: model captured from AssistantMessage (not ResultMessage — doesn't exist)
  - S7: history_to_sdk_envelopes replays BOTH user AND assistant envelopes verbatim (R13-verified)
  - S4: .claude/skills symlink target absolute, not relative
  - S10: orphan pending-turn cleanup on daemon bootstrap
  - S15: .claude/settings*.json with hooks/permissions keys blocks startup (sys.exit(3))
  - N2: pydantic validator resolves project_root to absolute
  - N6: .env.example + README note XDG location
---

# Phase 2 — Implementation (spike-verified v2.1, 2026-04-20)

> This document is the authoritative blueprint for phase 2. It folds in
> wave-1 devil's-advocate findings (Q1–Q13) + wave-2 spike results (R7–R12)
> + devil-wave-2 fix-pack corrections + R13 live probe (assistant envelope
> replay).
>
> Coder MUST read `plan/phase2/spike-findings.md §§1, 4, 6` and
> `plan/phase2/unverified-assumptions.md` before touching `src/`.

## Revision history

- **pre-wipe v1** (2026-04-15): initial after SDK spike R1–R5. Lost in wipe.
- **pre-wipe v2** (2026-04-15): devil-wave-1 applied. Lost in wipe.
- **v2** (2026-04-20): post-wipe rebuild. Owner Q&A + wave-2 R7–R12 folded.
- **v2.1** (2026-04-20, this file): fix-pack after devil-wave-2.
  B1–B10, S2, S4, S7, S10, S15, N2, N6 closed. R13 live probe added.

---

## 1. Verified decisions table (phase 2 source of truth)

### 1a. Owner Q&A decisions (wave 1)

| Q | Decision | Rationale |
|---|---|---|
| Q1 | `.env` → `~/.config/0xone-assistant/.env` (fallback `./.env`); `data_dir` → `~/.local/share/0xone-assistant/` | XDG compliance; keeps secrets outside `cwd` SDK |
| Q2 | `mkdir(parents=True, exist_ok=True)` for `data_dir` at daemon start | First-run without user scripting |
| Q3 | `claude-agent-sdk>=0.1.59,<0.2` | 0.1.59 spike-verified; minor version pinned |
| Q4 | Permission layer: `hooks={"PreToolUse": [...]}` (NOT `can_use_tool`) | R5: `can_use_tool` silent with `allowed_tools`; hooks fire unconditionally |
| Q5 | 7 explicit `HookMatcher`s (Bash + Read/Write/Edit/Glob/Grep + WebFetch) | U5 unverified (regex matcher); 7 explicit is safe default |
| Q6 | `ClaudeBridge.ask` → `AsyncIterator[Block]` + `emit` callback on handler | Streams blocks for DB, emits text to adapter |
| Q7 | History cap = **turn-limit 20** (`CLAUDE_HISTORY_LIMIT`); token-budget deferred to phase 4+ | v2.1 change: row-limit → turn-limit (fix B6) |
| Q8 | `load_recent` skips rows whose `turns.status != 'complete'` | Prevents orphan tool_use/tool_result from interrupted turns |
| Q9 | Manifest mtime-cached on `max(skills_dir.stat().st_mtime, *SKILL.md.st_mtime)` | APFS dir mtime doesn't change on in-place edit; must max over files |
| Q10 | `tools/ping/main.py` = plain stdlib (`python tools/ping/main.py`) | No nested venv for a 10-line smoke tool |
| Q11 | `parse_mode=None` (plain text) | Claude's markdown often invalid; HTML-escape overkill for phase 2 |
| Q12 | `EchoHandler` deleted, replaced by `ClaudeHandler` | No fallback — single handler entry point |
| Q13 | Migration 0002 (`turns` table + `conversations.block_type`) shipped in phase 2 | Closes tech-debt #4 now, not phase 4 |

### 1b. Spike R1–R5 (frozen baseline, do not re-verify)

See `spike-findings.md §1`. Summary:

- **R1** — multi-turn history via `query(prompt=async_gen, options=...)` with
  `{"type":"user","message":{"role":"user","content":<str|list>},...}`
  envelopes. No `resume=session_id`.
- **R2** — thinking via `ClaudeAgentOptions(max_thinking_tokens=N,
  effort="high")`. Not passed unless `thinking_budget > 0`.
- **R3** — skills discovery via `setting_sources=["project"]` + `cwd=project_root`.
  `SystemMessage(subtype="init").data["skills"]` lists registered skills.
- **R4** — message stream: `SystemMessage(init)` → `AssistantMessage(content=blocks)`*
  → `ResultMessage(usage, cost_usd, stop_reason, ...)`.
- **R5** — `hooks={"PreToolUse": [HookMatcher(matcher, hooks=[fn])]}` fires
  on every tool call. `can_use_tool` silent when `allowed_tools` set.

### 1c. Spike R7–R13 (wave-2 + fix-pack live probes, 2026-04-20)

| R | Question | Finding | Implementation impact |
|---|---|---|---|
| R7 | Automatic prompt caching at CLI layer? | YES — ephemeral_1h tier (`cache_creation ~5700`, `cache_read ~17400`). No manual `cache_control`. | Zero code change. Keep `build_manifest` mtime cache. |
| R8 | Bash hook bypass matrix | 36/36 vectors denied with hardened slip-guard. | `BASH_SLIP_GUARD_RE` in §2.4. `tests/test_bash_hook_bypass.py` with 36 cases + `cat a b .env` multi-arg case. |
| R9 | WebFetch DNS rebinding | String-only guard blind; adding DNS + `ipaddress` category check closes gap. TOCTOU residual → U9. | Two-layer guard in §2.4; DNS via `asyncio.to_thread` (B10). |
| R10 | session_id collision in parallel `query()` | SDK IGNORES our envelope session_id. Fresh UUID per query. | `session_id=f"chat-{chat_id}"` cosmetic. No Semaphore needed. |
| R11 | Migration 0002 crash + idempotency | All 3 scenarios pass: happy, crash → ROLLBACK preserves v=1, re-run on v=2 no-op. | **v2.1 change:** statement-by-statement runner (B2), not executescript + BEGIN EXCLUSIVE. |
| R12 | NULL `block_type` in history replay | Cannot happen on fresh migration; defense is cheap. | `btype = row.get("block_type") or "text"` in history. |
| R13 | **NEW:** assistant envelope replay honored by SDK? | **YES — SDK accepts and honors `{"type":"assistant","message":{"role":"assistant","content":[...]}}` envelopes.** Live probe: assistant envelope with SENTINEL "424242" → model replied "Your LUCKY_NUMBER is 424242". Baseline (user-only) → "I don't have any record". Clean differential. | **v2.1 change:** `history_to_sdk_envelopes` replays BOTH user AND assistant envelopes verbatim — no more synthetic tool-note fallback for assistant turns. |

---

## 2. Adjusted snippets (where this doc diverges from `detailed-plan.md`)

Below = authoritative code for each file. If it disagrees with
`detailed-plan.md`, this doc wins.

### 2.1 `src/assistant/state/migrations/0002_turns_block_type.sql` (reference only)

Keep as documentation of intent. **Production path is the statement-by-statement
Python in §2.2** — `executescript()` wrapped in `BEGIN EXCLUSIVE` is unsafe
(see §4 gotcha #3 + B2 rationale below).

```sql
-- Migration 0002 — add `turns` table + `conversations.block_type` column.
-- REFERENCE ONLY. Actual runner in src/assistant/state/db.py executes
-- each statement individually to avoid the executescript implicit-COMMIT trap.
-- This .sql file is kept so engineers can read the schema changes in one place.

DROP TABLE IF EXISTS conversations_new;

CREATE TABLE conversations_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    block_type   TEXT NOT NULL DEFAULT 'text',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

INSERT INTO conversations_new (
    id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type
)
SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text'
FROM conversations;

DROP TABLE conversations;
ALTER TABLE conversations_new RENAME TO conversations;

CREATE INDEX idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX idx_conversations_turn
    ON conversations(chat_id, turn_id);

CREATE TABLE turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|complete|interrupted
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at TEXT,
    meta_json    TEXT
);

INSERT OR IGNORE INTO turns (chat_id, turn_id, status, created_at)
SELECT chat_id, turn_id, 'complete', MIN(created_at)
FROM conversations
GROUP BY chat_id, turn_id;

CREATE INDEX idx_turns_chat_completed
    ON turns(chat_id, completed_at);
```

### 2.2 `src/assistant/state/db.py` — migration runner (statement-by-statement)

**v2.1 critical fix (B2):** the R11 spike tested statement-by-statement, but
v2 of this doc specified `conn.executescript(MIGRATION_0002_SQL)` inside
`BEGIN EXCLUSIVE`. Per [sqlite3 docs]
(https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.executescript):

> If there is a pending transaction, an implicit COMMIT statement is
> executed first.

→ Our `BEGIN EXCLUSIVE` is committed before migration SQL starts. If the
migration crashes mid-way, `rollback()` has nothing to rollback. Statement-
by-statement execution keeps the explicit BEGIN EXCLUSIVE active throughout.

**v2.1 critical fix (B1):** `_apply_0002` early-exits when `PRAGMA
user_version >= 2`. The outer `apply_schema` already checks this, but tests
occasionally call `_apply_0002` directly — the early-exit prevents re-running
on a production v=2 DB where rows already have `block_type='tool_use'` and
would be stomped by the backfill's `'text'` default.

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA_VERSION = 2

SCHEMA_0001 = """
CREATE TABLE IF NOT EXISTS conversations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    content_json TEXT NOT NULL,
    meta_json    TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_chat_time
    ON conversations(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_turn
    ON conversations(chat_id, turn_id);
"""


async def connect(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def _current_version(conn: aiosqlite.Connection) -> int:
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _apply_0001(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA_0001)
    await conn.execute("PRAGMA user_version=1")
    await conn.commit()


async def _apply_0002(conn: aiosqlite.Connection) -> None:
    """Run migration 0002 statement-by-statement.

    Atomicity & idempotency:
    - Early-exit if PRAGMA user_version >= 2 — protects direct test calls
      from stomping production data (B1).
    - statement-by-statement: executescript() would implicit-COMMIT our
      BEGIN EXCLUSIVE, defeating atomicity (B2 / sqlite3 docs).
    - ROLLBACK on exception preserves v=1 state; rerun converges to v=2.

    FK is toggled OFF during recreate-table; back ON in finally.
    """
    if await _current_version(conn) >= 2:
        return

    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN EXCLUSIVE")

        # 1. Drop any leftover from a previous partial run.
        await conn.execute("DROP TABLE IF EXISTS conversations_new")

        # 2. New conversations schema with block_type column.
        await conn.execute(
            "CREATE TABLE conversations_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER NOT NULL, "
            "turn_id TEXT NOT NULL, "
            "role TEXT NOT NULL, "
            "content_json TEXT NOT NULL, "
            "meta_json TEXT, "
            "block_type TEXT NOT NULL DEFAULT 'text', "
            "created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
        )

        # 3. Backfill rows from old table, coercing block_type to 'text' for legacy rows.
        await conn.execute(
            "INSERT INTO conversations_new "
            "(id, chat_id, turn_id, role, content_json, meta_json, created_at, block_type) "
            "SELECT id, chat_id, turn_id, role, content_json, meta_json, created_at, 'text' "
            "FROM conversations"
        )

        # 4. Drop old table.
        await conn.execute("DROP TABLE conversations")

        # 5. Rename new to conversations.
        await conn.execute("ALTER TABLE conversations_new RENAME TO conversations")

        # 6-7. Recreate indexes.
        await conn.execute(
            "CREATE INDEX idx_conversations_chat_time "
            "ON conversations(chat_id, created_at)"
        )
        await conn.execute(
            "CREATE INDEX idx_conversations_turn "
            "ON conversations(chat_id, turn_id)"
        )

        # 8. Turns table.
        await conn.execute(
            "CREATE TABLE turns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER NOT NULL, "
            "turn_id TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL DEFAULT 'pending', "
            "created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now')), "
            "completed_at TEXT, "
            "meta_json TEXT)"
        )

        # 9. Backfill turns from existing conversations grouped by turn_id.
        await conn.execute(
            "INSERT OR IGNORE INTO turns "
            "(chat_id, turn_id, status, created_at, completed_at) "
            "SELECT chat_id, turn_id, 'complete', MIN(created_at), MAX(created_at) "
            "FROM conversations GROUP BY chat_id, turn_id"
        )

        # 10. Turns index for load_recent ORDER BY completed_at.
        await conn.execute(
            "CREATE INDEX idx_turns_chat_completed "
            "ON turns(chat_id, completed_at)"
        )

        # 11. Bump version inside the transaction — commit makes both visible.
        await conn.execute("PRAGMA user_version=2")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


async def apply_schema(conn: aiosqlite.Connection) -> None:
    current = await _current_version(conn)
    if current < 1:
        await _apply_0001(conn)
    if current < 2:
        await _apply_0002(conn)
```

### 2.3 `src/assistant/state/conversations.py` — Turn API + turn-limit `load_recent`

Replaces phase-1 minimal store. **v2.1 critical fix (B6):** `load_recent` was
a row-limit (`LIMIT 20`), which can return a partial turn (first 12 blocks of
turn_D but not the last 3). history→envelope grouping then gets a truncated
envelope, and the SDK sees a broken conversation.

v2.1 reworks `load_recent` to a **turn-limit**: fetch all rows belonging to
the last N complete turns. Semantic change: `history_limit` now = "last N
turns", not "last N rows". Default still 20.

```python
from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def start_turn(self, chat_id: int) -> str:
        turn_id = uuid.uuid4().hex
        await self._conn.execute(
            "INSERT INTO turns(chat_id, turn_id, status, created_at) "
            "VALUES (?,?,'pending', strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (chat_id, turn_id),
        )
        await self._conn.commit()
        return turn_id

    async def complete_turn(self, turn_id: str, meta: dict[str, Any]) -> None:
        await self._conn.execute(
            "UPDATE turns SET status='complete', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "meta_json=? WHERE turn_id=?",
            (json.dumps(meta, ensure_ascii=False), turn_id),
        )
        await self._conn.commit()

    async def interrupt_turn(self, turn_id: str) -> None:
        await self._conn.execute(
            "UPDATE turns SET status='interrupted', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE turn_id=? AND status='pending'",
            (turn_id,),
        )
        await self._conn.commit()

    async def append(
        self,
        chat_id: int,
        turn_id: str,
        role: str,
        blocks: list[dict[str, Any]],
        *,
        block_type: str,
        meta: dict[str, Any] | None = None,
    ) -> int:
        async with self._conn.execute(
            "INSERT INTO conversations"
            "(chat_id, turn_id, role, content_json, meta_json, block_type) "
            "VALUES (?,?,?,?,?,?) RETURNING id",
            (
                chat_id,
                turn_id,
                role,
                json.dumps(blocks, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False) if meta else None,
                block_type,
            ),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def load_recent(
        self, chat_id: int, limit: int
    ) -> list[dict[str, Any]]:
        """Return all rows belonging to the N most recent COMPLETE turns,
        in chronological order (ASC by id).

        `limit` is a turn count, not a row count (B6 fix). Interrupted and
        pending turns are skipped wholesale (subquery filter).
        """
        q = (
            "WITH recent_turns AS ("
            "  SELECT turn_id FROM turns "
            "  WHERE chat_id = ? AND status = 'complete' "
            "  ORDER BY completed_at DESC LIMIT ?"
            ") "
            "SELECT c.id, c.chat_id, c.turn_id, c.role, c.content_json, "
            "c.meta_json, c.created_at, c.block_type "
            "FROM conversations c "
            "JOIN recent_turns t ON c.turn_id = t.turn_id "
            "ORDER BY c.id ASC"
        )
        async with self._conn.execute(q, (chat_id, limit)) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "chat_id": r[1],
                "turn_id": r[2],
                "role": r[3],
                "content": json.loads(r[4]),
                "meta": json.loads(r[5]) if r[5] else None,
                "created_at": r[6],
                "block_type": r[7],
            }
            for r in rows
        ]

    async def cleanup_orphan_pending_turns(self) -> int:
        """Mark any turns still in 'pending' status as 'interrupted'.

        Call at daemon startup — after a crash mid-turn, turns row keeps
        status='pending' forever. `load_recent` filter skips them but they
        accumulate forever (S10 fix).

        Returns number of rows updated for logging.
        """
        cur = await self._conn.execute(
            "UPDATE turns SET status='interrupted', "
            "completed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE status='pending'"
        )
        await self._conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0
```

### 2.4 `src/assistant/bridge/hooks.py` (new) — 7 hook handlers

Separated from `bridge/claude.py` for unit-testability. Each factory returns
a hook closure; `claude.py::_build_options` assembles them into
`HookMatcher`s.

**v2.1 fixes:**
- **B7:** Bash `cat` allowlist validates ALL positional args (not just the
  first). `cat README.md .env` must deny.
- **B8:** file_hook ALWAYS resolves candidate path (relative or absolute)
  against project_root. Was: "only resolve if is_absolute". Relative
  `../../.env` bypassed the guard because SDK's cwd=project_root would
  resolve it at fetch time.
- **B9:** For `Read`/`Write`/`Edit` — empty `file_path` → explicit deny.
  For `Glob`/`Grep` — empty `path` defaults to `"."` (project scan is
  intentional, not a bypass).
- **B10:** WebFetch DNS lookup via `asyncio.to_thread` — blocking
  `getaddrinfo` out of event loop. Except-clause widened:
  `(socket.gaierror, OSError, socket.timeout)`.

```python
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from assistant.logger import get_logger

log = get_logger("bridge.hooks")

# ---------------------------------------------------------------------------
# Bash allowlist-first (R8 hardened slip-guard; 36/36 bypass matrix passes)
# ---------------------------------------------------------------------------
BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "python3 tools/",
    "uv run tools/",
    "git status",
    "git log",
    "git diff",
    "ls ",
    "ls",
    "pwd",
    "echo ",
    # "cat <path>..." — handled by special-case below
)

BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}|"
    r"[;&|`]|\$\(|<\(|>\(|"
    r"\\x[0-9a-f]{2}|\\[0-7]{3}"
    r")",
    re.IGNORECASE,
)

FILE_TOOL_NAMES: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

WEBFETCH_BLOCKED_HOST_SUBSTRINGS: tuple[str, ...] = (
    "localhost", "127.", "0.0.0.0", "169.254.", "10.",
    "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.",
    "[::1]", "[fc", "[fd",
)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ---------------- Bash ----------------

def _cat_targets_ok(args: list[str], project_root: Path) -> tuple[bool, str | None]:
    """Validate ALL positional cat args resolve inside project_root (B7 fix).

    Returns (ok, deny_reason). `ok=True, reason=None` → allow.
    Any `-` flag-style arg → deny (conservative — no `-n`, `-A` etc).
    """
    root = project_root.resolve()
    if not args:
        return False, "cat with no arguments"
    if any(a.startswith("-") for a in args):
        return False, "cat flags (-n, -A, etc.) not allowed"
    for target in args:
        try:
            p = Path(target).expanduser()
            resolved = (project_root / p).resolve() if not p.is_absolute() else p.resolve()
        except OSError as e:
            return False, f"cat target {target!r}: {e}"
        if not str(resolved).startswith(str(root)):
            return False, f"cat target {target!r} resolves outside project_root"
    return True, None


def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    """Return deny-reason iff cmd is NOT allowed. None → allow."""
    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if any(stripped.startswith(p) for p in BASH_ALLOWLIST_PREFIXES):
        return None
    # Special-case `cat <path>...` — allow iff ALL args resolve inside project_root.
    if stripped.startswith("cat "):
        args = stripped[4:].strip().split()
        ok, reason = _cat_targets_ok(args, project_root)
        if ok:
            return None
        return reason or "cat target outside project_root"
    return (
        "Bash command not in allowlist. If you need this operation, ask the "
        "owner to add it to tools/<name>/main.py or expand the allowlist."
    )


def make_bash_hook(project_root: Path) -> Any:
    """Allowlist-first Bash guard + R8 slip-guard defence-in-depth."""
    async def bash_hook(
        input_data: dict, tool_use_id: str | None, ctx: dict
    ) -> dict:
        cmd = (input_data.get("tool_input", {}) or {}).get("command", "") or ""
        reason = _bash_allowlist_check(cmd, project_root)
        if reason is not None:
            log.warning(
                "pretool_decision", tool_name="Bash", decision="deny",
                subreason="allowlist", cmd=cmd[:200],
            )
            return _deny(reason)
        if BASH_SLIP_GUARD_RE.search(cmd):
            log.warning(
                "pretool_decision", tool_name="Bash", decision="deny",
                subreason="slip_guard", cmd=cmd[:200],
            )
            return _deny(
                "Command matched a secrets/encoded-payload pattern. Reading "
                ".env/.ssh/.aws/tokens/encoded blobs via Bash is blocked."
            )
        log.debug("pretool_decision", tool_name="Bash", decision="allow", cmd=cmd[:120])
        return {}
    return bash_hook


# ---------------- File-tools (Read/Write/Edit/Glob/Grep) ----------------

def make_file_hook(project_root: Path) -> Any:
    """Single factory used for all 5 file-tool HookMatcher entries.

    B8 fix: ALWAYS resolve against project_root (relative OR absolute).
    B9 fix: Read/Write/Edit require file_path; Glob/Grep allow empty path
      (defaults to '.'); pattern-only Grep is the common case.
    """
    root = project_root.resolve()

    async def file_hook(
        input_data: dict, tool_use_id: str | None, ctx: dict
    ) -> dict:
        tool_name = input_data.get("tool_name") or ""
        ti = input_data.get("tool_input", {}) or {}

        if tool_name in ("Read", "Write", "Edit"):
            candidate = ti.get("file_path")
            if not candidate:
                log.warning(
                    "pretool_decision", tool_name=tool_name,
                    decision="deny", subreason="missing_file_path",
                )
                return _deny(f"{tool_name} requires file_path")
        elif tool_name in ("Glob", "Grep"):
            # Glob: `path` optional (defaults to cwd); Grep: `path` optional too.
            candidate = ti.get("path") or "."
        else:
            # Hook should only be registered for the 5 tools above.
            return {}

        try:
            p = Path(candidate).expanduser()
            resolved = p.resolve() if p.is_absolute() else (project_root / p).resolve()
        except OSError as e:
            return _deny(f"invalid path {candidate!r}: {e}")

        if not str(resolved).startswith(str(root)):
            log.warning(
                "pretool_decision", tool_name=tool_name,
                decision="deny", subreason="outside_project_root",
                path=str(resolved),
            )
            return _deny(
                f"Path outside project_root ({root}) is not allowed: {resolved}"
            )
        return {}
    return file_hook


# ---------------- WebFetch SSRF (R9 two-layer: string + DNS → ipaddress) ----------------

def _ip_is_blocked(ip_str: str) -> str | None:
    """Return a reason if the IP is in a private/reserved/loopback range."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"invalid IP {ip_str!r}"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link_local"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    return None


def make_webfetch_hook() -> Any:
    """Two-layer SSRF guard (see spike-findings §R9).

    Layer 1: literal host/URL substring match — cheap, catches direct attempts.
    Layer 2: DNS → category check — catches public hostname → private IP.

    B10 fix: `getaddrinfo` runs in `asyncio.to_thread` so blocking DNS (up to
    ~5s per Darwin default) does NOT stall the event loop. Exception clause
    widened to `(gaierror, OSError, timeout)`.

    Residual TOCTOU DNS-rebinding risk → see unverified-assumptions.md §U9.
    """
    async def webfetch_hook(
        input_data: dict, tool_use_id: str | None, ctx: dict
    ) -> dict:
        ti = input_data.get("tool_input", {}) or {}
        url = (ti.get("url") or "").strip()
        if not url:
            return {}
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return _deny(f"malformed URL: {url!r}")
        raw = url.lower()
        # Layer 1: literal match
        for needle in WEBFETCH_BLOCKED_HOST_SUBSTRINGS:
            if host.startswith(needle.rstrip(".").rstrip("]")) or needle in raw:
                log.warning(
                    "pretool_decision", tool_name="WebFetch",
                    decision="deny", subreason="literal_blocked", url=url[:200],
                )
                return _deny(
                    f"WebFetch to private/link-local/metadata host is blocked: {host!r}."
                )
        if not host:
            return _deny("WebFetch with empty host is not allowed.")
        # Layer 2: DNS (in thread) + ipaddress category check
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM
            )
        except (socket.gaierror, OSError, socket.timeout):
            # NXDOMAIN / transient / timeout → allow; CLI will fail the fetch.
            return {}
        for _, _, _, _, sockaddr in infos:
            reason = _ip_is_blocked(sockaddr[0])
            if reason:
                log.warning(
                    "pretool_decision", tool_name="WebFetch",
                    decision="deny", subreason=f"dns_{reason}",
                    url=url[:200], resolved_ip=sockaddr[0],
                )
                return _deny(
                    f"WebFetch resolved to blocked IP {sockaddr[0]} ({reason})."
                )
        return {}
    return webfetch_hook
```

### 2.5 `src/assistant/bridge/claude.py` — ClaudeBridge

Thin facade over SDK. Assembles options; owns `asyncio.Semaphore`; yields
blocks + final `ResultMessage`. Contract with handler: `ResultMessage` is
the success signal — any other termination path (timeout, exception, break)
leaves `completed=False` and the handler calls `interrupt_turn`.

**v2.1 fix (S2):** `ResultMessage.model` does not exist in SDK types.py.
Capture `model` from `AssistantMessage.model` inside the loop and use that
when logging the ResultMessage.

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    query,
)

from assistant.bridge.history import history_to_sdk_envelopes
from assistant.bridge.hooks import (
    FILE_TOOL_NAMES,
    make_bash_hook,
    make_file_hook,
    make_webfetch_hook,
)
from assistant.bridge.skills import build_manifest
from assistant.config import Settings
from assistant.logger import get_logger

log = get_logger("bridge.claude")


class ClaudeBridgeError(RuntimeError):
    pass


class ClaudeBridge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        pr = self._settings.project_root
        hooks = {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[make_bash_hook(pr)]),
                *[
                    HookMatcher(matcher=t, hooks=[make_file_hook(pr)])
                    for t in FILE_TOOL_NAMES
                ],
                HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
            ]
        }
        thinking_kwargs: dict[str, Any] = {}
        if self._settings.claude.thinking_budget > 0:
            thinking_kwargs["max_thinking_tokens"] = (
                self._settings.claude.thinking_budget
            )
            thinking_kwargs["effort"] = self._settings.claude.effort
        return ClaudeAgentOptions(
            cwd=str(pr),
            setting_sources=["project"],
            max_turns=self._settings.claude.max_turns,
            allowed_tools=[
                "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
            ],
            hooks=hooks,
            system_prompt=system_prompt,
            **thinking_kwargs,
        )

    def _render_system_prompt(self) -> str:
        template = (
            self._settings.project_root
            / "src" / "assistant" / "bridge" / "system_prompt.md"
        ).read_text(encoding="utf-8")
        manifest = build_manifest(self._settings.project_root / "skills")
        return template.format(
            project_root=str(self._settings.project_root),
            skills_manifest=manifest,
        )

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Yield Block instances, then ResultMessage (last), then return.

        R10 note: the `session_id=f"chat-{chat_id}"` in envelopes is cosmetic;
        SDK/CLI reassigns its own UUID per query. No collision possible on
        concurrent chat_id.

        R13 note: assistant envelopes ARE honored. `history_to_sdk_envelopes`
        replays both user AND assistant turns verbatim.

        S2 note: `model` is captured from `AssistantMessage.model` — it does
        NOT exist on `ResultMessage` in SDK 0.1.59's types.py.
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        log.info(
            "query_start", chat_id=chat_id, prompt_len=len(user_text),
            history_rows=len(history),
        )

        async def prompt_stream() -> AsyncIterable[dict[str, Any]]:
            for envelope in history_to_sdk_envelopes(history, chat_id):
                yield envelope
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": f"chat-{chat_id}",  # cosmetic — SDK ignores per R10
            }

        last_model: str | None = None
        async with self._sem:
            try:
                async with asyncio.timeout(self._settings.claude.timeout):
                    async for message in query(prompt=prompt_stream(), options=opts):
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            log.info(
                                "sdk_init",
                                model=message.data.get("model"),
                                skills=list(message.data.get("skills") or []),
                                cwd=message.data.get("cwd"),
                            )
                            continue
                        if isinstance(message, AssistantMessage):
                            # S2: capture model here (ResultMessage has no .model).
                            last_model = getattr(message, "model", None) or last_model
                            for block in message.content:
                                log.debug("block_received", type=type(block).__name__)
                                yield block
                            continue
                        if isinstance(message, ResultMessage):
                            usage = message.usage or {}
                            log.info(
                                "result_received",
                                model=last_model,  # S2: from AssistantMessage, not ResultMessage
                                stop_reason=getattr(message, "stop_reason", None),
                                cost_usd=message.total_cost_usd,
                                duration_ms=getattr(message, "duration_ms", None),
                                num_turns=getattr(message, "num_turns", None),
                                input_tokens=usage.get("input_tokens"),
                                output_tokens=usage.get("output_tokens"),
                                cache_read=usage.get("cache_read_input_tokens"),
                                cache_creation=usage.get("cache_creation_input_tokens"),
                                sdk_session_id=getattr(message, "session_id", None),
                            )
                            yield message
                            return
                        # RateLimitEvent, other SystemMessage subtypes, UserMessage — skip.
            except TimeoutError as e:
                log.warning(
                    "timeout", chat_id=chat_id,
                    timeout_s=self._settings.claude.timeout,
                )
                raise ClaudeBridgeError("timeout") from e
            except Exception as e:
                log.error("sdk_error", error=repr(e))
                raise ClaudeBridgeError(f"sdk error: {e}") from e
```

### 2.6 `src/assistant/bridge/history.py` — envelopes with full assistant replay

**v2.1 critical fix (S7, R13-verified):** the previous v2 dropped all
assistant rows and injected a synthetic Russian-language tool-note. Live
probe R13 proved the SDK honors assistant envelopes verbatim — feeding the
model an assistant turn with a sentinel number caused the next turn's reply
to contain that number (baseline without the assistant envelope returned
"I don't have a record").

So we now emit a full envelope list:
- `role='user'` rows (`block_type='text'` or `block_type='tool_result'`) →
  user envelope.
- `role='assistant'` rows (`block_type∈{text,tool_use}`) → assistant envelope.
- `block_type='thinking'` rows → DROPPED (U2 stands: cross-session thinking
  signature rejected by SDK).

Function renamed `history_to_sdk_envelopes` (from `_to_user_envelopes`) to
reflect the new shape.

**v2.1 fix (B5 / classification rationale):** `ToolResultBlock` goes on
`role='user'`, NOT `role='tool'`. SDK streaming-input mode expects tool
results on a user envelope per Anthropic's tools API contract.

```python
from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def history_to_sdk_envelopes(
    rows: list[dict[str, Any]], chat_id: int
) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows → SDK streaming-input envelope stream.

    Row → envelope mapping (R13-verified):
      role='user', block_type='text'        → user envelope (text block)
      role='user', block_type='tool_result' → user envelope (tool_result block)
      role='assistant', block_type='text'   → assistant envelope (text block)
      role='assistant', block_type='tool_use' → assistant envelope (tool_use block)
      block_type='thinking'                 → DROPPED (U2: SDK rejects cross-session)

    R12 defence: treat NULL `block_type` as 'text' (shouldn't happen after 0002).
    R10 note: `session_id` is cosmetic; SDK ignores it in streaming-input mode.
    Kept as a human-readable breadcrumb in logs only.

    Rows are grouped by (turn_id, role) so that multiple same-role rows
    within a turn become a single multi-block envelope — this matches the
    structure the SDK produced originally.
    """
    session_id = f"chat-{chat_id}"

    # Preserve temporal order (load_recent already ORDER BY id ASC).
    # Group consecutive same-(turn_id, role) rows into a single envelope.
    current_key: tuple[str, str] | None = None
    buffer: list[dict[str, Any]] = []

    def flush() -> Iterator[dict[str, Any]]:
        if not buffer or current_key is None:
            return
        _turn_id, role = current_key
        # If a single text block, pass as string (SDK accepts either shape).
        if (
            len(buffer) == 1
            and buffer[0].get("type") == "text"
            and role == "user"
        ):
            content: Any = buffer[0]["text"]
        else:
            content = list(buffer)
        envelope: dict[str, Any] = {
            "type": role,  # "user" or "assistant"
            "message": {"role": role, "content": content},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
        # SDK's assistant envelope shape includes 'model' on the inner message.
        # Use whatever model stamped the turn's result (meta_json on turns
        # table carries it); absent → omit the field (SDK tolerates).
        if role == "assistant":
            # Model not round-tripped in row.meta currently — omit is fine.
            pass
        yield envelope

    for row in rows:
        btype = row.get("block_type") or "text"  # R12 defence
        if btype == "thinking":
            continue  # U2: SDK rejects cross-session thinking signature

        role = row["role"]
        # Normalize role: B5 — DB stores tool_result rows with role='user'
        # (because handler classifies ToolResultBlock as role='user', see
        # handlers/message.py::_classify_block). So no remapping here.
        turn_id = row["turn_id"]
        key = (turn_id, role)

        if current_key != key:
            # Boundary — flush accumulated buffer for previous (turn, role).
            yield from flush()
            current_key = key
            buffer = []

        # `content` column is already a list of block dicts. Extend, don't wrap.
        blocks = row.get("content") or []
        if isinstance(blocks, list):
            buffer.extend(blocks)
        else:
            # Legacy defensive: single block dict
            buffer.append(blocks)  # type: ignore[arg-type]

    # Final flush after loop.
    yield from flush()
```

### 2.7 `src/assistant/bridge/skills.py` — manifest with mtime-max cache (Q9)

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_MANIFEST_CACHE: dict[Path, tuple[float, int, str]] = {}


def parse_skill(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    return {
        "name": meta.get("name", path.parent.name),
        "description": (meta.get("description") or "").strip(),
        "allowed_tools": meta.get("allowed-tools", []),
    }


def _manifest_cache_key(skills_dir: Path) -> tuple[float, int]:
    """Tuple of (max-mtime, file-count) so ADD/REMOVE of SKILL.md invalidates.

    APFS dir mtime doesn't change on in-place edit; must include per-file
    mtimes. file-count defends the S5 corner case where a SKILL.md is
    deleted and another file is added with mtime == deleted file's mtime.
    """
    paths = sorted(skills_dir.glob("*/SKILL.md"))
    mtimes = [skills_dir.stat().st_mtime]
    mtimes.extend(p.stat().st_mtime for p in paths)
    return (max(mtimes), len(paths))


def build_manifest(skills_dir: Path) -> str:
    if not skills_dir.exists():
        return "(skills directory missing)"
    mtime, count = _manifest_cache_key(skills_dir)
    cached = _MANIFEST_CACHE.get(skills_dir)
    if cached and cached[0] == mtime and cached[1] == count:
        return cached[2]
    entries: list[str] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        meta = parse_skill(skill_md)
        if not meta.get("description"):
            continue
        entries.append(f"- **{meta['name']}** — {meta['description']}")
    manifest = "\n".join(entries) if entries else "(no skills registered yet)"
    _MANIFEST_CACHE[skills_dir] = (mtime, count, manifest)
    return manifest
```

### 2.8 `src/assistant/bridge/bootstrap.py` — symlink + settings audit

**v2.1 fixes:**
- **S4:** symlink target is absolute (`project_root / "skills"`), not relative
  (`"../skills"`). Resolves the edge-case where the parent working directory
  changes before SDK dereferences the link.
- **S15:** `.claude/settings*.json` containing `hooks` or `permissions` keys
  blocks startup with `sys.exit(3)` + a migration instruction. Plain settings
  (e.g. `statusLine` only) still pass with a warning.

```python
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any


SETTINGS_STARTUP_BLOCK_KEYS: tuple[str, ...] = ("hooks", "permissions")


def ensure_skills_symlink(project_root: Path) -> None:
    """Idempotently create `.claude/skills -> <project_root>/skills` (absolute).

    S4 fix: use an absolute symlink target so the link stays valid even if the
    process chdir's away. If a link with a different target (e.g. old
    relative `../skills`) is present, replace it.
    """
    link = project_root / ".claude" / "skills"
    link.parent.mkdir(exist_ok=True)
    target = (project_root / "skills").resolve()
    if link.is_symlink():
        # readlink() returns whatever was stored (absolute OR relative).
        # Compare resolved target.
        try:
            current = (link.parent / link.readlink()).resolve()
        except OSError:
            current = None
        if current == target:
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(
            f".claude/skills exists and is not a symlink: {link}"
        )
    link.symlink_to(target, target_is_directory=True)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<REDACTED>" if any(
                s in k.lower() for s in ("token", "secret", "key", "password")
            ) else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def assert_no_custom_claude_settings(
    project_root: Path, logger: logging.Logger
) -> None:
    """Guard against `.claude/settings*.json` silently overriding our hooks.

    S15 policy (v2.1):
    - If the settings file contains `hooks` or `permissions` at top level →
      BLOCK startup with sys.exit(3). The SDK would merge these into the
      session and could bypass ALL our deny hooks.
    - Otherwise (cosmetic keys like `statusLine`, `defaultModel`) → warn and
      log redacted content.

    Rationale: `setting_sources=["project"]` includes `.claude/settings.json`
    in CLI config resolution. A stray permissions or hooks block here was a
    silent bypass of every defence in phase 2. Owner should consciously
    re-home such config under our source-of-truth (bridge/hooks.py).
    """
    for name in ("settings.json", "settings.local.json"):
        path = project_root / ".claude" / name
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — diagnostic
            logger.warning("Failed to parse .claude/%s: %s", name, exc)
            continue
        blocker_keys = (
            [k for k in SETTINGS_STARTUP_BLOCK_KEYS if k in raw]
            if isinstance(raw, dict)
            else []
        )
        if blocker_keys:
            logger.error(
                "claude_settings_conflict",
                file=name,
                blocker_keys=blocker_keys,
                hint=(
                    f"Remove or migrate {blocker_keys} from .claude/{name}. "
                    "Permissions and hooks MUST live in bridge/hooks.py "
                    "(phase 2 baseline). Startup aborted to prevent silent "
                    "hook bypass."
                ),
            )
            sys.exit(3)
        logger.warning(
            ".claude/%s present — allowed (no hooks/permissions). Content (redacted): %s",
            name,
            _redact(raw),
        )
```

### 2.9 `src/assistant/bridge/system_prompt.md`

```
You are 0xone-assistant, a personal Claude-Code-powered assistant for your owner.

Identity & style:
- Default language: Russian unless the user writes in another language.
- Be concise; avoid filler.

Capabilities:
- You have access to the project at {project_root}.
- You extend your own capabilities through Skills (self-contained CLI tools).

Available skills (rebuilt on every request):
{skills_manifest}

Rules:
- Long-term memory lives in an Obsidian vault accessible only through the `memory` skill.
  If the `memory` skill is not yet listed above, tell the owner you cannot persist long-term
  memory yet and do NOT try to simulate it with ad-hoc files.
- Do not invent skills that are not in the list above.
- Bash is allowed but constrained to an allowlist (you will see a deny
  message if a command is out of scope).
- File edits are sandboxed to {project_root}.
```

### 2.10 `src/assistant/handlers/message.py` — `ClaudeHandler`

**v2.1 fix (B5):** `ToolResultBlock` classified as `role='user'` +
`block_type='tool_result'` (was: `role='tool'`). SDK contract: tool results
live on a user envelope per Anthropic's tools API. Storing rows with
`role='tool'` would be dropped by `history_to_sdk_envelopes` on replay →
silent broken continuity.

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore

Emit = Callable[[str], Awaitable[None]]
log = get_logger("handlers.message")


def _classify_block(
    item: Any,
) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Classify an SDK message/block into (role, payload, text_to_emit, block_type).

    v2.1 (B5) block-type / role contract, per Anthropic tools API:
      TextBlock from AssistantMessage → role='assistant', block_type='text'
      ThinkingBlock → role='assistant', block_type='thinking'
      ToolUseBlock → role='assistant', block_type='tool_use'
      ToolResultBlock → role='user', block_type='tool_result'
        ^^^ SDK streaming-input mode requires tool_result on USER envelope, not 'tool'.
      ResultMessage → special role='result' (handled by caller for meta, not stored as row).
    """
    if isinstance(item, ResultMessage):
        usage = item.usage or {}
        meta = {
            "stop_reason": getattr(item, "stop_reason", None),
            "usage": usage,
            "cost_usd": item.total_cost_usd,
            "duration_ms": getattr(item, "duration_ms", None),
            "num_turns": getattr(item, "num_turns", None),
            "sdk_session_id": getattr(item, "session_id", None),
        }
        return ("result", meta, None, None)
    if isinstance(item, TextBlock):
        return (
            "assistant",
            {"type": "text", "text": item.text},
            item.text,
            "text",
        )
    if isinstance(item, ThinkingBlock):
        return (
            "assistant",
            {"type": "thinking", "thinking": item.thinking, "signature": item.signature},
            None,
            "thinking",
        )
    if isinstance(item, ToolUseBlock):
        return (
            "assistant",
            {"type": "tool_use", "id": item.id, "name": item.name, "input": item.input},
            None,
            "tool_use",
        )
    if isinstance(item, ToolResultBlock):
        return (
            # B5: role='user' (NOT 'tool') — SDK requires ToolResultBlock on
            # user envelope per Anthropic tools API contract.
            "user",
            {
                "type": "tool_result",
                "tool_use_id": item.tool_use_id,
                "content": item.content,
                "is_error": item.is_error,
            },
            None,
            "tool_result",
        )
    return (None, {}, None, None)


class ClaudeHandler:
    def __init__(
        self,
        settings: Settings,
        conv: ConversationStore,
        bridge: ClaudeBridge,
    ) -> None:
        self._settings = settings
        self._conv = conv
        self._bridge = bridge

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._conv.start_turn(msg.chat_id)
        log.info(
            "turn_started",
            turn_id=turn_id,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": msg.text}],
            block_type="text",
        )
        history = await self._conv.load_recent(
            msg.chat_id, self._settings.claude.history_limit
        )
        # Current turn has status='pending' → load_recent's 'complete' filter
        # excludes it; we won't see our own user row.

        completed = False
        try:
            async for item in self._bridge.ask(msg.chat_id, msg.text, history):
                role, payload, text_out, block_type = _classify_block(item)
                if role == "result":
                    await self._conv.complete_turn(turn_id, meta=payload)
                    completed = True
                    log.info(
                        "turn_complete",
                        turn_id=turn_id,
                        cost_usd=payload.get("cost_usd"),
                    )
                    continue
                if role is None:
                    continue
                assert block_type is not None
                await self._conv.append(
                    msg.chat_id, turn_id, role, [payload],
                    block_type=block_type,
                )
                if text_out:
                    await emit(text_out)
        except ClaudeBridgeError as e:
            await emit(f"\n\n⚠ {e}")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
```

### 2.11 `src/assistant/config.py` — XDG + ClaudeSettings

**v2.1 fix (N2):** `project_root` pydantic `@field_validator` resolves to
absolute, defending against someone passing a relative path via env var.

```python
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "0xone-assistant"


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _user_env_file() -> Path:
    return _default_config_dir() / ".env"


class ClaudeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    timeout: int = 300
    max_turns: int = 20
    max_concurrent: int = 2
    history_limit: int = 20  # turn-count, not row-count (B6)
    thinking_budget: int = 0
    effort: str = "medium"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    telegram_bot_token: str = Field(min_length=10)
    owner_chat_id: int = Field(gt=0)
    log_level: str = "INFO"
    project_root: Path = Field(default_factory=_default_project_root)
    data_dir: Path = Field(default_factory=_default_data_dir)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)  # type: ignore[arg-type]

    @field_validator("project_root", "data_dir", mode="after")
    @classmethod
    def _resolve_absolute(cls, v: Path) -> Path:
        """N2: resolve to absolute so downstream code never sees '.'-relative paths."""
        return v.expanduser().resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

### 2.12 `src/assistant/main.py` — Daemon wiring

**v2.1 critical fix (B3):** the phase-1 `main()` has supervision logic
(`stop_event`, `polling_task.add_done_callback`, SIGTERM/SIGINT handlers,
`polling_exc` re-raise) that MUST be preserved. The v2 draft showed a
one-liner `asyncio.run(daemon.start())` that would drop all of it — bot
would either exit on start or hang on SIGTERM.

**Coder instruction:** keep phase-1 `main()` body exactly; only change is
to thread `settings` through to `Daemon(settings)`.

**v2.1 fix (S10):** bootstrap cleans up any `pending` turns from a prior
crash (mark them `interrupted`).

```python
from __future__ import annotations

import asyncio
import signal
import sys

import aiosqlite

from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.bootstrap import (
    assert_no_custom_claude_settings,
    ensure_skills_symlink,
)
from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, get_settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger, setup_logging
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect

log = get_logger("main")


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._adapter: TelegramAdapter | None = None
        self._conn: aiosqlite.Connection | None = None

    async def _preflight_claude_auth(self) -> None:
        """Fail-fast if `claude` CLI is missing or not authenticated."""
        plog = get_logger("daemon.preflight")
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--print", "ping",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _out, err = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                plog.error("claude_cli_timeout", hint="`claude --print ping` hung for 15s")
                sys.exit(3)
        except FileNotFoundError:
            plog.error(
                "claude_cli_missing",
                hint="Install Claude Code CLI and run `claude login`.",
            )
            sys.exit(3)
        if proc.returncode != 0:
            tail = (err or b"").decode("utf-8", "replace").lower()
            if "auth" in tail or "login" in tail or "not authenticated" in tail:
                plog.error(
                    "claude_cli_not_authenticated",
                    hint="Run `claude login` before starting the bot.",
                )
            else:
                plog.error("claude_cli_failed", stderr=tail[:500])
            sys.exit(3)
        plog.info("auth_preflight_ok")

    async def start(self) -> None:
        await self._preflight_claude_auth()
        assert_no_custom_claude_settings(self._settings.project_root, log)
        ensure_skills_symlink(self._settings.project_root)

        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        store = ConversationStore(self._conn)
        # S10: clean up orphan pending turns from prior crash.
        orphans = await store.cleanup_orphan_pending_turns()
        if orphans:
            log.info("orphan_turns_cleaned", count=orphans)

        bridge = ClaudeBridge(self._settings)
        handler = ClaudeHandler(self._settings, store, bridge)
        self._adapter = TelegramAdapter(self._settings)
        self._adapter.set_handler(handler)
        await self._adapter.start()
        log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        log.info("daemon_stopping")
        if self._adapter is not None:
            await self._adapter.stop()
        if self._conn is not None:
            await self._conn.close()
        log.info("daemon_stopped")

    @property
    def polling_task(self) -> asyncio.Task[None] | None:
        if self._adapter is None:
            return None
        return self._adapter.polling_task


async def main() -> None:
    """B3: phase-1 signal supervision preserved verbatim.

    Only delta from phase 1: pass `settings` into `Daemon(settings)`.
    Do NOT replace with a one-liner `asyncio.run(daemon.start())` — that
    drops SIGTERM handling and the polling crash supervision.
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon(settings)
    stop_event = asyncio.Event()
    polling_exc: BaseException | None = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        polling = d.polling_task
        assert polling is not None, "polling_task must exist after start()"

        def _on_polling_done(t: asyncio.Task[None]) -> None:
            nonlocal polling_exc
            if not t.cancelled():
                polling_exc = t.exception()
            stop_event.set()

        polling.add_done_callback(_on_polling_done)
        await stop_event.wait()
    finally:
        await d.stop()
    if polling_exc is not None:
        log.error("polling_crashed", error=str(polling_exc))
        raise polling_exc
```

### 2.13 `src/assistant/adapters/base.py` + `telegram.py` — emit callback, preserve ABC, preserve message_id

**v2.1 fixes (B4):**
- Phase-1 `IncomingMessage` has `{chat_id, message_id, text}`. Keep
  `message_id` — it's referenced in handler logs for correlation (SDK's
  `sdk_session_id` is ephemeral per R10).
- Keep `MessengerAdapter(ABC)` with `send_text` — phase-5 scheduler will
  inject via adapter (no handler in scope then). Add `Handler(Protocol)`
  **alongside**, not replacing.

Full updated `base.py`:

```python
# src/assistant/adapters/base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

Emit = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    message_id: int  # B4: preserved from phase 1 for log correlation
    text: str


class MessengerAdapter(ABC):
    """Phase-1 ABC kept — phase-5 scheduler injects via adapter, not handler."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...


class Handler(Protocol):
    """Phase-2 handler contract: receive an incoming message and emit text chunks."""

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None: ...
```

Deltas for `telegram.py` (phase-1 patterns preserved — only relevant changes
shown):

```python
# telegram.py — deltas vs phase 1
# (1) DefaultBotProperties(parse_mode=None) — was HTML in phase 1.
# (2) _on_text uses the new Handler protocol with emit-callback,
#     aggregates chunks locally, splits messages >4096 chars for Telegram.
# (3) _Handler(Protocol) replaced by assistant.adapters.base.Handler.

from aiogram.client.default import DefaultBotProperties
# parse_mode=None per Q11 — Claude's markdown often invalid; plain text is safer.
bot = Bot(
    token=settings.telegram_bot_token,
    default=DefaultBotProperties(parse_mode=None),
)

async def _on_text(message: Message) -> None:
    if self._handler is None:
        log.warning("text_received_without_handler")
        return
    assert message.text is not None  # guaranteed by F.text
    chunks: list[str] = []
    async def emit(text: str) -> None:
        chunks.append(text)
    incoming = IncomingMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,  # B4: preserved
        text=message.text,
    )
    async with ChatActionSender.typing(bot=self._bot, chat_id=message.chat.id):
        await self._handler.handle(incoming, emit)
    full = "".join(chunks).strip() or "(пустой ответ)"
    for part in _split_for_telegram(full, limit=4096):
        await self._bot.send_message(message.chat.id, part)


def _split_for_telegram(text: str, *, limit: int = 4096) -> list[str]:
    """Split on \\n\\n if possible, else \\n, else hard-cut at `limit`."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit)
        if cut < 0:
            cut = remaining.rfind("\n", 0, limit)
        if cut < 0:
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        out.append(remaining)
    return out
```

### 2.14 `skills/ping/SKILL.md` + `tools/ping/main.py`

```markdown
---
name: ping
description: Healthcheck skill. Runs the ping CLI which prints {"pong": true}. Use when the user says "use the ping skill" or asks to verify skill discovery.
allowed-tools: [Bash]
---

# ping

Run `python tools/ping/main.py` via Bash. The tool prints a single JSON line
`{"pong": true}`. Report the parsed value back to the user.
```

```python
# tools/ping/main.py
import json
import sys

sys.stdout.write(json.dumps({"pong": True}) + "\n")
```

---

## 3. Step-by-step execution recipe

All paths absolute from `/Users/agent2/Documents/0xone-assistant/`. Phase 1
shipped state is intact (`src/assistant/`, `tests/test_db.py`). Wave-2 + R13
spike probes live in `plan/phase2/spikes/`.

### 3.1 Sync dependencies

```bash
uv add "claude-agent-sdk>=0.1.59,<0.2" "pyyaml>=6.0"
uv add --dev "types-pyyaml>=6.0"
uv sync
```

Expected: `pyproject.toml` + `uv.lock` updated (commit both).

### 3.2 Owner migration script (phase 1 → phase 2)

Phase 1 kept `.env` and `data/assistant.db` in the repo. Phase 2 moves both
out. Owner runs ONCE before first phase-2 start:

```bash
mkdir -p ~/.config/0xone-assistant ~/.local/share/0xone-assistant

[ -f .env ] && mv .env ~/.config/0xone-assistant/.env || true
for f in data/assistant.db data/assistant.db-wal data/assistant.db-shm; do
    [ -f "$f" ] && mv "$f" ~/.local/share/0xone-assistant/ || true
done
rmdir data 2>/dev/null || true
```

Optional: persist as `scripts/migrate-phase1-to-phase2.sh`. Document in
README "Upgrading from phase 1".

### 3.3 Write / update files

Follow §2 sections in order:

1. `src/assistant/state/migrations/0002_turns_block_type.sql` — §2.1 (reference only).
2. `src/assistant/state/db.py` — §2.2 (statement-by-statement runner).
3. `src/assistant/state/conversations.py` — §2.3 (Turn API + turn-limit + orphan cleanup).
4. `src/assistant/bridge/__init__.py` — empty.
5. `src/assistant/bridge/hooks.py` — §2.4 (7 hooks, DNS-in-thread).
6. `src/assistant/bridge/bootstrap.py` — §2.8 (absolute symlink + settings hard-block on hooks/permissions).
7. `src/assistant/bridge/skills.py` — §2.7 (manifest + cache key includes count).
8. `src/assistant/bridge/system_prompt.md` — §2.9.
9. `src/assistant/bridge/history.py` — §2.6 (S7/B5 — full assistant replay).
10. `src/assistant/bridge/claude.py` — §2.5 (S2 — model from AssistantMessage).
11. `src/assistant/handlers/message.py` — §2.10 (B5 — ToolResult role='user').
12. `src/assistant/config.py` — §2.11 (XDG + N2 validator).
13. `src/assistant/main.py` — §2.12 (B3 — preserve phase-1 main supervision; only change Daemon class body).
    **Coder instruction verbatim:** Rewrite the `Daemon` class. Do NOT touch
    `main()` except to pass `settings` into `Daemon(settings)`. All signal
    handling, `stop_event`, polling supervision, and `polling_exc` re-raise
    code from phase-1 stays.
14. `src/assistant/adapters/base.py` + `adapters/telegram.py` — §2.13 (B4 — keep MessengerAdapter ABC + message_id).
15. `skills/ping/SKILL.md` + `tools/ping/main.py` — §2.14.
16. `.env.example` — add `CLAUDE_*` block. **N6 note:** add comment
    pointing to `~/.config/0xone-assistant/.env` as the post-migration location.
17. `.gitignore` — add `.claude/skills`, `.claude/settings.json`,
    `.claude/settings.local.json`, `plan/phase2/spikes/*.json`.
18. `README.md` — "Upgrading from phase 1" + "Where do .env and data/ live?"
    + Security considerations note on WebFetch TOCTOU residual (U9).

### 3.4 Tests

**Migration test discipline (v2.1):** All migration tests ALWAYS go through
`apply_schema(conn)`, never `_apply_0002(conn)` directly. The B1 early-exit
guard changes direct-call behaviour and tests should not rely on it.

**Core (6 pass):**

- `tests/test_skills_manifest.py`
- `tests/test_skills_manifest_cache.py` (mtime-max + file-count, Q9 / S5)
- `tests/test_bootstrap.py` (idempotent symlink with absolute target, S4)
- `tests/test_bridge_mock.py` (monkeypatch `query`; assert envelope shape +
  block emission + ResultMessage last)
- `tests/test_load_recent_turn_limit.py` (turn-limit semantics, B6)
- `tests/test_interrupted_turn_skipped.py`

**Wave-2 spike regressions (4 pass):**

- `tests/test_bash_hook_bypass.py` — 36 cases from R8 + 3 new multi-arg cat cases:
  - `"cat README.md .env"` → deny (B7)
  - `"cat a b ../../etc/passwd"` → deny (B7 + relative path)
  - `"cat README.md pyproject.toml"` → allow
- `tests/test_webfetch_ssrf.py` — string-only + DNS-mocked cases (R9) +
  `asyncio.to_thread` runs without blocking loop (B10). Use
  `unittest.mock.patch('socket.getaddrinfo')`.
- `tests/test_migration_0002_crash.py` — 3 scenarios from R11 (happy /
  crash-rollback / re-run). All via `apply_schema(conn)`, not direct
  `_apply_0002`. Scenario 3 now explicitly tests that v=2 DB with production
  data (row with `block_type='tool_use'`) is NOT stomped on re-run (B1).
- `tests/test_history_null_block_type.py` — R12 defence.

**New v2.1 regression tests:**

- `tests/test_history_assistant_replay.py` — R13/S7 fix:
  insert rows for a completed user→assistant→user(new) history;
  `history_to_sdk_envelopes` → stream of 3 envelopes (user, assistant, user)
  in order, each with correct `type` field. Hermetic (no SDK call).
- `tests/test_bash_cat_multi_arg.py` — B7 (covered inside
  `test_bash_hook_bypass.py` but one explicit regression here helps grep-ability).
- `tests/test_file_hook_relative_path.py` — B8: `Read("../../etc/passwd")`
  must deny. Absolute `/etc/passwd` must deny. `Read("relative/in_root.py")`
  must allow.
- `tests/test_settings_block_hooks.py` — S15: `.claude/settings.json`
  containing `{"hooks": {}}` → `assert_no_custom_claude_settings` calls
  `sys.exit(3)`. Cosmetic key only → returns normally.
- `tests/test_orphan_pending_turn_cleanup.py` — S10: insert row with
  `status='pending'`, call `cleanup_orphan_pending_turns`, assert it becomes
  `'interrupted'`.
- `tests/test_timeout_cleanup.py` — S9 add-only (not source-modified): use
  `anyio.sleep_forever()` inside a fake query generator, assert
  `asyncio.timeout` cleans up correctly and raises `ClaudeBridgeError("timeout")`.

**Manual-verification (xfail + live, not in CI):**

- `tests/test_u2_cross_session_thinking_rejected_xfail.py` — ThinkingBlock replay.
- `tests/test_u3_symlink_skill_discovery.py` — requires `requires_claude_cli` marker.
- `tests/test_u5_hookmatcher_regex_xfail.py` — ditto.

**Deleted:** `test_u1_tool_block_roundtrip_xfail.py` — R13 resolved it (assistant
envelope replay works). The synthetic-note fallback that existed to mitigate
U1 is gone; replace with live multi-turn smoke from §3.6 step 4bis.

Expected CI: 1 phase-1 (test_db) + 6 core + 4 wave-2 + 6 v2.1 regression =
**17 passed**; 3 skipped (requires_claude_cli). `just lint` (ruff + mypy
strict) green.

### 3.5 Lint + test

```bash
uv run ruff format .
just lint
just test
```

### 3.6 Manual smoke (owner runs)

1. `~/.config/0xone-assistant/.env` populated.
2. `claude --print ping` returns without prompting (auth intact). If not:
   `claude login`.
3. `just run` → JSON logs:
   - `auth_preflight_ok`
   - `orphan_turns_cleaned` (0 on first run, >0 if prior crash)
   - `daemon_started`
   - On first message: `sdk_init` with `skills: ['ping']`
4. Telegram: `"use the ping skill"` → model → Bash `python tools/ping/main.py`
   → `{"pong": true}` → bot replies "pong" / `true`.
4bis. **Multi-turn continuity (new, R13-gated).** From Telegram:
   - Turn 1: `"Remember the magic number 777333 for me"` → bot acknowledges.
   - Turn 2: `"What magic number did I give you?"` → bot replies with `777333`.
   - Confirms `history_to_sdk_envelopes` round-trips assistant state.
5. Security — Bash:
   - `"run 'cat .env'"` → slip_guard deny.
   - `"run 'env | grep TOKEN'"` → allowlist deny.
   - `"run 'wc -l README.md'"` → allowlist deny (wc not whitelisted).
   - `"run 'cat README.md'"` → allowed.
   - **B7 new:** `"run 'cat README.md .env'"` → deny (second arg outside allow).
6. Security — File tool: `"read /etc/passwd"` via Read → file_hook deny.
   **B8 new:** `"read ../../etc/passwd"` → deny (relative resolved).
7. Security — WebFetch: `"fetch http://169.254.169.254/latest/meta-data/"`
   → webfetch_hook deny (literal). `"fetch http://localhost"` → deny.
8. `sqlite3 ~/.local/share/0xone-assistant/assistant.db 'PRAGMA user_version'`
   → `2`.
9. `SELECT status, COUNT(*) FROM turns GROUP BY status` → `complete|N`
   (no `pending` after bot has been running).
10. Long response (>4096 chars) split into 2+ Telegram messages.
11. `ls -la .claude/skills` shows absolute symlink target ending in
    `/0xone-assistant/skills`.
12. Cost check: `result_received` log entries show `cache_read > 0` after
    the 2nd turn — confirms R7 prompt caching works in production.
13. **S15 check:** if `.claude/settings.json` has `{"hooks": {}}`, bot
    fails fast with `claude_settings_conflict` log + exit 3.

---

## 4. Known gotchas (wave-2 + fix-pack additions on top of spike-findings §3)

1. **Prompt caching invalidates on manifest change (R7).** Every time a new
   skill is installed, `build_manifest` returns new text → system prompt
   changes → cache_creation for the next ~5700 tokens. Acceptable. Do NOT
   defeat the mtime cache in an attempt to "always rebuild".
2. **`session_id` in envelopes is cosmetic (R10).** Anyone reading the code
   might assume SDK honors it for continuity. Comment in
   `history_to_sdk_envelopes` explains this so it isn't a trap.
3. **executescript() commits the outer BEGIN EXCLUSIVE implicitly (B2).**
   sqlite3 docs: "If there is a pending transaction, an implicit COMMIT
   statement is executed first." → Migrations MUST use per-statement
   `conn.execute(...)`, not `conn.executescript(...)` inside a
   `BEGIN EXCLUSIVE`. Our `_apply_0002` is statement-by-statement for this
   reason.
4. **WebFetch DNS lookup runs in thread pool (B10).** `asyncio.to_thread`
   prevents ~5s event-loop freeze per call. Thread pool size is default (32
   threads on CPython 3.12+); single-user bot won't saturate.
5. **R12 defensive NULL handling.** `btype or "text"` is purely
   defense-in-depth. If you see `block_type IS NULL` in production, it means
   the migration backfill missed something — investigate, don't just rely
   on the default.
6. **`asyncio.timeout` around streaming-input `query()`**: when the timeout
   fires mid-stream, the prompt_stream async-gen is closed via `aclose()`.
   SDK's `client.py:162` `finally: await query.close()` handles shutdown.
   `ClaudeBridgeError("timeout")` is raised cleanly.
7. **`HookMatcher(matcher="Bash")` case-sensitive.** Spike used exact match.
   SDK tool names arrive as PascalCase (`Bash`, `Read`, `WebFetch`). Do not
   lowercase.
8. **`system_prompt=str`** in `ClaudeAgentOptions` — plain string works.
   `SystemPromptPreset`/`SystemPromptFile` variants also exist but we don't use.
9. **`UserMessage.content`** accepts `str` OR `list[ContentBlock]`. In
   `history_to_sdk_envelopes` we send `str` when a single text block on a
   user envelope; `list` when multi-block. See §2.6.
10. **`ThinkingBlock.signature`** is opaque. Store as-is; never parse.
11. **SQLite FK omitted in migration 0002.** detailed-plan §7a described a
    full recreate path including FK; we pragmatically ship without. Rationale
    in spike-findings §R11 "note on FK".
12. **Aiogram's `ChatActionSender.typing`** re-sends every ~5s while the
    async-context is open. Bridge turns of 30–60s (with tool calls) stay
    "typing..." the whole time.
13. **Spikes dir IS in the repo** (`plan/phase2/spikes/`) as reference; their
    generated `.json` reports are git-ignored.
14. **OAuth reminder:** never add `ANTHROPIC_API_KEY` to `.env.example` or
    `ClaudeSettings`. If code review finds it — remove. The bridge uses the
    `claude` CLI session under `~/.claude/`; subprocesses inherit the user's
    env (no explicit key needed).
15. **S3 (TOCTOU symlink):** between `file_hook` resolving a path and the
    SDK's CLI actually opening the file, a rogue symlink swap could escape
    the sandbox. Documented as acceptable single-user risk; phase-6 hardening
    can close it with `os.open(..., O_NOFOLLOW)` around the actual file I/O.
16. **S14 (`parse_mode=None`) migration UX:** existing Telegram chats may
    display legacy HTML-formatted turns correctly, but new turns from phase
    2 will show raw `*markdown*` characters. Owner can optionally clear
    history (`/start` on the bot) — not required.
17. **`ResultMessage.model` does not exist (S2).** SDK 0.1.59 types.py shows
    `usage, total_cost_usd, stop_reason, duration_ms, num_turns,
    session_id` — no `model`. Capture from `AssistantMessage.model` inside
    the loop before ResultMessage arrives.
18. **Assistant envelope replay IS honored (R13).** `history_to_sdk_envelopes`
    emits both user AND assistant envelopes verbatim; SDK accepts and model
    sees prior assistant turns. Sentinel probe `plan/phase2/spikes/r13_*.json`
    confirms.

---

## 5. Citations

- **R13 spike artefact:** `/Users/agent2/Documents/0xone-assistant/plan/phase2/spikes/r13_assistant_envelope_replay.py`
  + `.json`. Live OAuth run, sentinel differential.
- **Wave-2 spike artefacts:** `plan/phase2/spikes/r7_prompt_caching.py`,
  `r8_bash_bypass.py`, `r9_webfetch_ssrf.py`, `r10_session_id.py`,
  `r11_migration_crash_sim.py` + `.json` reports.
- **Wave-1 spike baseline:** `plan/phase2/spike-findings.md §§1–5`.
- **SDK source (installed 0.1.59):** `_internal/client.py`, `_internal/query.py`,
  `types.py` (ResultMessage field list — no `model`).
- **Python sqlite3 executescript implicit-COMMIT:**
  https://docs.python.org/3/library/sqlite3.html#sqlite3.Cursor.executescript
- **Claude Agent SDK docs:** https://docs.claude.com/en/api/agent-sdk/python
- **Claude Code hooks:** https://docs.claude.com/en/docs/claude-code/hooks
- **Anthropic prompt caching:** https://docs.claude.com/en/docs/build-with-claude/prompt-caching
- **Anthropic tools API (tool_result on user role):** https://docs.claude.com/en/docs/build-with-claude/tool-use — confirms ToolResult goes on user envelope, informs B5.
- **OWASP SSRF Prevention:** https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- **SQLite ALTER TABLE (recreate-table):** https://sqlite.org/lang_altertable.html#otheralter
- **SQLite `PRAGMA user_version`:** https://sqlite.org/pragma.html#pragma_user_version
- **Python `ipaddress` (RFC1918 etc.):** https://docs.python.org/3/library/ipaddress.html
- **Python `asyncio.timeout`:** https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout
- **Python `asyncio.to_thread`:** https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread
- **aiogram 3.26 ChatActionSender:** https://docs.aiogram.dev/en/latest/utils/chat_action.html
- **pydantic-settings 2.6 env_file list:** https://docs.pydantic.dev/latest/concepts/pydantic_settings/
