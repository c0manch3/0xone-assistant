"""Phase 7 / commit 6 — `ARTEFACT_RE` corpus parity with spike S-2.

This file is the test-suite projection of
`spikes/phase7_s2_artefact_regex.py`. The 46-case corpus is ported
VERBATIM so a regression in `media/artefacts.py::ARTEFACT_RE` is
caught without re-running the spike. Per plan §4.2 + §7 acceptance:
the regex is v3 and the three known residual failures are marked
``xfail`` with the spike's rationale.

The corpus exercises:
  * Basic positives (7 cases across photo / audio / document exts).
  * Trailing punctuation (`.`, `,`, `!`, `?`).
  * Markdown / bracket / quote / backtick / angle wrappers.
  * URL false-positives (HTTPS + IPv6) — MUST NOT match.
  * Code-fence, multi-artefact, adjacent paths.
  * Cyrillic filenames, whitespace inside filename.
  * Relative / `./` paths.
  * Case-insensitivity for the extension.
  * Emoji inside/outside the filename.
  * Boundary cases (zero-width space, colon-before).
  * Scheduler system-note style output.
  * Path-with-query-string edge (treated as trailing noise).

Known xfails (see spike report `phase7_s2_report.json` §
``remaining_failures_acceptable``):
  1. ``adjacent_paths`` — two abs paths with no separator.
  2. ``colon_before``  — no space after colon (SKILL.md H-13 rule
     instructs the model to always add a space).
  3. ``nested_path``   — `/x.png/y` — regex extracts `/x.png`, trailing
     `y` treated as noise (downstream `exists()` check catches it).
"""

from __future__ import annotations

import pytest

from assistant.media.artefacts import ARTEFACT_RE

# --- Corpus (46 cases, mirrors spikes/phase7_s2_artefact_regex.py) --

# Each row: (case_id, text, expected_matches).
# Keep the list order identical to the spike so a diff between here
# and `spikes/phase7_s2_artefact_regex.py::CORPUS` is trivial.
_CORPUS: list[tuple[str, str, list[str]]] = [
    # --- BASIC POSITIVES ---
    ("basic_png", "готово: /abs/outbox/file.png", ["/abs/outbox/file.png"]),
    ("basic_pdf", "см /abs/outbox/report.pdf", ["/abs/outbox/report.pdf"]),
    ("basic_docx", "document at /abs/outbox/doc.docx", ["/abs/outbox/doc.docx"]),
    ("basic_mp3", "audio /abs/outbox/voice.mp3", ["/abs/outbox/voice.mp3"]),
    ("basic_txt", "text at /abs/outbox/note.txt", ["/abs/outbox/note.txt"]),
    # --- TRAILING PUNCT ---
    ("trailing_period", "готово: /abs/outbox/x.png.", ["/abs/outbox/x.png"]),
    ("trailing_comma", "вот /abs/outbox/x.png, готово", ["/abs/outbox/x.png"]),
    ("trailing_excl", "смотри /abs/outbox/x.pdf!", ["/abs/outbox/x.pdf"]),
    ("trailing_qmark", "это /abs/outbox/x.pdf?", ["/abs/outbox/x.pdf"]),
    # --- MARKDOWN / PAREN WRAPPERS ---
    ("md_link", "[caption](/abs/outbox/x.png)", ["/abs/outbox/x.png"]),
    (
        "md_link_pdf",
        "see [the PDF](/abs/outbox/report.pdf) attached",
        ["/abs/outbox/report.pdf"],
    ),
    ("parenthesised", "(cf. /abs/outbox/x.png)", ["/abs/outbox/x.png"]),
    ("backtick_wrap", "path: `/abs/outbox/x.png`", ["/abs/outbox/x.png"]),
    ("angle_wrap", "<path>/abs/outbox/x.png</path>", ["/abs/outbox/x.png"]),
    ("double_quote", 'path: "/abs/outbox/x.png"', ["/abs/outbox/x.png"]),
    ("single_quote", "path: '/abs/outbox/x.png'", ["/abs/outbox/x.png"]),
    # --- URL-CONTAINING PATHS (false positive to avoid) ---
    ("url_with_ext", "https://host.com/abs/outbox/x.png", []),
    ("url_pdf", "https://example.com/docs/report.pdf", []),
    # --- CODE FENCE (accept path inside) ---
    ("code_fence_triple", "```\n/abs/outbox/x.png\n```", ["/abs/outbox/x.png"]),
    # --- MULTI-ARTEFACT ---
    (
        "two_paths_newline",
        "first /abs/outbox/a.png\nsecond /abs/outbox/b.pdf",
        ["/abs/outbox/a.png", "/abs/outbox/b.pdf"],
    ),
    (
        "two_paths_comma",
        "паки: /abs/outbox/a.png, /abs/outbox/b.pdf",
        ["/abs/outbox/a.png", "/abs/outbox/b.pdf"],
    ),
    # `adjacent_paths` is the first of 3 accepted residual failures.
    ("adjacent_paths", "/abs/x.png/abs/y.pdf", []),
    # --- CYRILLIC FILENAMES ---
    (
        "cyrillic_filename",
        "готово: /abs/outbox/документ.pdf",
        ["/abs/outbox/документ.pdf"],
    ),
    (
        "cyrillic_surrounding",
        "результат: /abs/outbox/отчёт.docx — всё готово",
        ["/abs/outbox/отчёт.docx"],
    ),
    # --- PATH WITH SPACES (should NOT match) ---
    ("path_with_space", "готово: /abs/outbox/my file.png", []),
    # --- RELATIVE PATHS ---
    ("relative_path", "see outbox/x.png please", []),
    ("dot_slash", "see ./outbox/x.png please", []),
    # --- DOUBLE EXTENSION ---
    ("double_ext_tar_gz", "archive /abs/out/x.tar.gz", []),
    ("zip_upper", "file /abs/out/X.ZIP", []),
    # --- EMOJI / UNICODE ---
    (
        "emoji_before",
        "🎉 /abs/outbox/party.png готово",
        ["/abs/outbox/party.png"],
    ),
    (
        "emoji_in_filename",
        "/abs/outbox/party🎉.png",
        ["/abs/outbox/party🎉.png"],
    ),
    # --- EXT CASE INSENSITIVE ---
    ("ext_upper", "see /abs/outbox/X.PNG now", ["/abs/outbox/X.PNG"]),
    ("ext_mixed", "see /abs/outbox/X.Png now", ["/abs/outbox/X.Png"]),
    # --- BOUNDARY CASES ---
    # `colon_before` is the second of 3 accepted residual failures.
    ("colon_before", "готово:/abs/outbox/x.png", ["/abs/outbox/x.png"]),
    ("zero_width_before", "\u200b/abs/outbox/x.png", ["/abs/outbox/x.png"]),
    # --- IPv6-looking URL (false positive potential) ---
    ("ipv6_url_with_path", "from http://[::1]:9100/abs/outbox/x.png", []),
    # --- MULTIPLE LINES ---
    (
        "multiline_triple",
        "первый: /abs/outbox/a.png\nвторой: /abs/outbox/b.pdf\nтретий: /abs/outbox/c.mp3",
        ["/abs/outbox/a.png", "/abs/outbox/b.pdf", "/abs/outbox/c.mp3"],
    ),
    # --- RUSSIAN MIXED SENTENCE ---
    (
        "mixed_cyr_ascii",
        "документ сохранён /abs/outbox/report_2026-04-17.pdf и готов",
        ["/abs/outbox/report_2026-04-17.pdf"],
    ),
    # --- NO ARTEFACT ---
    ("no_artefact", "просто текст без путей", []),
    ("slash_but_no_ext", "/abs/outbox/file", []),
    ("abs_path_unsupported_ext", "/abs/outbox/binary.exe", []),
    # --- SCHEDULER NOTES ---
    (
        "scheduler_with_path",
        "[system-note: owner sent /abs/outbox/x.png]",
        ["/abs/outbox/x.png"],
    ),
    # --- ADJACENT EXT INSIDE WORDS ---
    ("ext_inside_word", "file.png notation", []),
    ("ext_inside_word_abs_word", "/file.pngword", []),
    # --- PATH WITH QUERY STRING ---
    ("path_with_query", "/abs/outbox/x.png?foo=1", ["/abs/outbox/x.png"]),
    # --- NESTED PATH (third of 3 accepted residual failures) ---
    ("nested_path", "/abs/outbox/x.png/y", []),
]


