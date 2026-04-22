"""Memory-tool shared helpers (index schema, atomic write, frontmatter,
FTS5 query builder, vault scanning, untrusted-content wrapping).

These are TRUSTED in-process helpers — not ``@tool``-decorated. They are
called from ``memory.py`` after the ``@tool`` handlers have validated
the arguments. The trust boundary is enforced at the ``@tool`` layer;
anything reaching these helpers has already been path-validated,
sanitized, and size-capped.

Module is organised in dependency order so later helpers can reference
earlier ones without forward declarations.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import Stemmer  # type: ignore[import-not-found]  # PyStemmer has no stubs
import structlog
import yaml

# ---------------------------------------------------------------------------
# Module-level constants and singletons
# ---------------------------------------------------------------------------
# Fix 11 / OPS-14: structlog.get_logger returns a BoundLogger that
# emits structured JSON through the same pipeline configured in
# assistant.logger.setup_logging. Using stdlib logging.getLogger here
# would bypass the JSONRenderer and produce plain-text lines that
# operators cannot reliably grep by event name.
_log = structlog.get_logger(__name__)

# M2.3: stemmer is expensive to construct; keep one at module scope.
# PyStemmer is thread-safe for stemWord calls against a single instance
# under CPython (it releases the GIL around the C-level snowball call).
_STEMMER = Stemmer.Stemmer("russian")

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_CYRILLIC_CHAR_RE = re.compile(r"[\u0400-\u04ff]")

# Matches any untrusted-note-{body,snippet} tag in either direction, with
# or without nonce suffix, case-insensitive. Used both for write-time
# rejection (sanitize_body) and read-time scrubbing (wrap_untrusted).
_SENTINEL_RE = re.compile(
    r"</?\s*untrusted-note-(?:body|snippet)(?:-[0-9a-f]+)?\s*>",
    re.IGNORECASE,
)

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

_VAULT_SCAN_EXCLUDES = frozenset(
    {".obsidian", ".tmp", ".git", ".trash", "__pycache__", ".DS_Store"}
)

_MAX_AUTO_REINDEX = 2000
_LOCK_TIMEOUT_SEC = 5.0
_FS_TYPE_CMD_TIMEOUT_SEC = 5.0

# Per R8 whitelist/blacklist; ``_fs_type_check`` warns on UNSAFE_FS or
# path-prefix matches.
_ALLOWED_FS = frozenset(
    {
        "apfs",
        "hfs",
        "hfsplus",
        "ufs",
        "ext2",
        "ext3",
        "ext4",
        "btrfs",
        "xfs",
        "tmpfs",
        "zfs",
    }
)
_UNSAFE_FS = frozenset(
    {
        "smbfs",
        "afpfs",
        "nfs",
        "nfs4",
        "cifs",
        "fuse",
        "osxfuse",
        "webdav",
        "msdos",
        "exfat",
        "vfat",
    }
)
_UNSAFE_PATH_PREFIXES = (
    "~/Library/Mobile Documents",
    "~/Library/CloudStorage",
    "~/Dropbox",
)

# Mount parser for Darwin (RQ3 + C2.1). Greedy ``.+`` on the mount point
# handles paths with spaces like ``/Volumes/Google Chrome``.
_DARWIN_MOUNT_RE = re.compile(r"^.+? on (.+) \(([a-z0-9]+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# SQL schema and prepared statements
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS notes (
  path    TEXT PRIMARY KEY,
  title   TEXT NOT NULL,
  tags    TEXT,
  area    TEXT,
  body    TEXT NOT NULL,
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  path, title, tags, area, body,
  content='notes', content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
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


# ---------------------------------------------------------------------------
# Error helper (mirrors installer's ``tool_error`` convention)
# ---------------------------------------------------------------------------
def tool_error(message: str, code: int) -> dict[str, Any]:
    """Return an MCP-shaped tool-error response.

    The model-facing surface is the formatted text containing
    ``(code=N)``. Extra dict keys (``error``, ``code``) are only
    surfaced to Python tests invoking ``.handler(...)`` directly
    (S1 caveat from installer).
    """
    return {
        "content": [{"type": "text", "text": f"error: {message} (code={code})"}],
        "is_error": True,
        "error": message,
        "code": code,
    }


# ---------------------------------------------------------------------------
# Index bootstrap
# ---------------------------------------------------------------------------
def _ensure_index(index_db_path: Path) -> None:
    """Create the index DB + FTS5 virtual table + triggers if absent.

    Idempotent; safe to call on every daemon boot. Uses
    ``CREATE ... IF NOT EXISTS`` throughout. Writes
    ``meta('schema_version', '1')`` on fresh DB so future migrations can
    detect the current layout.

    Fix 2 / H2: the index DB — along with its WAL/SHM side-cars —
    contains raw note bodies. Restrict to owner-only (``0o600``) so a
    second account on the host cannot read them.
    """
    index_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_db_path)
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
    finally:
        conn.close()
    # Tighten permissions on the DB and any WAL/SHM side-cars that
    # sqlite has already created. ``chmod`` on non-existent side-cars
    # is a no-op wrapped in ``contextlib.suppress`` since WAL may not
    # exist yet on a freshly-created index.
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(index_db_path) + suffix)
        if candidate.exists():
            with contextlib.suppress(OSError):
                os.chmod(candidate, 0o600)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------
def validate_path(rel: str, vault_root: Path) -> Path:
    """Resolve ``rel`` against ``vault_root`` and assert safety.

    Rejects (with :class:`ValueError`):
      - non-strings or empty strings
      - absolute paths / ``~``-prefixed paths
      - paths containing ``..``
      - non-``.md`` suffixes
      - MOC files (``_*.md`` — Obsidian index convention; H3)
      - paths whose first component is in ``_VAULT_SCAN_EXCLUDES`` or
        starts with ``.`` (Fix 5 / QA M2) — reindex would later
        silently skip these; writes to them produce "phantom" notes
        that exist on disk but never show up in search/list results.
      - paths that escape the vault root after ``.resolve()``
      - symlink targets (if the file exists and is a symlink)

    Returns the fully-resolved absolute path on success. Caller may
    ``.exists()``-check separately; the function does not require the
    target to exist (``memory_write`` creates new paths).
    """
    if not isinstance(rel, str) or not rel:
        raise ValueError("path must be a non-empty string")
    if rel.startswith("/") or rel.startswith("~"):
        raise ValueError("path must be vault-relative")
    p = Path(rel)
    if ".." in p.parts:
        raise ValueError("path may not contain '..'")
    if p.suffix != ".md":
        raise ValueError("path must end in '.md'")
    # H3: MOC files are Obsidian-maintained; do not let the model touch
    # them via read/write/delete.
    if p.name.startswith("_"):
        raise ValueError("MOC files (_*.md) are not accessible via memory tools")
    # Fix 5 / QA M2: reject writes into scan-excluded directories.
    # ``_vault_rel_eligible`` filters these out on reindex; without an
    # up-front reject, a write to ``inbox/.tmp/foo.md`` (accidental)
    # or ``.obsidian/foo.md`` (malicious) would succeed, never index,
    # and silently disappear from search and list results.
    if p.parts:
        first = p.parts[0]
        if first in _VAULT_SCAN_EXCLUDES or first.startswith("."):
            raise ValueError(
                f"path's first segment {first!r} is a reserved/excluded directory"
            )
    vault_resolved = vault_root.resolve()
    # Symlink check FIRST, pre-resolve: ``Path.resolve()`` follows symlinks
    # and turns ``vault/link.md`` → the real target, which might live
    # OUTSIDE the vault — masking the symlink rejection as a "path escapes"
    # error. Use lstat on the raw vault-relative candidate.
    raw = vault_resolved / p
    if raw.is_symlink():
        raise ValueError("symlink targets not permitted")
    full = raw.resolve()
    try:
        full.relative_to(vault_resolved)
    except ValueError as exc:
        raise ValueError("path escapes vault root") from exc
    return full


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
def atomic_write(dest: Path, content: str, *, tmp_dir: Path) -> None:
    """Write ``content`` to ``dest`` atomically via tmp + fsync + rename.

    ``content`` MUST be already-scrubbed (no lone surrogates). ``tmp_dir``
    must exist and be on the same filesystem as ``dest`` for
    ``os.replace`` to be atomic; ``configure_memory`` creates
    ``<vault>/.tmp/`` for this purpose (M2.2).
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=tmp_dir,
            prefix=".tmp-",
            suffix=".md",
            delete=False,
            encoding="utf-8",
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# YAML frontmatter parsing (IsoDateLoader per RQ4 + H2.4)
# ---------------------------------------------------------------------------
class IsoDateLoader(yaml.SafeLoader):
    """SafeLoader subclass that coerces bare dates/datetimes to ISO strings.

    Obsidian and hand-authored frontmatter routinely emit
    ``created: 2026-04-16`` — which yaml.SafeLoader parses as
    ``datetime.date``. Passing that to ``json.dumps`` crashes
    (``TypeError: Object of type date is not JSON serializable``). This
    loader intercepts the ``!!timestamp`` tag and returns an ISO-8601
    string instead.

    H2.4: malformed dates (``2026-13-99``, ``time``-only values) fall
    back to the raw scalar so parsing continues.
    """


