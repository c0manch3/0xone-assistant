"""memory CLI — Obsidian-compatible long-term memory for 0xone-assistant.

Stdlib-only (+ `yaml` from the main venv, phase-2 dep). Run via
`python tools/memory/main.py <subcommand> ...`. Every subcommand prints
a single JSON line on stdout; errors go to stderr as `{"ok": false, ...}`.

Exit codes:
  0  ok
  2  usage (argparse / user error)
  3  validation (frontmatter / path / body-size)
  4  I/O (vault dir missing, permissions, etc.)
  5  FTS5 / filesystem advisory-lock failure
  6  collision (write existing path without --overwrite)
  7  not-found (read / delete on missing path)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Put this tool's `_lib/` on sys.path so subcommand modules can import it
# without making `tools/memory/` a python package (keeps stdlib-only).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _memlib.frontmatter import (  # noqa: E402 — sys.path tweak above
    FrontmatterError,
    extract_wikilinks,
    parse_note,
    sanitize_body,
    serialize_note,
)
from _memlib.fts import (  # noqa: E402
    delete_from_index,
    ensure_index,
    reindex_all,
    search_index,
    upsert_index,
    vault_lock,
)
from _memlib.paths import (  # noqa: E402
    PathValidationError,
    validate_rel_path,
)
from _memlib.vault import (  # noqa: E402
    atomic_write,
    delete_note,
    ensure_vault,
    list_notes,
    read_note,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VAL = 3
EXIT_IO = 4
EXIT_FTS = 5
EXIT_COLL = 6
EXIT_NOT_FOUND = 7

_DEFAULT_SEARCH_LIMIT = 10
_MAX_BODY_BYTES_DEFAULT = 1_048_576


# ---------------------------------------------------------------------------
# Config resolution (env-driven; stdlib-only — no pydantic import)
# ---------------------------------------------------------------------------


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _resolve_vault_dir() -> Path:
    override = os.environ.get("MEMORY_VAULT_DIR")
    if override:
        return Path(override).expanduser()
    return _default_data_dir() / "vault"


def _resolve_index_path() -> Path:
    override = os.environ.get("MEMORY_INDEX_DB_PATH")
    if override:
        return Path(override).expanduser()
    return _default_data_dir() / "memory-index.db"


def _resolve_tokenizer() -> str:
    return os.environ.get("MEMORY_FTS_TOKENIZER") or "porter unicode61 remove_diacritics 2"


def _resolve_max_body_bytes() -> int:
    raw = os.environ.get("MEMORY_MAX_BODY_BYTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _MAX_BODY_BYTES_DEFAULT


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _ok(data: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=False) + "\n")
    return EXIT_OK


def _fail(code: int, error: str, **extra: Any) -> int:
    payload = {"ok": False, "error": error}
    payload.update(extra)
    sys.stderr.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    index = _resolve_index_path()
    tokenizer = _resolve_tokenizer()

    for w in ensure_vault(vault):
        sys.stderr.write(f"[memory] {w}\n")
    try:
        ensure_index(index, tokenizer)
    except OSError as exc:
        return _fail(EXIT_IO, f"could not open index: {exc}")

    limit = args.limit if args.limit and args.limit > 0 else _DEFAULT_SEARCH_LIMIT
    try:
        hits = search_index(index, args.query, area=args.area, limit=limit)
    except Exception as exc:  # FTS5 syntax errors surface here.
        return _fail(EXIT_FTS, f"FTS5 query failed: {exc}", query=args.query)
    return _ok({"query": args.query, "area": args.area, "hits": hits})


def cmd_read(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    try:
        rel = validate_rel_path(args.path)
    except PathValidationError as exc:
        return _fail(EXIT_VAL, str(exc), path=args.path)

    if not (vault / rel).exists():
        return _fail(EXIT_NOT_FOUND, "note not found", path=str(rel))
    try:
        text = read_note(vault, rel)
    except OSError as exc:
        return _fail(EXIT_IO, f"read failed: {exc}", path=str(rel))
    try:
        fm, body = parse_note(text)
    except FrontmatterError as exc:
        return _fail(EXIT_VAL, f"invalid frontmatter: {exc}", path=str(rel))

    return _ok(
        {
            "path": str(rel),
            "frontmatter": fm,
            "body": body,
            "wikilinks": extract_wikilinks(body),
        }
    )


def cmd_write(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    index = _resolve_index_path()
    tokenizer = _resolve_tokenizer()
    max_body = _resolve_max_body_bytes()

    try:
        rel = validate_rel_path(args.path)
    except PathValidationError as exc:
        return _fail(EXIT_VAL, str(exc), path=args.path)

    if args.title is None or not args.title.strip():
        return _fail(EXIT_VAL, "--title is required and must be non-empty")

    if args.body != "-":
        return _fail(
            EXIT_USAGE,
            "only --body - (stdin) is supported; pipe the body on stdin",
        )

    body_raw = sys.stdin.read()
    if len(body_raw.encode("utf-8")) > max_body:
        return _fail(
            EXIT_VAL,
            f"body exceeds MEMORY_MAX_BODY_BYTES ({max_body})",
            path=str(rel),
        )
    body = sanitize_body(body_raw)

    tags: list[str] = []
    if args.tags:
        tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    # Area from argv overrides directory-derived; if not given, infer
    # from the first path segment (common convention: `inbox/a.md`).
    inferred_area = rel.parts[0] if len(rel.parts) > 1 else None
    area = args.area or inferred_area

    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    frontmatter: dict[str, Any] = {
        "title": args.title.strip(),
        "tags": tags,
        "area": area,
        "created": created,
        "related": [],
    }
    content = serialize_note(frontmatter, body)

    for w in ensure_vault(vault):
        sys.stderr.write(f"[memory] {w}\n")
    try:
        ensure_index(index, tokenizer)
    except OSError as exc:
        return _fail(EXIT_IO, f"could not open index: {exc}")

    target = vault / rel
    if target.exists() and not args.overwrite:
        return _fail(
            EXIT_COLL,
            f"collision: {rel} exists; use --overwrite to replace",
            path=str(rel),
        )

    try:
        with vault_lock(index):
            atomic_write(vault, rel, content)
            upsert_index(
                index,
                str(rel.as_posix()),
                frontmatter["title"],
                tags,
                area,
                body,
                created,
                created,
            )
    except OSError as exc:
        return _fail(EXIT_IO, f"write failed: {exc}", path=str(rel))

    return _ok({"path": str(rel), "title": frontmatter["title"], "area": area})


def cmd_list(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    for w in ensure_vault(vault):
        sys.stderr.write(f"[memory] {w}\n")
    rels = list_notes(vault, args.area)
    entries: list[dict[str, Any]] = []
    for rel in rels:
        try:
            text = (vault / rel).read_text(encoding="utf-8")
            fm, _body = parse_note(text)
            entries.append(
                {
                    "path": str(rel),
                    "title": fm["title"],
                    "tags": fm["tags"],
                    "area": fm["area"],
                    "created": fm["created"],
                }
            )
        except (FrontmatterError, OSError):
            # Show the path but mark it as parse-failed so the model
            # doesn't silently lose notes it could manually inspect.
            entries.append(
                {
                    "path": str(rel),
                    "title": None,
                    "tags": [],
                    "area": None,
                    "created": None,
                    "parse_error": True,
                }
            )
    return _ok({"area": args.area, "notes": entries})


def cmd_delete(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    index = _resolve_index_path()
    tokenizer = _resolve_tokenizer()

    try:
        rel = validate_rel_path(args.path)
    except PathValidationError as exc:
        return _fail(EXIT_VAL, str(exc), path=args.path)

    for w in ensure_vault(vault):
        sys.stderr.write(f"[memory] {w}\n")
    try:
        ensure_index(index, tokenizer)
    except OSError as exc:
        return _fail(EXIT_IO, f"could not open index: {exc}")

    if not (vault / rel).exists():
        return _fail(EXIT_NOT_FOUND, "note not found", path=str(rel))

    try:
        with vault_lock(index):
            delete_note(vault, rel)
            delete_from_index(index, str(rel.as_posix()))
    except OSError as exc:
        return _fail(EXIT_IO, f"delete failed: {exc}", path=str(rel))

    return _ok({"path": str(rel), "deleted": True})


def cmd_reindex(args: argparse.Namespace) -> int:
    vault = _resolve_vault_dir()
    index = _resolve_index_path()
    tokenizer = _resolve_tokenizer()

    for w in ensure_vault(vault):
        sys.stderr.write(f"[memory] {w}\n")
    try:
        ensure_index(index, tokenizer)
    except OSError as exc:
        return _fail(EXIT_IO, f"could not open index: {exc}")

    to_write: list[tuple[str, str, list[str], str | None, str, str, str]] = []
    rels = list_notes(vault)
    parse_errors: list[dict[str, Any]] = []
    for rel in rels:
        try:
            text = (vault / rel).read_text(encoding="utf-8")
            fm, body = parse_note(text)
        except FrontmatterError as exc:
            parse_errors.append({"path": str(rel), "error": str(exc)})
            continue
        except OSError as exc:
            parse_errors.append({"path": str(rel), "error": str(exc)})
            continue
        created = fm.get("created") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        to_write.append(
            (
                str(rel.as_posix()),
                fm["title"],
                fm["tags"],
                fm["area"],
                body,
                created,
                created,
            )
        )

    try:
        with vault_lock(index):
            count = reindex_all(index, to_write)
    except Exception as exc:  # pragma: no cover — bubbled as EXIT_FTS
        return _fail(EXIT_FTS, f"reindex failed: {exc}")

    return _ok(
        {
            "reindexed": count,
            "parse_errors": parse_errors,
        }
    )


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory", description="0xone-assistant memory CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_search = sub.add_parser("search", help="FTS5 search")
    sp_search.add_argument("query")
    sp_search.add_argument("--area", default=None)
    sp_search.add_argument("--limit", type=int, default=_DEFAULT_SEARCH_LIMIT)
    sp_search.set_defaults(func=cmd_search)

    sp_read = sub.add_parser("read", help="read a note by relative path")
    sp_read.add_argument("path")
    sp_read.set_defaults(func=cmd_read)

    sp_write = sub.add_parser("write", help="write a note (body on stdin)")
    sp_write.add_argument("path")
    sp_write.add_argument("--title", required=True)
    sp_write.add_argument("--tags", default=None, help="comma-separated tag list")
    sp_write.add_argument("--area", default=None)
    sp_write.add_argument("--body", required=True, help="must be '-' (stdin)")
    sp_write.add_argument("--overwrite", action="store_true")
    sp_write.set_defaults(func=cmd_write)

    sp_list = sub.add_parser("list", help="list notes")
    sp_list.add_argument("--area", default=None)
    sp_list.set_defaults(func=cmd_list)

    sp_del = sub.add_parser("delete", help="delete a note")
    sp_del.add_argument("path")
    sp_del.set_defaults(func=cmd_delete)

    sp_rx = sub.add_parser("reindex", help="wipe + rebuild the FTS5 index from the vault")
    sp_rx.set_defaults(func=cmd_reindex)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