# Cases whose "ideal" expectation differs from v3 regex behaviour but
# where the residual is deemed acceptable per spike S-2 report §
# ``remaining_failures_acceptable``. Keeping them as xfail (strict=False)
# means:
#   * A v3 regex tightening that flips them to PASS will show up in
#     the pytest output as XPASS — visible but non-fatal.
#   * The 43/46 corpus gate remains enforced by the regular cases.
_ACCEPTED_RESIDUAL_FAILURES: dict[str, str] = {
    "adjacent_paths": (
        "spike S-2: '/abs/x.png/abs/y.pdf' is pathological model output "
        "— regex extracts first path; downstream exists() rejects the "
        "concatenated second path."
    ),
    "colon_before": (
        "spike S-2: 'готово:/abs/outbox/x.png' — v3 rejects colon prefix "
        "to avoid URL false positives; SKILL.md H-13 instructs the model "
        "to always add a space after ':'."
    ),
    "nested_path": (
        "spike S-2: '/abs/outbox/x.png/y' — v3 extracts '/abs/outbox/x.png' "
        "as the artefact; downstream exists() filters trailing noise."
    ),
}


@pytest.mark.parametrize(
    ("case_id", "text", "expected"),
    [
        pytest.param(
            cid,
            txt,
            exp,
            id=cid,
            marks=(
                [pytest.mark.xfail(reason=_ACCEPTED_RESIDUAL_FAILURES[cid])]
                if cid in _ACCEPTED_RESIDUAL_FAILURES
                else []
            ),
        )
        for cid, txt, exp in _CORPUS
    ],
)
def test_artefact_re_corpus(case_id: str, text: str, expected: list[str]) -> None:
    """Port of spike S-2 corpus — 43/46 passing + 3 documented xfails."""
    actual = ARTEFACT_RE.findall(text)
    assert actual == expected, (
        f"case {case_id}: text={text!r} expected={expected} actual={actual}"
    )


def test_corpus_size_matches_spike() -> None:
    # Tripwire for drift: if someone adds a case here without updating
    # the spike (or vice versa), the 46-case contract breaks explicitly.
    assert len(_CORPUS) == 46


def test_xfail_count_matches_spike() -> None:
    # Exactly 3 documented xfails per plan §4.2 ("3 known failures
    # marked xfail"). More than 3 means the regex regressed; fewer
    # means we fixed one without updating the docstring.
    assert len(_ACCEPTED_RESIDUAL_FAILURES) == 3
