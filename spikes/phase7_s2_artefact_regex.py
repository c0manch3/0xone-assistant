"""Phase 7 spike S-2 — ARTEFACT_RE corpus (devil Gap #5).

The regex lives in plan/phase7/detailed-plan.md §7:

    _PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")
    _AUDIO_EXT = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac")
    _DOC_EXT   = (".pdf", ".docx", ".txt", ".xlsx", ".rtf")
    _ALL_EXT   = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

    _ARTEFACT_RE = re.compile(
        r"(?<![\\w/])(/[^\\s`\"'<>()\\[\\]]+"
        rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
        r"(?![\\w/])",
        re.IGNORECASE,
    )

We exercise it with 40+ realistic model-output samples + edge cases.

We DO NOT enforce the path-guard in the regex; that's the caller's
responsibility via `resolve().is_relative_to(outbox_root)` + `exists()`.
So the regex may MATCH paths that later fail the path-guard; we record
that and label them CANDIDATE, not MATCH.

Run:  uv run python spikes/phase7_s2_artefact_regex.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s2_report.json"

_PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO_EXT = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac")
_DOC_EXT = (".pdf", ".docx", ".txt", ".xlsx", ".rtf")
_ALL_EXT = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

_ARTEFACT_RE_V1 = re.compile(
    r"(?<![\w/])(/[^\s`\"'<>()\[\]]+"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?![\w/])",
    re.IGNORECASE,
)

# v2: non-greedy body + explicit stop-set lookahead.
_ARTEFACT_RE_V2 = re.compile(
    r"(?<![\w/])(/[^\s`\"'<>()\[\]]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?=[\s`\"'<>()\[\].,;:!?]|$)",
    re.IGNORECASE,
)

# v3: widen lookbehind to reject dot and colon (URL scheme residues) and
# add a stop at a subsequent `/` after the extension so adjacent paths
# split cleanly. This is the tightest variant.
_ARTEFACT_RE_V3 = re.compile(
    r"(?<![\w/.:])(/[^\s`\"'<>()\[\]]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?=[\s`\"'<>()\[\].,;:!?/]|$)",
    re.IGNORECASE,
)


# Corpus — each case declares the pattern to probe and the EXPECTED match(es).
# "expected" is a list of path strings we expect the regex to extract,
# in order. `[]` means no match expected.
CORPUS: list[dict[str, object]] = [
    # --- BASIC POSITIVES ---
    {"id": "basic_png", "text": "готово: /abs/outbox/file.png", "expected": ["/abs/outbox/file.png"]},
    {"id": "basic_pdf", "text": "см /abs/outbox/report.pdf", "expected": ["/abs/outbox/report.pdf"]},
    {"id": "basic_docx", "text": "document at /abs/outbox/doc.docx", "expected": ["/abs/outbox/doc.docx"]},
    {"id": "basic_mp3", "text": "audio /abs/outbox/voice.mp3", "expected": ["/abs/outbox/voice.mp3"]},
    {"id": "basic_txt", "text": "text at /abs/outbox/note.txt", "expected": ["/abs/outbox/note.txt"]},
    # --- TRAILING PUNCT ---
    {"id": "trailing_period", "text": "готово: /abs/outbox/x.png.", "expected": ["/abs/outbox/x.png"]},
    {"id": "trailing_comma", "text": "вот /abs/outbox/x.png, готово", "expected": ["/abs/outbox/x.png"]},
    {"id": "trailing_excl", "text": "смотри /abs/outbox/x.pdf!", "expected": ["/abs/outbox/x.pdf"]},
    {"id": "trailing_qmark", "text": "это /abs/outbox/x.pdf?", "expected": ["/abs/outbox/x.pdf"]},
    # --- MARKDOWN / PAREN WRAPPERS ---
    {"id": "md_link", "text": "[caption](/abs/outbox/x.png)", "expected": ["/abs/outbox/x.png"]},
    {"id": "md_link_pdf", "text": "see [the PDF](/abs/outbox/report.pdf) attached", "expected": ["/abs/outbox/report.pdf"]},
    {"id": "parenthesised", "text": "(cf. /abs/outbox/x.png)", "expected": ["/abs/outbox/x.png"]},
    {"id": "backtick_wrap", "text": "path: `/abs/outbox/x.png`", "expected": ["/abs/outbox/x.png"]},
    {"id": "angle_wrap", "text": "<path>/abs/outbox/x.png</path>", "expected": ["/abs/outbox/x.png"]},
    {"id": "double_quote", "text": 'path: "/abs/outbox/x.png"', "expected": ["/abs/outbox/x.png"]},
    {"id": "single_quote", "text": "path: '/abs/outbox/x.png'", "expected": ["/abs/outbox/x.png"]},
    # --- URL-CONTAINING PATHS (false positive to avoid) ---
    {"id": "url_with_ext", "text": "https://host.com/abs/outbox/x.png", "expected": []},
    {"id": "url_pdf", "text": "https://example.com/docs/report.pdf", "expected": []},
    # --- CODE FENCE (accept the path inside) ---
    {"id": "code_fence_triple", "text": "```\n/abs/outbox/x.png\n```", "expected": ["/abs/outbox/x.png"]},
    # --- MULTI-ARTEFACT ---
    {"id": "two_paths_newline", "text": "first /abs/outbox/a.png\nsecond /abs/outbox/b.pdf", "expected": ["/abs/outbox/a.png", "/abs/outbox/b.pdf"]},
    {"id": "two_paths_comma", "text": "паки: /abs/outbox/a.png, /abs/outbox/b.pdf", "expected": ["/abs/outbox/a.png", "/abs/outbox/b.pdf"]},
    {"id": "adjacent_paths", "text": "/abs/x.png/abs/y.pdf", "expected": []},  # edge case — regex ambiguous; accept NO match to be safe
    # --- CYRILLIC FILENAMES ---
    {"id": "cyrillic_filename", "text": "готово: /abs/outbox/документ.pdf", "expected": ["/abs/outbox/документ.pdf"]},
    {"id": "cyrillic_surrounding", "text": "результат: /abs/outbox/отчёт.docx — всё готово", "expected": ["/abs/outbox/отчёт.docx"]},
    # --- PATH WITH SPACES (should NOT match — we require no-space chars) ---
    {"id": "path_with_space", "text": "готово: /abs/outbox/my file.png", "expected": []},
    # --- RELATIVE PATHS (reject — not abs) ---
    {"id": "relative_path", "text": "see outbox/x.png please", "expected": []},
    {"id": "dot_slash", "text": "see ./outbox/x.png please", "expected": []},
    # --- DOUBLE EXTENSION ---
    {"id": "double_ext_tar_gz", "text": "archive /abs/out/x.tar.gz", "expected": []},  # .gz not in allowlist
    {"id": "zip_upper", "text": "file /abs/out/X.ZIP", "expected": []},  # .zip not in allowlist
    # --- EMOJI / UNICODE (current regex uses \w which excludes emoji in ASCII mode) ---
    {"id": "emoji_before", "text": "🎉 /abs/outbox/party.png готово", "expected": ["/abs/outbox/party.png"]},
    {"id": "emoji_in_filename", "text": "/abs/outbox/party🎉.png", "expected": ["/abs/outbox/party🎉.png"]},
    # --- EXT CASE INSENSITIVE ---
    {"id": "ext_upper", "text": "see /abs/outbox/X.PNG now", "expected": ["/abs/outbox/X.PNG"]},
    {"id": "ext_mixed", "text": "see /abs/outbox/X.Png now", "expected": ["/abs/outbox/X.Png"]},
    # --- BOUNDARY CASES ---
    {"id": "colon_before", "text": "готово:/abs/outbox/x.png", "expected": ["/abs/outbox/x.png"]},
    {"id": "zero_width_before", "text": "\u200b/abs/outbox/x.png", "expected": ["/abs/outbox/x.png"]},
    # --- IPv6-looking URL (false positive potential) ---
    {"id": "ipv6_url_with_path", "text": "from http://[::1]:9100/abs/outbox/x.png", "expected": []},  # inside URL
    # --- MULTIPLE LINES ---
    {"id": "multiline_triple", "text": "первый: /abs/outbox/a.png\nвторой: /abs/outbox/b.pdf\nтретий: /abs/outbox/c.mp3", "expected": ["/abs/outbox/a.png", "/abs/outbox/b.pdf", "/abs/outbox/c.mp3"]},
    # --- RUSSIAN SENTENCE WITH MIXED CYRILLIC + ASCII ---
    {"id": "mixed_cyr_ascii", "text": "документ сохранён /abs/outbox/report_2026-04-17.pdf и готов", "expected": ["/abs/outbox/report_2026-04-17.pdf"]},
    # --- NO ARTEFACT (sanity) ---
    {"id": "no_artefact", "text": "просто текст без путей", "expected": []},
    {"id": "slash_but_no_ext", "text": "/abs/outbox/file", "expected": []},
    {"id": "abs_path_unsupported_ext", "text": "/abs/outbox/binary.exe", "expected": []},
    # --- SCHEDULER / URL NOTES WITH PATHS ---
    {"id": "scheduler_with_path", "text": "[system-note: owner sent /abs/outbox/x.png]", "expected": ["/abs/outbox/x.png"]},
    # --- ADJACENT EXT INSIDE WORDS ---
    {"id": "ext_inside_word", "text": "file.png notation", "expected": []},  # no leading /
    {"id": "ext_inside_word_abs_word", "text": "/file.pngword", "expected": []},  # would extend past ext
    # --- PATH WITH QUERY STRING (shouldn't happen for filesystem, but test) ---
    {"id": "path_with_query", "text": "/abs/outbox/x.png?foo=1", "expected": ["/abs/outbox/x.png"]},
    # --- MULTIPLE ADJACENT EXTS IN ONE WORD (edge) ---
    {"id": "nested_path", "text": "/abs/outbox/x.png/y", "expected": []},  # y follows path; ambiguous; safer to NOT match
]


def _run(regex: re.Pattern[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for case in CORPUS:
        text = str(case["text"])
        expected = list(case.get("expected") or [])
        matches = regex.findall(text)
        ok = matches == expected
        out.append(
            {
                "id": case["id"],
                "text": text,
                "expected": expected,
                "actual": matches,
                "pass": ok,
            }
        )
    return out


def main() -> None:
    v1 = _run(_ARTEFACT_RE_V1)
    v2 = _run(_ARTEFACT_RE_V2)
    v3 = _run(_ARTEFACT_RE_V3)

    def _summarize(rows: list[dict[str, object]]) -> dict[str, object]:
        passed = sum(1 for r in rows if r["pass"])
        failed = [r for r in rows if not r["pass"]]
        return {
            "passed": passed,
            "total": len(rows),
            "failures": failed,
        }

    summary = {
        "corpus_size": len(CORPUS),
        "v1_regex": _ARTEFACT_RE_V1.pattern,
        "v1_results": _summarize(v1),
        "v2_regex": _ARTEFACT_RE_V2.pattern,
        "v2_results": _summarize(v2),
        "v3_regex": _ARTEFACT_RE_V3.pattern,
        "v3_results": _summarize(v3),
        "all_details_v1": v1,
        "all_details_v2": v2,
        "all_details_v3": v3,
    }

    def _print_summary(tag: str, rows: list[dict[str, object]]) -> None:
        passed = sum(1 for r in rows if r["pass"])
        print(f"\n=== {tag} === {passed}/{len(rows)} passed")
        for r in rows:
            if not r["pass"]:
                print(f"  FAIL {r['id']}: expected={r['expected']}  actual={r['actual']}")
                print(f"       text={r['text']!r}")

    _print_summary("v1 (detailed-plan §7)", v1)
    _print_summary("v2 (non-greedy + stop-set lookahead)", v2)
    _print_summary("v3 (v2 + reject dot/colon before + slash stop)", v3)

    # Choose the regex with the most passes.
    scores = {
        "v1": summary["v1_results"]["passed"],
        "v2": summary["v2_results"]["passed"],
        "v3": summary["v3_results"]["passed"],
    }
    best = max(scores, key=lambda k: scores[k])
    summary["recommendation"] = best
    summary["scores"] = scores
    summary["verdict"] = "PASS" if scores[best] == len(CORPUS) else "PARTIAL"
    summary["remaining_failures_acceptable"] = [
        {
            "id": "adjacent_paths",
            "text": "/abs/x.png/abs/y.pdf",
            "v3_actual": ["/abs/x.png"],
            "reasoning": (
                "Model output '/abs/x.png/abs/y.pdf' is pathological — a "
                "real assistant reply would insert whitespace or a "
                "newline between two abs paths. If the model DID emit "
                "this, v3 extracts the first path; path-guard + exists() "
                "would deny the second since '/abs/x.png/abs/y.pdf' "
                "won't be a file. Accept as second-line-of-defense."
            ),
        },
        {
            "id": "colon_before",
            "text": "готово:/abs/outbox/x.png",
            "v3_actual": [],
            "reasoning": (
                "v3 rejects the colon prefix (to avoid URL false "
                "positives). Real assistant replies will almost always "
                "include a space after ':' — sampling shows this. "
                "Accept as minor false-negative; cost is one missed "
                "send, recoverable via re-prompt."
            ),
        },
        {
            "id": "nested_path",
            "text": "/abs/outbox/x.png/y",
            "v3_actual": ["/abs/outbox/x.png"],
            "reasoning": (
                "v3 extracts '/abs/outbox/x.png' as the artefact; 'y' "
                "is treated as trailing noise. Path-guard + exists() "
                "passes if x.png is a real file. If the model genuinely "
                "meant '/abs/outbox/x.png/y' as a nested path, that "
                "won't exist either way. Accept."
            ),
        },
    ]

    REPORT.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nRecommendation: {summary['recommendation']}  verdict={summary['verdict']}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    main()
