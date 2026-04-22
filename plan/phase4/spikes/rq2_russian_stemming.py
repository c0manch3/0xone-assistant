"""RQ2 — Russian morphology in FTS5.

Tests 5 approaches against a corpus of inflection pairs common in
Russian user text. Devil-wave-1 ID-C1 verified that the plan's
``porter unicode61 remove_diacritics 2`` tokenizer fails Russian
morphology (``жене`` doesn't match ``жены``). This spike confirms the
issue and evaluates fixes.

Approaches:
  1. Plan-default ``porter unicode61 remove_diacritics 2`` (baseline).
  2. Plan-fallback pure ``unicode61 remove_diacritics 2`` (no morphology).
  3. PyStemmer/Snowball pre-stemming both at index time (shadow column) and query time.
  4. Query-side wildcard expansion using PyStemmer stem + ``*`` suffix.
  5. Query-side wildcard (no stemmer, naïve len-3 prefix) — cheapest fallback.

Run:  .venv/bin/python plan/phase4/spikes/rq2_russian_stemming.py
Capture stdout → plan/phase4/spikes/rq2_russian_stemming.txt
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
OUT_TXT = HERE / "rq2_russian_stemming.txt"
OUT_JSON = HERE / "rq2_russian_stemming.json"

try:
    import Stemmer  # pystemmer
except ImportError:
    print("ERROR: pystemmer not installed. Run: uv pip install PyStemmer")
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Test corpus: (indexed_note_body, query, should_match, label)
# ---------------------------------------------------------------------------
CORPUS: list[tuple[str, str, bool, str]] = [
    # жена noun forms
    ("у моей жены день рождения 3 апреля", "жене", True, "жены→жене (dative)"),
    ("у моей жены день рождения 3 апреля", "жена", True, "жены→жена (nominative)"),
    ("у моей жены день рождения 3 апреля", "жену", True, "жены→жену (accusative)"),
    ("у моей жены день рождения 3 апреля", "жёны", True, "жены→жёны (ё variant)"),
    # апрель month forms
    ("встреча назначена на апрель 2026", "апреля", True, "апрель→апреля (genitive)"),
    ("встреча назначена на апрель 2026", "апрелю", True, "апрель→апрелю (dative)"),
    ("встреча назначена на апрель 2026", "апреле", True, "апрель→апреле (prepositional)"),
    # совещание abstract noun
    ("совещание в студии 44", "совещания", True, "совещание→совещания (genitive)"),
    ("совещание в студии 44", "совещанию", True, "совещание→совещанию (dative)"),
    ("совещание в студии 44", "совещанием", True, "совещание→совещанием (instr)"),
    # рождение
    ("день рождения 3 апреля", "рождение", True, "рождения→рождение"),
    ("день рождения 3 апреля", "рождений", True, "рождения→рождений (plural gen)"),
    # verb forms
    ("запомнил, что у жены день рождения", "запомни", True, "запомнил→запомни (imperative)"),
    ("запомнил, что у жены день рождения", "запомнить", True, "запомнил→запомнить (infinitive)"),
    ("я работаю в студии", "работать", True, "работаю→работать"),
    ("я работаю в студии", "работает", True, "работаю→работает"),
    # архитектура
    ("архитектурное бюро Никиты Явейна", "архитектура", True, "архитектурное→архитектура"),
    ("архитектурное бюро Никиты Явейна", "архитектурой", True, "архитектурное→архитектурой"),
    # negative — should NOT match (precision check)
    ("у моей жены день рождения", "муж", False, "жены != муж (no false positive)"),
    ("я работаю в студии", "деревня", False, "работаю != деревня (no false positive)"),
    ("архитектурное бюро", "машина", False, "архитектура != машина (no false positive)"),
    # mixed cyrillic + latin
    ("AI-решения для flowgent", "flowgent", True, "flowgent exact"),
    ("AI-решения для flowgent", "AI", True, "AI exact"),
    # Obsidian wikilink body
    ("см. [[studio44-workload-platform|Студией 44]]", "studio44", True, "wikilink prefix match"),
    ("см. [[studio44-workload-platform|Студией 44]]", "студия", True, "wikilink alias match"),
]


def build_db(tokenizer: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        f"CREATE VIRTUAL TABLE notes_fts USING fts5(body, tokenize='{tokenizer}')"
    )
    return conn


def approach_plain(label: str, tokenizer: str) -> dict:
    """Plain tokenizer, body stored raw, query passed raw."""
    conn = build_db(tokenizer)
    for i, (body, _, _, _) in enumerate(CORPUS):
        conn.execute("INSERT INTO notes_fts(rowid, body) VALUES (?, ?)", (i, body))
    hits: list[int] = []
    misses: list[int] = []
    false_hits: list[int] = []
    for i, (_, query, should, _) in enumerate(CORPUS):
        try:
            rows = conn.execute(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", (query,)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            rows = []
            print(f"  [{label}] ERROR on query {query!r}: {exc}", file=sys.stderr)
        got = any(r[0] == i for r in rows)
        if should and got:
            hits.append(i)
        elif should and not got:
            misses.append(i)
        elif (not should) and got:
            false_hits.append(i)
    conn.close()
    return {"hits": hits, "misses": misses, "false_hits": false_hits}


def approach_pystem_index_and_query(label: str) -> dict:
    """PyStemmer: pre-stem bodies and queries (Snowball Russian)."""
    stemmer = Stemmer.Stemmer("russian")

    def stem_text(s: str) -> str:
        # Cheap tokenization: split on non-alnum (unicode), stem each token,
        # keep latin words as-is (pystemmer russian stems latin as noise).
        import re as _re

        tokens = _re.findall(r"[\w]+", s, flags=_re.UNICODE)
        stemmed: list[str] = []
        for t in tokens:
            low = t.lower()
            # Detect cyrillic presence; if none, keep literal
            if any("\u0400" <= c <= "\u04ff" for c in low):
                low = low.replace("ё", "е")  # yo-folding
                stemmed.append(stemmer.stemWord(low))
            else:
                stemmed.append(low)
        return " ".join(stemmed)

    conn = sqlite3.connect(":memory:")
    # Use plain unicode61 so pystemmer output (already lowercase latin) is
    # tokenized by whitespace/punct only.
    conn.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2')"
    )
    for i, (body, _, _, _) in enumerate(CORPUS):
        conn.execute(
            "INSERT INTO notes_fts(rowid, body) VALUES (?, ?)", (i, stem_text(body))
        )
    hits: list[int] = []
    misses: list[int] = []
    false_hits: list[int] = []
    for i, (_, query, should, _) in enumerate(CORPUS):
        q_stem = stem_text(query)
        try:
            rows = conn.execute(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", (q_stem,)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            rows = []
            print(f"  [{label}] ERROR on query {query!r}: {exc}", file=sys.stderr)
        got = any(r[0] == i for r in rows)
        if should and got:
            hits.append(i)
        elif should and not got:
            misses.append(i)
        elif (not should) and got:
            false_hits.append(i)
    conn.close()
    return {"hits": hits, "misses": misses, "false_hits": false_hits}


def approach_query_wildcard_pystem(label: str) -> dict:
    """Body stored raw with unicode61; query is ``stem(q)*`` (prefix wildcard)."""
    stemmer = Stemmer.Stemmer("russian")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2')"
    )
    for i, (body, _, _, _) in enumerate(CORPUS):
        conn.execute("INSERT INTO notes_fts(rowid, body) VALUES (?, ?)", (i, body))
    hits: list[int] = []
    misses: list[int] = []
    false_hits: list[int] = []
    for i, (_, query, should, _) in enumerate(CORPUS):
        # tokenise query, stem each cyrillic token, wildcard-suffix
        import re as _re

        parts = _re.findall(r"[\w]+", query, flags=_re.UNICODE)
        q_parts: list[str] = []
        for p in parts:
            low = p.lower().replace("ё", "е")
            if any("\u0400" <= c <= "\u04ff" for c in low):
                stem = stemmer.stemWord(low)
                # Use prefix-match operator; wrap each term in quotes when safe.
                q_parts.append(f"{stem}*")
            else:
                q_parts.append(low)
        fts_query = " ".join(q_parts)
        try:
            rows = conn.execute(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", (fts_query,)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            rows = []
            print(f"  [{label}] ERROR on query {query!r} -> {fts_query!r}: {exc}", file=sys.stderr)
        got = any(r[0] == i for r in rows)
        if should and got:
            hits.append(i)
        elif should and not got:
            misses.append(i)
        elif (not should) and got:
            false_hits.append(i)
    conn.close()
    return {"hits": hits, "misses": misses, "false_hits": false_hits}


def approach_query_wildcard_naive(label: str) -> dict:
    """No stemmer — naïve prefix-3 of each query token (cheapest fallback)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2')"
    )
    for i, (body, _, _, _) in enumerate(CORPUS):
        conn.execute("INSERT INTO notes_fts(rowid, body) VALUES (?, ?)", (i, body))
    hits: list[int] = []
    misses: list[int] = []
    false_hits: list[int] = []
    for i, (_, query, should, _) in enumerate(CORPUS):
        import re as _re

        parts = _re.findall(r"[\w]+", query, flags=_re.UNICODE)
        q_parts: list[str] = []
        for p in parts:
            low = p.lower().replace("ё", "е")
            if len(low) >= 4:
                q_parts.append(f"{low[:-1]}*")
            else:
                q_parts.append(low)
        fts_query = " ".join(q_parts)
        try:
            rows = conn.execute(
                "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", (fts_query,)
            ).fetchall()
        except sqlite3.OperationalError as exc:
            rows = []
            print(f"  [{label}] ERROR: {exc}", file=sys.stderr)
        got = any(r[0] == i for r in rows)
        if should and got:
            hits.append(i)
        elif should and not got:
            misses.append(i)
        elif (not should) and got:
            false_hits.append(i)
    conn.close()
    return {"hits": hits, "misses": misses, "false_hits": false_hits}


