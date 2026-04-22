"""RQ4 — YAML bare dates round-trip through JSON (devil ID-C3).

Reproduces: `created: 2026-04-16` in seed frontmatter is a bare YAML date;
yaml.safe_load returns datetime.date; json.dumps crashes.

Tests three fixes:
  (A) Custom SafeLoader with date constructor override → ISO string.
  (B) Post-parse dict walk coercing date/datetime to isoformat().
  (C) Lazy `json.dumps(..., default=str)`.

Run:  .venv/bin/python plan/phase4/spikes/rq4_yaml_bare_dates.py
Capture stdout → plan/phase4/spikes/rq4_yaml_bare_dates.txt
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yaml

HERE = Path(__file__).parent
OUT = HERE / "rq4_yaml_bare_dates.txt"

SEED_FILE = Path("/Users/agent2/.local/share/0xone-assistant/vault/projects/flowgent.md")


def extract_frontmatter(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError("no frontmatter start")
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        raise ValueError("no frontmatter end")
    return rest[:end].lstrip("\n")


def reproduce_crash() -> dict:
    fm = extract_frontmatter(SEED_FILE)
    parsed = yaml.safe_load(fm)
    try:
        json.dumps(parsed)
        return {"repro": False, "parsed": parsed}
    except TypeError as exc:
        return {"repro": True, "parsed_repr": repr(parsed), "error": str(exc)}


# ---------------------------------------------------------------------------
# Fix A — custom loader that constructs date/datetime as ISO strings.
# ---------------------------------------------------------------------------
class IsoDateLoader(yaml.SafeLoader):
    pass


def _construct_iso_timestamp(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    """Replace YAML 1.1 !!timestamp with ISO-8601 string.

    yaml.SafeLoader maps bare ``2026-04-16`` to the !!timestamp tag. We
    intercept construction and return the raw scalar as text — but that
    scalar is already normalised by the resolver. Safer: reconstruct via
    the default SafeConstructor, then stringify.
    """
    # Use the default constructor, then convert.
    val = yaml.SafeLoader.construct_yaml_timestamp(loader, node)
    if isinstance(val, (dt.date, dt.datetime)):
        return val.isoformat()
    return str(val)


IsoDateLoader.add_constructor("tag:yaml.org,2002:timestamp", _construct_iso_timestamp)


def fix_a_custom_loader() -> dict:
    fm = extract_frontmatter(SEED_FILE)
    parsed = yaml.load(fm, Loader=IsoDateLoader)
    try:
        s = json.dumps(parsed, ensure_ascii=False)
        return {"ok": True, "parsed": parsed, "json_len": len(s)}
    except TypeError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Fix B — post-parse walk.
# ---------------------------------------------------------------------------
def _coerce_dates(obj):  # noqa: ANN001, ANN201
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _coerce_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_dates(v) for v in obj]
    return obj


def fix_b_post_walk() -> dict:
    fm = extract_frontmatter(SEED_FILE)
    parsed = yaml.safe_load(fm)
    coerced = _coerce_dates(parsed)
    try:
        s = json.dumps(coerced, ensure_ascii=False)
        return {"ok": True, "parsed": coerced, "json_len": len(s)}
    except TypeError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Fix C — lazy json dumps with default=str.
# ---------------------------------------------------------------------------
def fix_c_lazy() -> dict:
    fm = extract_frontmatter(SEED_FILE)
    parsed = yaml.safe_load(fm)
    try:
        s = json.dumps(parsed, ensure_ascii=False, default=str)
        return {"ok": True, "json_preview": s[:200]}
    except TypeError as exc:
        return {"ok": False, "error": str(exc)}


def scan_all_seed_notes() -> dict:
    """Find every seed note that has at least one date-typed frontmatter value."""
    vault = Path("/Users/agent2/.local/share/0xone-assistant/vault")
    affected: list[str] = []
    for md in vault.rglob("*.md"):
        try:
            fm = extract_frontmatter(md)
            p = yaml.safe_load(fm)
        except Exception:
            continue
        if isinstance(p, dict) and any(isinstance(v, (dt.date, dt.datetime)) for v in p.values()):
            affected.append(str(md.relative_to(vault)))
    return {"affected_count": len(affected), "affected": affected}


def main() -> int:
    lines: list[str] = []

    def w(s: str = "") -> None:
        lines.append(s)
        print(s)

    w("## 1. Reproduce crash on seed note")
    r = reproduce_crash()
    w(f"  reproduced: {r['repro']}")
    if r["repro"]:
        w(f"  parsed: {r['parsed_repr']}")
        w(f"  error:  {r['error']}")
    w()

    w("## 2. Fix A (custom SafeLoader)")
    r = fix_a_custom_loader()
    w(f"  ok: {r['ok']}")
    if r["ok"]:
        w(f"  parsed[0]={list(r['parsed'].items())[0]!r}")
        w(f"  json_len: {r['json_len']}")
    w()

    w("## 3. Fix B (post-parse dict walk)")
    r = fix_b_post_walk()
    w(f"  ok: {r['ok']}")
    if r["ok"]:
        w(f"  parsed[0]={list(r['parsed'].items())[0]!r}")
        w(f"  json_len: {r['json_len']}")
    w()

    w("## 4. Fix C (json.dumps default=str)")
    r = fix_c_lazy()
    w(f"  ok: {r['ok']}")
    if r["ok"]:
        w(f"  json_preview: {r['json_preview']}")
    w()

    w("## 5. Scan all seed notes")
    r = scan_all_seed_notes()
    w(f"  affected: {r['affected_count']} / {len(r['affected'])}")
    for p in r["affected"]:
        w(f"    - {p}")
    w()

    w("## Recommendation")
    w("""
Preferred: Fix A (custom Loader).
  - Single point of truth: every call site that parses frontmatter gets
    ISO strings automatically.
  - Preserves YAML semantics elsewhere; no post-walk cost.
  - Code sketch:

    class IsoDateLoader(yaml.SafeLoader):
        pass

    def _timestamp_as_iso(loader, node):
        val = yaml.SafeLoader.construct_yaml_timestamp(loader, node)
        return val.isoformat() if isinstance(val, (datetime.date, datetime.datetime)) else str(val)

    IsoDateLoader.add_constructor('tag:yaml.org,2002:timestamp', _timestamp_as_iso)

    def parse_frontmatter(text: str) -> dict:
        m = re.match(r'^---\\n(.*?)\\n---', text, re.DOTALL)
        if not m:
            return {}
        data = yaml.load(m.group(1), Loader=IsoDateLoader) or {}
        if not isinstance(data, dict):
            raise ValueError('frontmatter is not a mapping')
        return data

Acceptable: Fix B (defensive).  Use ONLY if a downstream consumer needs
  native datetime objects somewhere — keep the Loader raw and coerce on
  the path to JSON-RPC. Adds one O(n) walk per memory_read.

Avoid: Fix C (json.dumps default=str).  Works but ``default=str`` hides
  bugs — next schema evolution adds e.g. a custom type and silently
  stringifies it.  Also: round-trip inconsistency (write stores string,
  Obsidian may re-write as bare date).

Additional concern: on WRITE, ``yaml.safe_dump(frontmatter)`` may re-emit
  the value as a bare date if it's an ISO string that looks date-like? No —
  safe_dump emits as quoted string unless the Python value is date/datetime.
  But Obsidian users may re-edit the file and switch back to bare form.
  The READ path handles both. The WRITE path should always emit quoted
  strings for created/updated (use ``default_flow_style=False`` + put
  value as string explicitly).
""")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
