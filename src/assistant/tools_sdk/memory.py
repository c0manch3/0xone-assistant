"""Long-term memory MCP server — 6 ``@tool`` functions backing the
single-user memory vault.

Each handler delegates to :mod:`assistant.tools_sdk._memory_core`
helpers after argument validation. Model-facing errors use the
``(code=N)`` suffix convention from :func:`core.tool_error`.

The module exposes two constants wired into
:class:`assistant.bridge.claude.ClaudeBridge` at init:

- :data:`MEMORY_SERVER` — the :func:`create_sdk_mcp_server` record.
- :data:`MEMORY_TOOL_NAMES` — tuple of fully-qualified
  ``mcp__memory__*`` names the model will see in ``allowed_tools``.

Configuration (``vault_dir`` / ``index_db_path`` / ``max_body_bytes``)
lives in the module-level ``_CTX`` dict, populated by
:func:`configure_memory` during ``Daemon.start()``. Mirrors the
installer pattern.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import structlog
import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.tools_sdk import _memory_core as core

# Fix 11 / OPS-14: use structlog so memory log lines travel the same
# JSON-rendering pipeline as the rest of the daemon. Keeping stdlib
# ``logging`` here would emit plain-text lines that the operator
# cannot grep for ``"event": "memory_*"`` reliably.
_log = structlog.get_logger(__name__)

# Fix 9 / QA M1: area names are top-level path segments — a single
# lowercase token. Rejecting arbitrary strings up-front prevents the
# model from passing ``/`` or ``..`` separators through to
# ``list_notes`` where they would simply return zero results with no
# hint that the area name was bogus.
_AREA_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# ---------------------------------------------------------------------------
# Module context (populated by configure_memory)
# ---------------------------------------------------------------------------
_CTX: dict[str, Any] = {}
_CONFIGURED: bool = False


def configure_memory(
    *,
    vault_dir: Path,
    index_db_path: Path,
    max_body_bytes: int = 1_048_576,
) -> None:
    """Initialise the memory subsystem.

    Idempotent with the same ``vault_dir`` + ``index_db_path``. Per
    H2.6: re-config with a NEW ``max_body_bytes`` is permitted (logs at
    WARNING) — owner env var changes must not brick daemon boot.
    Re-config with a different ``vault_dir`` or ``index_db_path`` raises
    :class:`RuntimeError` because the cached ``_CTX`` path would be out
    of sync with the on-disk state.
    """
    global _CONFIGURED
    if _CONFIGURED:
        cur = (_CTX.get("vault_dir"), _CTX.get("index_db_path"))
        new = (vault_dir, index_db_path)
        if cur != new:
            raise RuntimeError(
                "configure_memory re-called with different paths: "
                f"vault_dir={vault_dir} (was {_CTX.get('vault_dir')}), "
                f"index_db_path={index_db_path} (was {_CTX.get('index_db_path')})"
            )
        if _CTX.get("max_body_bytes") != max_body_bytes:
            _log.warning(
                "memory_max_body_bytes_changed",
                old=_CTX.get("max_body_bytes"),
                new=max_body_bytes,
            )
            _CTX["max_body_bytes"] = max_body_bytes
        return
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / ".tmp").mkdir(exist_ok=True)
    core._fs_type_check(vault_dir)
    core._ensure_index(index_db_path)
    _CTX.update(
        vault_dir=vault_dir,
        index_db_path=index_db_path,
        max_body_bytes=max_body_bytes,
    )
    _CONFIGURED = True
    core._maybe_auto_reindex(vault_dir, index_db_path)


def reset_memory_for_tests() -> None:
    """Test-only: clear the module state so successive tests can
    re-configure with fresh ``tmp_path`` fixtures.
    """
    global _CONFIGURED
    _CTX.clear()
    _CONFIGURED = False


def _need_ctx() -> tuple[Path, Path, int]:
    """Return ``(vault_dir, index_db_path, max_body_bytes)`` or raise."""
    try:
        return (
            _CTX["vault_dir"],
            _CTX["index_db_path"],
            int(_CTX["max_body_bytes"]),
        )
    except KeyError as exc:
        raise RuntimeError(
            "memory not configured; call configure_memory() first"
        ) from exc


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------
CODE_PATH = 1
CODE_NOT_FOUND = 2
CODE_VALIDATION = 3
CODE_IO = 4
CODE_FTS = 5
CODE_COLLISION = 6
CODE_AREA = 7
CODE_NOT_CONFIGURED = 8
CODE_LOCK = 9
CODE_NOT_CONFIRMED = 10


def _not_configured() -> dict[str, Any]:
    return core.tool_error("memory not configured", CODE_NOT_CONFIGURED)


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------
@tool(
    "memory_search",
    "Search saved memory notes via FTS5 with Russian morphology. "
    "Returns ranked hits with highlighted snippets.",
    {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Raw user text; handler tokenizes + stems Cyrillic.",
            },
            "area": {
                "type": "string",
                "description": "Optional top-level area filter (e.g. 'inbox', 'projects').",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "Max results; default 10.",
            },
        },
        "required": ["query"],
    },
)
async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    _vault, index_db, _mbb = _need_ctx()
    query = args.get("query", "")
    area_raw = args.get("area")
    area = str(area_raw) if area_raw else None
    limit_raw = args.get("limit", 10)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))
    try:
        fts_query = core._build_fts_query(query)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_FTS)
    try:
        hits = await asyncio.to_thread(
            core.search_notes, index_db, fts_query, area, limit
        )
    except sqlite3.OperationalError as exc:
        return core.tool_error(f"FTS5 error: {exc}", CODE_FTS)
    wrapped_hits: list[dict[str, Any]] = []
    lines = [f"Found {len(hits)} notes:"]
    for hit in hits:
        wrapped_snip, _nonce = core.wrap_untrusted(
            str(hit.get("snippet") or ""), "untrusted-note-snippet"
        )
        wrapped_hits.append({**hit, "snippet": wrapped_snip})
        lines.append(
            f"- {hit['path']} ({hit['title']}): {wrapped_snip}"
        )
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "hits": wrapped_hits,
    }


# ---------------------------------------------------------------------------
# memory_read
# ---------------------------------------------------------------------------
@tool(
    "memory_read",
    "Read a memory note by vault-relative path.",
    {"path": str},
)
async def memory_read(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    vault, _index_db, _mbb = _need_ctx()
    path_arg = args.get("path", "")
    try:
        full = core.validate_path(path_arg, vault)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_PATH)
    if not full.is_file():
        return core.tool_error("note not found", CODE_NOT_FOUND)
    try:
        raw = await asyncio.to_thread(full.read_text, encoding="utf-8")
    except OSError as exc:
        return core.tool_error(f"read error: {exc}", CODE_IO)
    try:
        fm, body = core.parse_frontmatter(raw)
    except ValueError as exc:
        return core.tool_error(f"frontmatter: {exc}", CODE_VALIDATION)
    rel = Path(path_arg)
    title_raw = fm.get("title")
    title = (
        str(title_raw).strip()
        if title_raw
        else core._title_from_body_or_stem(body, rel)
    )
    wikilinks = core.extract_wikilinks(body)
    wrapped_body, _nonce = core.wrap_untrusted(body, "untrusted-note-body")
    text = f"Title: {title}\n{wrapped_body}"
    allowed_fm_keys = {"title", "tags", "area", "created", "updated"}
    safe_fm: dict[str, Any] = {
        k: v for k, v in fm.items() if k in allowed_fm_keys
    }
    # Coerce tags to list[str] for JSON safety; preserve other keys.
    tags_val = safe_fm.get("tags")
    if tags_val is None:
        safe_fm["tags"] = []
    elif isinstance(tags_val, list):
        safe_fm["tags"] = [str(t) for t in tags_val]
    else:
        safe_fm["tags"] = [str(tags_val)]
    safe_fm["title"] = title
    return {
        "content": [{"type": "text", "text": text}],
        "frontmatter": safe_fm,
        "body": body,
        "wikilinks": wikilinks,
    }


# ---------------------------------------------------------------------------
# memory_write
# ---------------------------------------------------------------------------
@tool(
    "memory_write",
    "Persist a memory note to the vault with YAML frontmatter.",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Vault-relative path ending in .md (e.g. 'inbox/birthday.md').",
            },
            "title": {"type": "string"},
            "body": {
                "type": "string",
                "description": "Markdown body; default cap 1 MiB (env-override).",
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "area": {
                "type": "string",
                "description": "Top-level area; inferred from path if omitted.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "If true, replace existing note. Default false.",
            },
        },
        "required": ["path", "title", "body"],
    },
)
async def memory_write(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    vault, index_db, max_body_bytes = _need_ctx()
    path_arg = args.get("path", "")
    try:
        full = core.validate_path(path_arg, vault)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_PATH)
    try:
        clean_body = core.sanitize_body(args.get("body", ""), max_body_bytes)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_VALIDATION)
    if full.exists() and not bool(args.get("overwrite", False)):
        return core.tool_error(
            "note exists; pass overwrite=true to replace", CODE_COLLISION
        )
    rel = Path(path_arg)
    path_area = rel.parts[0] if len(rel.parts) > 1 else ""
    user_area = args.get("area")
    if user_area and user_area != path_area:
        return core.tool_error(
            f"area {user_area!r} conflicts with path prefix {path_area!r}",
            CODE_AREA,
        )
    title_raw = args.get("title", "")
    if not isinstance(title_raw, str) or not title_raw.strip():
        return core.tool_error("title must be a non-empty string", CODE_VALIDATION)
    title = title_raw.strip()
    tags_in = args.get("tags") or []
    if not isinstance(tags_in, list):
        return core.tool_error("tags must be an array of strings", CODE_VALIDATION)
    tags = [str(t) for t in tags_in]
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    # Fix 3 / H4-W3: ``created`` is NOT a model-controlled field. The
    # schema doesn't advertise it, but a prior implementation read it
    # from ``args`` — allowing a crafted note to pin ``updated`` to
    # ``"9999-99-99"`` and always-sort first in ``memory_list``. The
    # handler always stamps it server-side.
    #
    # Fix 4 / H3: on overwrite=true preserve the original note's
    # ``created`` timestamp — otherwise every edit loses the birthday
    # of the note and Obsidian's sort-by-created metadata lies.
    preserved_created: str | None = None
    if full.exists():
        try:
            existing_raw = full.read_text(encoding="utf-8")
            existing_fm, _ = core.parse_frontmatter(existing_raw)
            cand = existing_fm.get("created")
            if cand is not None:
                preserved_created = str(cand)
        except (OSError, ValueError):
            preserved_created = None
    created = preserved_created or now_iso
    fm: dict[str, Any] = {
        "title": title,
        "tags": tags,
        "area": path_area,
        "created": created,
        "updated": now_iso,
    }
    try:
        content = core.serialize_frontmatter(fm, clean_body)
    except yaml.YAMLError as exc:
        return core.tool_error(f"frontmatter serialize: {exc}", CODE_VALIDATION)
    tags_json = json.dumps(tags, ensure_ascii=False)
    row = (
        str(rel),
        title,
        tags_json,
        path_area,
        clean_body,
        created,
        now_iso,
    )
    try:
        await asyncio.to_thread(
            core.write_note_tx,
            full,
            rel,
            row,
            content,
            vault,
            index_db,
        )
    except TimeoutError:
        return core.tool_error("lock contention", CODE_LOCK)
    except sqlite3.OperationalError as exc:
        return core.tool_error(f"index: {exc}", CODE_FTS)
    except OSError as exc:
        return core.tool_error(f"vault io: {exc}", CODE_IO)
    return {
        "content": [{"type": "text", "text": f"saved {rel}"}],
        "path": str(rel),
        "title": title,
        "area": path_area,
        "bytes": len(clean_body.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# memory_list
# ---------------------------------------------------------------------------
@tool(
    "memory_list",
    "List saved notes, optionally filtered by top-level area.",
    {
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": "Optional top-level area filter.",
            },
        },
        "required": [],
    },
)
async def memory_list(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    _vault, index_db, _mbb = _need_ctx()
    area_raw = args.get("area")
    area = str(area_raw) if area_raw else None
    # Fix 9 / QA M1: enforce the area-name regex up-front so invalid
    # input (``INVALID!``, ``../``, UPPERCASE) returns a clear error
    # instead of silently yielding an empty result set.
    if area is not None and not _AREA_RE.match(area):
        return core.tool_error("invalid area name", CODE_AREA)
    try:
        rows, total = await asyncio.to_thread(
            core.list_notes, index_db, area, limit=100, offset=0
        )
    except sqlite3.OperationalError as exc:
        return core.tool_error(f"index: {exc}", CODE_FTS)
    lines = [f"{len(rows)} / {total} notes:"]
    for r in rows:
        lines.append(
            f"- {r['path']} - {r['title']} [{r.get('area') or '-'}]"
        )
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "notes": rows,
        "count": len(rows),
        "total": total,
    }


# ---------------------------------------------------------------------------
# memory_delete
# ---------------------------------------------------------------------------
@tool(
    "memory_delete",
    "Hard-delete a memory note. Requires confirmed=true.",
    {"path": str, "confirmed": bool},
)
async def memory_delete(args: dict[str, Any]) -> dict[str, Any]:
    if not _CONFIGURED:
        return _not_configured()
    vault, index_db, _mbb = _need_ctx()
    # H2.5: path validation FIRST, confirmation check SECOND.
    try:
        full = core.validate_path(args.get("path", ""), vault)
    except ValueError as exc:
        return core.tool_error(str(exc), CODE_PATH)
    if not bool(args.get("confirmed", False)):
        return core.tool_error("set confirmed=true to delete", CODE_NOT_CONFIRMED)
    if not full.is_file():
        return core.tool_error("note not found", CODE_NOT_FOUND)
    rel_path = args["path"]
    try:
        await asyncio.to_thread(
            core.delete_note_tx, full, rel_path, vault, index_db
        )
    except TimeoutError:
        return core.tool_error("lock contention", CODE_LOCK)
    except sqlite3.OperationalError as exc:
        return core.tool_error(f"index: {exc}", CODE_FTS)
    except OSError as exc:
        return core.tool_error(f"vault io: {exc}", CODE_IO)
    return {
        "content": [{"type": "text", "text": f"removed {rel_path}"}],
        "removed": True,
        "path": rel_path,
    }


# ---------------------------------------------------------------------------
# memory_reindex
# ---------------------------------------------------------------------------
@tool(
    "memory_reindex",
    "Rebuild the FTS5 index from disk. Disaster recovery.",
    {},
)
async def memory_reindex(args: dict[str, Any]) -> dict[str, Any]:
    del args
    if not _CONFIGURED:
        return _not_configured()
    vault, index_db, _mbb = _need_ctx()
    try:
        n = await asyncio.to_thread(
            core.reindex_under_lock, vault, index_db
        )
    except TimeoutError:
        return core.tool_error("lock contention", CODE_LOCK)
    except sqlite3.OperationalError as exc:
        return core.tool_error(f"index: {exc}", CODE_FTS)
    return {
        "content": [{"type": "text", "text": f"reindexed {n} notes"}],
        "reindexed": n,
    }


# ---------------------------------------------------------------------------
# MCP server + canonical tool name tuple
# ---------------------------------------------------------------------------
MEMORY_SERVER = create_sdk_mcp_server(
    name="memory",
    version="0.1.0",
    tools=[
        memory_search,
        memory_read,
        memory_write,
        memory_list,
        memory_delete,
        memory_reindex,
    ],
)

MEMORY_TOOL_NAMES: tuple[str, ...] = (
    "mcp__memory__memory_search",
    "mcp__memory__memory_read",
    "mcp__memory__memory_write",
    "mcp__memory__memory_list",
    "mcp__memory__memory_delete",
    "mcp__memory__memory_reindex",
)