def main() -> int:
    positives = sum(1 for _, _, s, _ in CORPUS if s)
    negatives = len(CORPUS) - positives

    results: dict[str, dict] = {}

    print(f"sqlite: {sqlite3.sqlite_version}, python: {sys.version.split()[0]}")
    print(f"corpus: {len(CORPUS)} cases ({positives} positive, {negatives} negative)")
    print()

    results["1_plan_default"] = approach_plain(
        "plan-default", "porter unicode61 remove_diacritics 2"
    )
    results["2_plan_fallback"] = approach_plain(
        "plan-fallback", "unicode61 remove_diacritics 2"
    )
    results["3_pystem_index_and_query"] = approach_pystem_index_and_query("pystem-index")
    results["4_pystem_query_wildcard"] = approach_query_wildcard_pystem("pystem-wildcard")
    results["5_naive_prefix_wildcard"] = approach_query_wildcard_naive("naive-prefix")

    # Summary table
    lines: list[str] = []
    lines.append(
        f"{'approach':<35} {'recall':>10} {'precision':>10} {'hits/pos':>12} {'false_hits':>12}"
    )
    lines.append("-" * 85)
    for name, r in results.items():
        tp = len(r["hits"])
        fn = len(r["misses"])
        fp = len(r["false_hits"])
        recall = tp / positives if positives else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        lines.append(
            f"{name:<35} {recall:>10.2%} {precision:>10.2%} {tp:>6}/{positives:<5} {fp:>12}"
        )

    print("\n".join(lines))
    print()

    # Detail miss/false-hit cases for top-3 approaches
    for name in ("1_plan_default", "2_plan_fallback", "4_pystem_query_wildcard", "3_pystem_index_and_query"):
        r = results[name]
        if r["misses"] or r["false_hits"]:
            print(f"[{name}]")
            for i in r["misses"]:
                _, q, _, label = CORPUS[i]
                print(f"  MISS   query={q!r}  ({label})")
            for i in r["false_hits"]:
                _, q, _, label = CORPUS[i]
                print(f"  FP     query={q!r}  ({label})")
            print()

    OUT_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    OUT_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"Wrote: {OUT_TXT}\nWrote: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
