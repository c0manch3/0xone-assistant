"""RQ6 — First-boot auto-reindex policy (devil ID-C6).

Analyzes the startup reindex decision: seed vault has 12 notes, index
schema is empty after _ensure_index. First memory_search returns 0 hits.
UX bug.

Measures:
  1. Time to reindex the 12-note seed vault (baseline).
  2. Extrapolate to 100 / 1000 / 5000 note vaults.
  3. Evaluate three policies:
      (A) Unconditional reindex on every configure_memory boot.
      (B) Staleness-gated reindex: reindex only if fs_count != index_count.
      (C) Lazy: only reindex on explicit memory_reindex call.

Run:  .venv/bin/python plan/phase4/spikes/rq6_autoreindex.py
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).parent
OUT = HERE / "rq6_autoreindex.txt"
VAULT = Path("/Users/agent2/.local/share/0xone-assistant/vault")


def extract_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        return {}, text
    fm_text = rest[:end].lstrip("\n")
    body = rest[end + 4:].lstrip("\n")
    try:
        data = yaml.safe_load(fm_text) or {}
    except Exception:
        data = {}
    return data, body


def reindex_measure(vault: Path, db_path: str = ":memory:") -> dict:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            path TEXT PRIMARY KEY, title TEXT NOT NULL, tags TEXT,
            area TEXT, body TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
        "path, title, tags, area, body, "
        "content='notes', content_rowid='rowid', "
        "tokenize='unicode61 remove_diacritics 2')"
    )

    exclude_parts = {".obsidian", ".tmp", ".git", ".trash", "__pycache__", ".DS_Store"}
    n = 0
    skipped: list[tuple[str, str]] = []
    t0 = time.perf_counter()
    for md in vault.rglob("*.md"):
        rel = md.relative_to(vault)
        if any(p in exclude_parts for p in rel.parts):
            continue
        if md.name.startswith("_") and md.name.endswith(".md"):
            skipped.append((str(rel), "_*.md exclude"))
            continue
        try:
            text = md.read_text(encoding="utf-8")
            fm, body = extract_frontmatter(text)
            title = fm.get("title")
            if not title:
                m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
                title = m.group(1).strip() if m else md.stem.replace("-", " ").title()
        except Exception as exc:
            skipped.append((str(rel), f"parse: {exc}"))
            continue
        conn.execute(
            "INSERT OR REPLACE INTO notes(path, title, tags, area, body, created, updated) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                str(rel),
                str(title),
                str(fm.get("tags") or []),
                rel.parts[0] if len(rel.parts) > 1 else "",
                body,
                str(fm.get("created") or ""),
                str(fm.get("updated") or ""),
            ),
        )
        n += 1
    conn.commit()
    elapsed = time.perf_counter() - t0
    count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    return {
        "indexed": n,
        "skipped": skipped,
        "duration_sec": round(elapsed, 4),
        "index_count": count,
    }


def count_on_disk(vault: Path) -> dict:
    exclude_parts = {".obsidian", ".tmp", ".git", ".trash", "__pycache__", ".DS_Store"}
    total_md = 0
    eligible = 0
    skipped_underscore = 0
    for md in vault.rglob("*.md"):
        total_md += 1
        rel = md.relative_to(vault)
        if any(p in exclude_parts for p in rel.parts):
            continue
        if md.name.startswith("_"):
            skipped_underscore += 1
            continue
        eligible += 1
    return {"total_md": total_md, "eligible": eligible, "skipped_underscore": skipped_underscore}


def main() -> int:
    lines: list[str] = []

    def w(s: str = "") -> None:
        lines.append(s)
        print(s)

    w("## Seed vault disk inventory")
    c = count_on_disk(VAULT)
    w(f"  total_md files: {c['total_md']}")
    w(f"  eligible for index: {c['eligible']}")
    w(f"  skipped (_*.md): {c['skipped_underscore']}")
    w()

    w("## Seed vault reindex measurement")
    r = reindex_measure(VAULT)
    w(f"  indexed:  {r['indexed']}")
    w(f"  index_count: {r['index_count']}")
    w(f"  duration: {r['duration_sec']} sec")
    w(f"  skipped:  {len(r['skipped'])}")
    for p, reason in r["skipped"]:
        w(f"    - {p} ({reason})")
    w()

    # Extrapolate
    per_note = r["duration_sec"] / max(r["indexed"], 1)
    w(f"## Extrapolation ({per_note * 1000:.1f} ms/note measured)")
    for n in [100, 500, 1000, 2000, 5000]:
        w(f"  {n:>5} notes: ~{per_note * n:.1f} sec reindex")
    w()

    w("## Policy evaluation")
    w("""
Policy A — unconditional reindex on every configure_memory boot.
  Pros: simplest code; seed works; survives FS-edits-while-stopped.
  Cons: 5000-note Obsidian vault blocks daemon startup for ~15 sec
        (phase-1 owner smoke: "bot never came online"). Regressive on
        the user's first bad experience.
  Verdict: reject for default; ok as debug flag.

Policy B — staleness-gated reindex: compare on-disk eligible_count to
  index_count; reindex if mismatched.
  Pros: seed vault case auto-works (0 vs 12 → reindex); normal restart
        is zero-cost; survives DB deletion (0 vs 12 → reindex).
  Cons: count comparison is cheap but imperfect — if exactly N files
        were edited in place with identical count, skipped. Need a
        mtime-based sanity check or explicit memory_reindex to force.
  Verdict: default. Enough for phase-4 scope.

Policy C — lazy, only on explicit memory_reindex.
  Pros: minimal startup code; explicit user intent.
  Cons: violates AC#1 out-of-box on seed vault (first memory_search
        misses everything); model must be told to run memory_reindex
        by system prompt, adding a turn to every first-boot interaction.
  Verdict: reject — burden shifted to prompt + model discipline.

## Recommended: Policy B with a safety cap.

Stub code for _memory_core.py:

```python
_VAULT_SCAN_EXCLUDES = {'.obsidian', '.tmp', '.git', '.trash', '__pycache__', '.DS_Store'}

def _count_eligible_on_disk(vault: Path) -> int:
    n = 0
    for md in vault.rglob('*.md'):
        rel = md.relative_to(vault)
        if any(p in _VAULT_SCAN_EXCLUDES for p in rel.parts):
            continue
        if md.name.startswith('_'):
            continue
        n += 1
    return n

def _maybe_auto_reindex(vault: Path, db: sqlite3.Connection, log) -> None:
    '''Called at end of configure_memory. Reindex if on-disk count disagrees
    with index count, up to a 2000-note cap (warn+skip above).
    '''
    disk = _count_eligible_on_disk(vault)
    idx = db.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
    if disk == idx:
        log.info('memory_index_fresh', disk_count=disk)
        return
    MAX_AUTO_REINDEX = 2000
    if disk > MAX_AUTO_REINDEX:
        log.warning(
            'memory_vault_too_large_for_auto_reindex',
            disk_count=disk, cap=MAX_AUTO_REINDEX,
            note='call memory_reindex explicitly; or set MEMORY_ALLOW_LARGE_REINDEX=1'
        )
        return
    t0 = time.perf_counter()
    n = _reindex_vault(vault, db)  # existing fn
    log.info('memory_auto_reindex_done', indexed=n,
             duration_ms=int((time.perf_counter() - t0) * 1000))
```

Addresses both devil C6 (seed vault empty-index UX bug) and devil H8
(large-vault blast radius cap).
""")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