def _timestamp_as_iso(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    # yaml's timestamp constructor expects a ScalarNode; for date/
    # timestamp YAML tags the resolver always produces one. If a caller
    # somehow wires this into a Mapping/Sequence node we fall back to
    # stringifying whatever .value is set.
    if not isinstance(node, yaml.ScalarNode):
        return str(getattr(node, "value", ""))
    try:
        val = yaml.SafeLoader.construct_yaml_timestamp(loader, node)
    except (ValueError, TypeError):
        return str(node.value)
    if isinstance(val, (dt.datetime, dt.date)):
        return val.isoformat()
    # Raw scalar (dt.time or other) — stringify for JSON safety.
    return str(val)


IsoDateLoader.add_constructor("tag:yaml.org,2002:timestamp", _timestamp_as_iso)


_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?(.*)$", re.DOTALL)


def parse_frontmatter(md_text: str) -> tuple[dict[str, Any], str]:
    """Split ``md_text`` into ``(frontmatter_dict, body_str)``.

    Returns ``({}, md_text)`` when the file has no YAML frontmatter.
    Raises :class:`ValueError` for a malformed YAML block or a
    non-mapping top-level node.
    """
    if not isinstance(md_text, str):
        raise ValueError("markdown text must be a string")
    m = _FRONTMATTER_RE.match(md_text)
    if not m:
        return {}, md_text
    try:
        data = yaml.load(m.group(1), Loader=IsoDateLoader) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"frontmatter YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data, m.group(2)


# ---------------------------------------------------------------------------
# Serialize frontmatter
# ---------------------------------------------------------------------------
def serialize_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Emit ``---\\n<yaml>---\\n\\n<body>\\n``.

    Any date/datetime values MUST already be ISO strings (callers that
    construct ``fm`` use ``dt.datetime.now(dt.UTC).isoformat()``). ``tags``
    remain a plain Python list — pyyaml renders them in flow style.

    ``sort_keys=False`` preserves insertion order so Obsidian's viewer
    keeps the author-intended ordering.
    """
    yaml_text = yaml.safe_dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    # Ensure exactly one trailing newline after body for Git-friendly
    # diffs — strip trailing newlines from input then append one.
    body_stripped = body.rstrip("\n")
    return f"---\n{yaml_text}---\n\n{body_stripped}\n"


# ---------------------------------------------------------------------------
# FTS5 query builder (RQ2 + H1 + H2.1)
# ---------------------------------------------------------------------------
def _build_fts_query(raw_q: str) -> str:
    """Transform user text into an FTS5 ``MATCH`` expression.

    - tokenise with ``[\\w]+`` (unicode-aware)
    - ``ё`` folding, lowercase
    - Cyrillic tokens: stem via PyStemmer; append ``*`` only if
      ``len(stem) >= 3`` (H2.1 — a lone ``я*`` would match every
      word starting with ``я``)
    - Latin tokens: wrap in double quotes so FTS5 sees them as a phrase
      (H1 — avoids MATCH parse errors on punctuation)

    Raises :class:`ValueError` when all tokens drop (nothing searchable).
    """
    if not isinstance(raw_q, str):
        raise ValueError("query must be a string")
    tokens = _TOKEN_RE.findall(raw_q)
    out: list[str] = []
    for tok in tokens:
        low = tok.lower().replace("ё", "е")
        if _CYRILLIC_CHAR_RE.search(low):
            stem = _STEMMER.stemWord(low)
            if len(stem) < 3:  # H2.1
                continue
            out.append(f"{stem}*")
        else:
            # H1: phrase form tolerates punctuation, no FTS5 parser
            # surprises on Latin tokens (emails, URLs, code).
            out.append(f'"{low}"')
    if not out:
        raise ValueError("query has no searchable tokens")
    return " ".join(out)


# ---------------------------------------------------------------------------
# Body sanitisation (C2.2 surrogates + R1 sentinel reject + M3 bare ---)
# ---------------------------------------------------------------------------
def sanitize_body(body: str, max_body_bytes: int) -> str:
    """Multi-layer body scrubber applied pre-write.

    (a) C2.2: scrub lone UTF-16 surrogates via ``surrogatepass``/``ignore``
        round-trip — prevents ``.encode('utf-8')`` crashes further down
        the write pipeline.
    (b) R1 layer 1: reject bodies that contain a literal sentinel tag —
        a model-written note that embeds ``</untrusted-note-body>`` would
        close the sentinel cage when later surfaced by ``memory_read``.
    (c) M3: reject bodies with a bare ``---`` on their own line — that
        would terminate the YAML frontmatter if Obsidian re-serialises
        the note.
    (d) Enforce the byte-length cap (``max_body_bytes``).

    Returns the cleaned body. Raises :class:`ValueError` on any failure.
    """
    if not isinstance(body, str):
        raise ValueError("body must be a string")

    cleaned = body.encode("utf-8", errors="surrogatepass").decode(
        "utf-8", errors="ignore"
    )

    if _SENTINEL_RE.search(cleaned):
        raise ValueError("body contains reserved sentinel tag")

    for line in cleaned.splitlines():
        if line.strip() == "---":
            raise ValueError(
                "bare '---' line conflicts with frontmatter boundary; "
                "use '***' or indent"
            )

    try:
        encoded = cleaned.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"body contains un-encodable char at offset {exc.start}"
        ) from exc
    if len(encoded) > max_body_bytes:
        raise ValueError(f"body exceeds {max_body_bytes} bytes")
    return cleaned


# ---------------------------------------------------------------------------
# Untrusted-content wrapping (R1 layers 2+3)
# ---------------------------------------------------------------------------
def wrap_untrusted(body: str, tag_name: str) -> tuple[str, str]:
    """Wrap ``body`` in a nonce-sentinel cage.

    ``tag_name`` ∈ ``{"untrusted-note-body", "untrusted-note-snippet"}``.

    Returns ``(wrapped_text, nonce)``. The nonce is 12-char hex;
    collisions with body content are handled by up-to-three retries,
    falling back to ``secrets.token_hex(16)`` (effectively unique).

    Defence-in-depth: any literal sentinel tag remaining in the body
    (even after ``sanitize_body`` rejected new writes; older on-disk
    notes may still carry them) is scrubbed by injecting a
    zero-width-space after the opening ``<``.
    """
    scrubbed = _SENTINEL_RE.sub(
        lambda m: m.group(0).replace("<", "<\u200b"), body
    )
    nonce = secrets.token_hex(6)
    for _ in range(3):
        if f"{tag_name}-{nonce}" not in scrubbed:
            break
        nonce = secrets.token_hex(6)
    else:
        nonce = secrets.token_hex(16)
    open_tag = f"<{tag_name}-{nonce}>"
    close_tag = f"</{tag_name}-{nonce}>"
    return f"{open_tag}\n{scrubbed}\n{close_tag}", nonce


# ---------------------------------------------------------------------------
# Filesystem-type detection (C2.1 + RQ3)
# ---------------------------------------------------------------------------
def _detect_fs_type(path: Path) -> str | None:
    """Return the filesystem type string (``apfs``, ``ext4`` ...) or
    ``None`` if detection fails. Never raises — callers need best-effort.
    """
    try:
        uname = os.uname().sysname
    except OSError:
        return None
    if uname == "Darwin":
        try:
            out = subprocess.run(
                ["/sbin/mount"],
                capture_output=True,
                text=True,
                timeout=_FS_TYPE_CMD_TIMEOUT_SEC,
                check=False,
            ).stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        # Find the mount-point entry that has the longest prefix-match
        # against ``path`` — deep mount points (e.g. the user's data
        # volume) MUST win over the root.
        best: tuple[int, str] | None = None
        resolved = path.resolve()
        for line in out.splitlines():
            m = _DARWIN_MOUNT_RE.match(line)
            if not m:
                continue
            mp_str, fs = m.group(1), m.group(2).lower()
            try:
                mp = Path(mp_str)
                if resolved == mp or mp in resolved.parents:
                    score = len(str(mp))
                    if best is None or score > best[0]:
                        best = (score, fs)
            except (ValueError, OSError):
                continue
        return best[1] if best else None
    # Linux / other POSIX: ``stat -f`` with ``-c '%T'`` reports the
    # filesystem type (Darwin's ``stat -f %T`` reports FILE type;
    # semantics diverge per OS).
    try:
        out = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True,
            text=True,
            timeout=_FS_TYPE_CMD_TIMEOUT_SEC,
            check=False,
        ).stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return out.lower() or None


def _fs_type_check(path: Path) -> None:
    """Warn (never raise) when ``path`` lives on a cloud-sync /
    network filesystem where ``fcntl.flock`` is silently a no-op.

    Triggers:
      - Path prefix matches one of ``_UNSAFE_PATH_PREFIXES`` (iCloud,
        CloudStorage, Dropbox) even if the mounted FS reports ``apfs``.
      - Detected filesystem is in ``_UNSAFE_FS``.
      - Detected filesystem is neither allowed nor explicitly unsafe —
        informational log so owner can investigate.
    """
    resolved = path.expanduser().resolve()
    for pref in _UNSAFE_PATH_PREFIXES:
        expanded = Path(pref).expanduser()
        if str(resolved).startswith(str(expanded)):
            _log.warning(
                "memory_vault_cloud_sync_path",
                path=str(resolved),
                prefix=pref,
            )
    fs = _detect_fs_type(resolved)
    if fs is None:
        _log.info("memory_vault_fs_type_unknown", path=str(resolved))
        return
    if fs in _UNSAFE_FS:
        _log.warning("memory_vault_unsafe_fs", path=str(resolved), fs=fs)
    elif fs not in _ALLOWED_FS:
        _log.info("memory_vault_unrecognized_fs", path=str(resolved), fs=fs)


# ---------------------------------------------------------------------------
# Vault lock (fcntl.flock context manager)
# ---------------------------------------------------------------------------
@contextmanager
def vault_lock(
    lock_path: Path,
    *,
    blocking: bool = True,
    timeout: float = _LOCK_TIMEOUT_SEC,
) -> Iterator[None]:
    """Hold ``fcntl.flock(LOCK_EX)`` on ``lock_path`` for the duration
    of the ``with`` block.

    ``blocking=False`` raises :class:`BlockingIOError` immediately when
    the lock is held (used by ``_maybe_auto_reindex`` on boot so a
    concurrent write doesn't stall daemon startup).

    ``blocking=True`` polls with a 50 ms sleep up to ``timeout`` seconds
    before raising :class:`TimeoutError`. Non-blocking poll is preferable
    over the blocking ``LOCK_EX`` variant because it lets us honour the
    timeout without relying on signal-based alarms.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        if not blocking:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"vault_lock timeout after {timeout}s"
                        ) from None
                    time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Vault scanning
# ---------------------------------------------------------------------------
def _vault_rel_eligible(rel: Path) -> bool:
    """True iff the relative vault path should be indexed (M2.1 + H3)."""
    for part in rel.parts:
        if part in _VAULT_SCAN_EXCLUDES:
            return False
    return not rel.name.startswith("_")


def _scan_vault_stats(vault_dir: Path) -> tuple[int, int]:
    """Return ``(eligible_md_count, max_st_mtime_ns)`` over the vault.

    Never raises on per-file errors (stat failures, broken symlinks) —
    skips the offender and keeps counting. Used exclusively by
    ``_maybe_auto_reindex`` as a cheap staleness probe.
    """
    n = 0
    max_mtime_ns = 0
    if not vault_dir.exists():
        return 0, 0
    for md in vault_dir.rglob("*.md"):
        try:
            rel = md.relative_to(vault_dir)
        except ValueError:
            continue
        if not _vault_rel_eligible(rel):
            continue
        try:
            st = md.stat()
        except OSError:
            continue
        n += 1
        if st.st_mtime_ns > max_mtime_ns:
            max_mtime_ns = st.st_mtime_ns
    return n, max_mtime_ns


def _title_from_body_or_stem(body: str, rel: Path) -> str:
    """Title fallback used by both ``memory_read`` and ``reindex_vault``.

    Precedence: first H1 heading in body → filename stem
    (``my-note.md`` → ``My Note``).
    """
    m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return rel.stem.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Reindex (full rebuild, holds lock)
# ---------------------------------------------------------------------------
def _parse_note_for_index(
    md_path: Path, rel: Path
) -> tuple[tuple[str, str, str, str, str, str, str] | None, str | None]:
    """Parse one on-disk note into a row tuple or a skip reason.

    Returns ``(row, None)`` on success or ``(None, reason)`` if the file
    should be skipped (unreadable, malformed frontmatter). Centralised so
    ``reindex_vault`` stays focused on transactional state management.
    """
    try:
        raw = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"read error: {exc}"
    try:
        fm, body = parse_frontmatter(raw)
    except ValueError as exc:
        return None, f"frontmatter: {exc}"
    title_raw = fm.get("title")
    title = (
        str(title_raw).strip() if title_raw else _title_from_body_or_stem(body, rel)
    )
    area_raw = fm.get("area")
    if area_raw:  # noqa: SIM108
        area = str(area_raw)
    else:
        area = rel.parts[0] if len(rel.parts) > 1 else ""
    tags_val = fm.get("tags") or []
    if not isinstance(tags_val, list):
        tags_val = [str(tags_val)]
    tags_json = json.dumps([str(t) for t in tags_val], ensure_ascii=False)  # M6
    created = str(
        fm.get("created") or dt.datetime.now(dt.UTC).isoformat()
    )
    updated = str(fm.get("updated") or created)
    return (str(rel), title, tags_json, area, body, created, updated), None


def reindex_vault(vault_dir: Path, index_db_path: Path) -> int:
    """Full rebuild of the notes table + FTS5 index.

    Caller MUST hold ``vault_lock`` — this function does not acquire it
    itself because callers differ in blocking semantics (auto-reindex on
    boot uses non-blocking, manual ``memory_reindex`` uses blocking).

    Per H5 + L2.3: deletes existing rows inside a single transaction,
    batch-inserts new rows, rebuilds the FTS5 index, and updates the
    ``max_mtime_ns`` meta row.
    """
    t0 = time.perf_counter()
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    skipped: list[dict[str, str]] = []
    max_mtime_ns = 0
    if vault_dir.exists():
        for md in vault_dir.rglob("*.md"):
            try:
                rel = md.relative_to(vault_dir)
            except ValueError:
                continue
            if not _vault_rel_eligible(rel):
                continue
            row, reason = _parse_note_for_index(md, rel)
            if reason is not None:
                skipped.append({"path": str(rel), "reason": reason})
                continue
            assert row is not None  # mypy: reason-None implies row-set
            rows.append(row)
            try:
                st = md.stat()
            except OSError:
                continue
            if st.st_mtime_ns > max_mtime_ns:
                max_mtime_ns = st.st_mtime_ns
    conn = sqlite3.connect(index_db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM notes")
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO notes"
                "(path, title, tags, area, body, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        # FTS5 rebuild is cheap on a freshly-populated shadow table and
        # defensively resyncs against any trigger drift (L2.3).
        conn.execute("INSERT INTO notes_fts(notes_fts) VALUES ('rebuild')")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('max_mtime_ns', ?)",
            (str(max_mtime_ns),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    duration_ms = int((time.perf_counter() - t0) * 1000)
    _log.info(
        "memory_reindex_done",
        indexed=len(rows),
        skipped=len(skipped),
        duration_ms=duration_ms,
    )
    return len(rows)


def reindex_under_lock(vault_dir: Path, index_db_path: Path) -> int:
    """Acquire the blocking vault lock and run a full reindex."""
    lock_path = Path(str(index_db_path) + ".lock")
    with vault_lock(lock_path, blocking=True, timeout=_LOCK_TIMEOUT_SEC):
        return reindex_vault(vault_dir, index_db_path)


# ---------------------------------------------------------------------------
# Auto-reindex on boot (Policy B enhanced — C2.3 signature + C2.4 mtime)
# ---------------------------------------------------------------------------
def _maybe_auto_reindex(vault_dir: Path, index_db_path: Path) -> None:
    """Detect stale index and rebuild it, non-blocking at boot.

    Triggers when:
      - ``len(notes on disk) != COUNT(*) FROM notes`` (file added/removed
        outside this daemon), OR
      - ``max_st_mtime_ns > meta('max_mtime_ns')`` (Obsidian edited a
        note in place, which leaves count unchanged; C2.4).

    Safety rails:
      - Non-blocking lock acquisition (``BlockingIOError`` → warn+skip)
        so a concurrent write in another process cannot stall daemon
        boot (C2.3 follow-on).
      - Large-vault cap (``_MAX_AUTO_REINDEX``); opt-out via
        ``MEMORY_ALLOW_LARGE_REINDEX`` env (Q-R5).
    """
    disk_count, disk_max_mtime_ns = _scan_vault_stats(vault_dir)
    conn = sqlite3.connect(index_db_path)
    try:
        idx_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        row = conn.execute(
            "SELECT value FROM meta WHERE key='max_mtime_ns'"
        ).fetchone()
        stored_mtime_ns = int(row[0]) if row and row[0] else 0
    finally:
        conn.close()

    needs_reindex = (
        idx_count != disk_count or disk_max_mtime_ns > stored_mtime_ns
    )
    if not needs_reindex:
        return
    if (
        disk_count > _MAX_AUTO_REINDEX
        and not os.environ.get("MEMORY_ALLOW_LARGE_REINDEX")
    ):
        _log.warning(
            "memory_vault_too_large_for_auto_reindex",
            disk_count=disk_count,
            idx_count=idx_count,
            cap=_MAX_AUTO_REINDEX,
        )
        return

    lock_path = Path(str(index_db_path) + ".lock")
    try:
        with vault_lock(lock_path, blocking=False):
            indexed = reindex_vault(vault_dir, index_db_path)
            _log.info("memory_auto_reindex_done", indexed=indexed)
    except BlockingIOError:
        _log.warning("memory_auto_reindex_skipped_lock_contention")


# ---------------------------------------------------------------------------
# Transaction helpers (write / delete) — H2 inverted ordering
# ---------------------------------------------------------------------------
def write_note_tx(
    full_path: Path,
    rel_path: Path,
    row: tuple[str, str, str, str, str, str, str],
    content: str,
    vault_dir: Path,
    index_db_path: Path,
) -> None:
    """Atomically insert/replace ``row`` in the index and write ``content``
    to ``full_path``.

    Ordering (H2):
      1. Acquire the blocking vault lock.
      2. ``BEGIN IMMEDIATE`` → ``INSERT OR REPLACE INTO notes`` —
         FTS5 triggers fire at commit.
      3. ``atomic_write`` — tmp + fsync + os.replace.
      4. ``COMMIT`` — commit-after-rename keeps the vault (filesystem)
         authoritative; worst case is a crashed commit after the rename,
         which the next ``_maybe_auto_reindex`` will repair.
      5. Update ``meta('max_mtime_ns')`` inside the same transaction —
         we do this BEFORE COMMIT by stat'ing the now-on-disk file.
    """
    lock_path = Path(str(index_db_path) + ".lock")
    tmp_dir = vault_dir / ".tmp"
    with vault_lock(lock_path, blocking=True, timeout=_LOCK_TIMEOUT_SEC):
        conn = sqlite3.connect(index_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO notes"
                "(path, title, tags, area, body, created, updated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
            try:
                atomic_write(full_path, content, tmp_dir=tmp_dir)
            except Exception:
                conn.rollback()
                raise
            try:
                new_mtime_ns = full_path.stat().st_mtime_ns
            except OSError:
                new_mtime_ns = 0
            cur_row = conn.execute(
                "SELECT value FROM meta WHERE key='max_mtime_ns'"
            ).fetchone()
            cur_max = int(cur_row[0]) if cur_row and cur_row[0] else 0
            new_max = max(cur_max, new_mtime_ns)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('max_mtime_ns', ?)",
                (str(new_max),),
            )
            conn.commit()
        finally:
            conn.close()
    # Silence unused variable warnings — rel_path is part of the
    # signature for call-site clarity.
    del rel_path


def delete_note_tx(
    full_path: Path,
    rel_path: str,
    vault_dir: Path,
    index_db_path: Path,
) -> None:
    """Atomically unlink ``full_path`` and drop the index row.

    Order mirrors ``write_note_tx``: lock → BEGIN → DELETE → unlink →
    COMMIT. We DO rollback if the unlink fails, but NOT if the COMMIT
    fails after the unlink — in that case the file is already gone and
    the next ``_maybe_auto_reindex`` picks up the count mismatch.
    """
    lock_path = Path(str(index_db_path) + ".lock")
    with vault_lock(lock_path, blocking=True, timeout=_LOCK_TIMEOUT_SEC):
        conn = sqlite3.connect(index_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
            try:
                full_path.unlink()
            except FileNotFoundError:
                # Someone deleted the file in parallel — keep DB
                # consistent by continuing the delete.
                pass
            except OSError:
                conn.rollback()
                raise
            # Recompute vault max mtime (M-after-delete). Cheap for a
            # single-user vault; no sub-thousand bound needed.
            _, new_max = _scan_vault_stats(vault_dir)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('max_mtime_ns', ?)",
                (str(new_max),),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Read helpers (search + list)
# ---------------------------------------------------------------------------
def search_notes(
    index_db_path: Path,
    fts_query: str,
    area: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Execute the FTS5 MATCH query and return hit dicts.

    Opens a fresh read-only connection per call; WAL-mode reads don't
    need the write lock.
    """
    conn = sqlite3.connect(f"file:{index_db_path}?mode=ro", uri=True)
    try:
        if area is None:
            sql = (
                "SELECT n.path, n.title, n.tags, n.area, "
                "snippet(notes_fts, 4, '<b>', '</b>', '...', 32) AS snip "
                "FROM notes n JOIN notes_fts f ON n.rowid = f.rowid "
                "WHERE f.notes_fts MATCH ? "
                "ORDER BY rank LIMIT ?"
            )
            params: tuple[Any, ...] = (fts_query, limit)
        else:
            sql = (
                "SELECT n.path, n.title, n.tags, n.area, "
                "snippet(notes_fts, 4, '<b>', '</b>', '...', 32) AS snip "
                "FROM notes n JOIN notes_fts f ON n.rowid = f.rowid "
                "WHERE f.notes_fts MATCH ? AND n.area = ? "
                "ORDER BY rank LIMIT ?"
            )
            params = (fts_query, area, limit)
        cur = conn.execute(sql, params)
        hits: list[dict[str, Any]] = []
        for path, title, tags_json, note_area, snip in cur.fetchall():
            try:
                tags = json.loads(tags_json) if tags_json else []
            except (TypeError, json.JSONDecodeError):
                tags = []
            hits.append(
                {
                    "path": path,
                    "title": title,
                    "tags": tags,
                    "area": note_area,
                    "snippet": snip,
                }
            )
        return hits
    finally:
        conn.close()


def list_notes(
    index_db_path: Path,
    area: str | None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return ``(rows, total_count)`` filtered by ``area`` (optional)."""
    conn = sqlite3.connect(f"file:{index_db_path}?mode=ro", uri=True)
    try:
        if area is None:
            total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            cur = conn.execute(
                "SELECT path, title, tags, area, created, updated "
                "FROM notes ORDER BY updated DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM notes WHERE area = ?", (area,)
            ).fetchone()[0]
            cur = conn.execute(
                "SELECT path, title, tags, area, created, updated "
                "FROM notes WHERE area = ? "
                "ORDER BY updated DESC LIMIT ? OFFSET ?",
                (area, limit, offset),
            )
        rows: list[dict[str, Any]] = []
        for path, title, tags_json, note_area, created, updated in cur.fetchall():
            try:
                tags = json.loads(tags_json) if tags_json else []
            except (TypeError, json.JSONDecodeError):
                tags = []
            rows.append(
                {
                    "path": path,
                    "title": title,
                    "tags": tags,
                    "area": note_area,
                    "created": created,
                    "updated": updated,
                }
            )
        return rows, int(total)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Wikilink extraction (M2.4 + H6)
# ---------------------------------------------------------------------------
def extract_wikilinks(body: str) -> list[str]:
    """Return target names of ``[[...]]`` wikilinks in ``body``.

    Strips:
      - alias: ``[[target|alias]]`` → ``target``
      - block ref: ``[[target#section]]`` / ``[[target^id]]`` → ``target``
    """
    out: list[str] = []
    for match in _WIKILINK_RE.findall(body):
        raw = match.strip()
        if not raw:
            continue
        # Order matters: alias split first (``|``), then section/block
        # ref (``#``/``^``).
        target = re.split(r"[#^|]", raw, maxsplit=1)[0].strip()
        if target:
            out.append(target)
    return out
