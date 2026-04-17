"""FTS5 index layer (stdlib `sqlite3`).

Schema: `notes(path PRIMARY KEY, title, tags, area, body, created, updated)`
mirrored into a `notes_fts` virtual table via triggers so FTS5 can use
`content='notes', content_rowid='rowid'` and give us rank-based ordering
without duplicating body text on write. `vault_lock(index_db)` wraps
every mutating operation behind a per-DB `fcntl.flock` (exclusive), plus
a one-shot runtime probe (`_ensure_lock_semantics_once`) that refuses to
continue on FSes where `flock` silently no-ops.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

_LOCK_PROBE_DONE = False
_LOCK_PROBE_SKIP_ENV = "ASSISTANT_SKIP_LOCK_PROBE"

_EXIT_FTS_FS_NO_LOCK = 5


def _probe_lock_semantics(lock_path: Path) -> bool:
    """Return True if advisory locks actually block on this filesystem.

    SMB / iCloud / Dropbox / some FUSE mounts make `fcntl.flock` a no-op
    (the second `LOCK_EX|LOCK_NB` acquire silently succeeds). On those
    FSes concurrent `memory write` from two chats (phase-5 scheduler +
    owner) would race at the SQLite layer and risk index corruption.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd1 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fd2 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False  # Both acquired exclusive — flock is a no-op.
        except BlockingIOError:
            return True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd1, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            fcntl.flock(fd2, fcntl.LOCK_UN)
        os.close(fd1)
        os.close(fd2)


def _ensure_lock_semantics_once(index_db: Path) -> None:
    """Cached startup probe. Exits with code 5 when flock is advisory-only."""
    global _LOCK_PROBE_DONE
    if _LOCK_PROBE_DONE:
        return
    if os.environ.get(_LOCK_PROBE_SKIP_ENV):
        _LOCK_PROBE_DONE = True
        return
    lock_path = Path(str(index_db) + ".lock")
    if not _probe_lock_semantics(lock_path):
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        "fcntl.flock is advisory-only on this filesystem. "
                        "Vault corruption likely on concurrent writes. "
                        "Move MEMORY_VAULT_DIR to a local POSIX FS "
                        "(APFS, ext4, ZFS, XFS). Override with "
                        f"{_LOCK_PROBE_SKIP_ENV}=1 only in CI where you "
                        "serialize memory writes externally."
                    ),
                    "lock_path": str(lock_path),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.exit(_EXIT_FTS_FS_NO_LOCK)
    _LOCK_PROBE_DONE = True


def _reset_lock_probe_cache() -> None:
    """Test hook — forces the probe to run again on next `_ensure_index`."""
    global _LOCK_PROBE_DONE
    _LOCK_PROBE_DONE = False


def _connect(index_db: Path) -> sqlite3.Connection:
    """Opinionated sqlite connection for the memory index.

    `timeout=5.0` is the python-level busy timeout; `PRAGMA busy_timeout`
    is redundant belt+suspenders in case a child connection inherits a
    different default. WAL gives us readers-don't-block-writers.
    """
    conn = sqlite3.connect(str(index_db), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _schema_sql(tokenizer: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS notes(
        path     TEXT PRIMARY KEY,
        title    TEXT NOT NULL,
        tags     TEXT,           -- JSON list
        area     TEXT,
        body     TEXT NOT NULL,
        created  TEXT NOT NULL,
        updated  TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
        path, title, tags, area, body,
        content='notes', content_rowid='rowid',
        tokenize='{tokenizer}'
    );
    CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
        INSERT INTO notes_fts(rowid,path,title,tags,area,body)
        VALUES (new.rowid,new.path,new.title,new.tags,new.area,new.body);
    END;
    CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts,rowid,path,title,tags,area,body)
        VALUES('delete',old.rowid,old.path,old.title,old.tags,old.area,old.body);
    END;
    CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
        INSERT INTO notes_fts(notes_fts,rowid,path,title,tags,area,body)
        VALUES('delete',old.rowid,old.path,old.title,old.tags,old.area,old.body);
        INSERT INTO notes_fts(rowid,path,title,tags,area,body)
        VALUES (new.rowid,new.path,new.title,new.tags,new.area,new.body);
    END;
    """


def ensure_index(index_db: Path, tokenizer: str) -> None:
    """Idempotent schema + runtime safety checks.

    Exits process with code 5 if the filesystem makes `fcntl.flock`
    advisory-only. Exception (`sqlite3.Error`) propagates to the caller
    so it can map to the FTS exit code.
    """
    index_db.parent.mkdir(parents=True, exist_ok=True)
    _ensure_lock_semantics_once(index_db)
    conn = _connect(index_db)
    try:
        conn.executescript(_schema_sql(tokenizer))
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def vault_lock(index_db: Path) -> Iterator[None]:
    """Serialise every mutating memory operation with `fcntl.flock(LOCK_EX)`.

    The lock file is `<index_db>.lock` — a separate path from the DB itself
    so WAL-mode internals are untouched. `LOCK_EX` is process-level on POSIX
    (held until the FD closes), which is the right granularity for a
    single-user CLI.
    """
    lock_path = Path(str(index_db) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# CRUD on the index
# ---------------------------------------------------------------------------


def _tags_to_text(tags: list[str]) -> str:
    """FTS5 tokenises on whitespace by default — serialise tags as a space-
    separated string so `search "family"` hits a note tagged `family`."""
    return " ".join(tags) if tags else ""


def upsert_index(
    index_db: Path,
    rel_path: str,
    title: str,
    tags: list[str],
    area: str | None,
    body: str,
    created: str,
    updated: str,
) -> None:
    """Insert-or-replace a note row. FTS5 triggers mirror into `notes_fts`.

    Path is the vault-relative POSIX-style string (`inbox/wife-birthday.md`).
    """
    conn = _connect(index_db)
    try:
        conn.execute(
            """
            INSERT INTO notes(path, title, tags, area, body, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                title=excluded.title,
                tags=excluded.tags,
                area=excluded.area,
                body=excluded.body,
                created=excluded.created,
                updated=excluded.updated
            """,
            (
                rel_path,
                title,
                _tags_to_text(tags),
                area or "",
                body,
                created,
                updated,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_from_index(index_db: Path, rel_path: str) -> bool:
    """Return True if a row was removed; False when the path was absent."""
    conn = _connect(index_db)
    try:
        cur = conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def search_index(
    index_db: Path,
    query: str,
    *,
    area: str | None = None,
    limit: int = 10,
) -> list[dict[str, object]]:
    """Run an FTS5 MATCH. Returns rank-ordered hits with a body snippet."""
    conn = _connect(index_db)
    try:
        where = "notes_fts MATCH ?"
        params: list[object] = [query]
        if area:
            where += " AND area = ?"
            params.append(area)
        cur = conn.execute(
            f"""
            SELECT path, title, tags, area,
                   snippet(notes_fts, 4, '<b>', '</b>', '...', 32) AS snippet
              FROM notes_fts
             WHERE {where}
             ORDER BY rank
             LIMIT ?
            """,
            (*params, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "path": r["path"],
            "title": r["title"],
            "tags": r["tags"].split() if r["tags"] else [],
            "area": r["area"] or None,
            "snippet": r["snippet"],
        }
        for r in rows
    ]


def list_index(index_db: Path, area: str | None = None) -> list[dict[str, object]]:
    """List notes from the index (used by `cmd_list` when it prefers the
    index over a FS rescan). For small vaults both are fine; the CLI uses
    the FS walk because Obsidian may have added files out-of-band.
    """
    conn = _connect(index_db)
    try:
        if area is not None:
            cur = conn.execute(
                "SELECT path, title, tags, area, created, updated FROM notes "
                "WHERE area = ? ORDER BY path",
                (area,),
            )
        else:
            cur = conn.execute(
                "SELECT path, title, tags, area, created, updated FROM notes ORDER BY path"
            )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "path": r["path"],
            "title": r["title"],
            "tags": r["tags"].split() if r["tags"] else [],
            "area": r["area"] or None,
            "created": r["created"],
            "updated": r["updated"],
        }
        for r in rows
    ]


def reindex_all(
    index_db: Path,
    notes: list[tuple[str, str, list[str], str | None, str, str, str]],
) -> int:
    """Wipe `notes` (FTS5 triggers cascade to `notes_fts`) and re-insert.

    `notes` tuples: `(rel_path, title, tags, area, body, created, updated)`.
    Returns the number of rows written. Caller holds `vault_lock` — we
    use `BEGIN IMMEDIATE` so concurrent readers can't see the empty gap.
    """
    conn = _connect(index_db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM notes")
        count = 0
        for rel_path, title, tags, area, body, created, updated in notes:
            conn.execute(
                "INSERT INTO notes(path, title, tags, area, body, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rel_path, title, _tags_to_text(tags), area or "", body, created, updated),
            )
            count += 1
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
