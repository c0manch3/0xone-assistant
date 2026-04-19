#!/usr/bin/env python3
"""Phase 8 R-13 / Q11: assert `ARTEFACT_RE` v3 false-positive rate = 0 on gh CLI corpus.

Assembles a synthetic corpus of 30+ model-reply strings that would plausibly
be emitted by the phase-8 scheduler-turn (or main-turn) AFTER the CLI
finishes `vault-commit-push`, `issue create`, `pr view`, etc. None of
these strings should contain an extraction-eligible outbox artefact path
(because gh CLI never writes to `<data_dir>/media/outbox/`).

Uses the same pattern that lives in
`src/assistant/media/artefacts.py::ARTEFACT_RE` to ensure parity.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ZW_TERMINATORS = "\u200b\u200c\u200d"
_PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO_EXT = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac")
_DOC_EXT = (".pdf", ".docx", ".txt", ".xlsx", ".rtf")
_ALL_EXT = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

ARTEFACT_RE = re.compile(
    rf"(?<![\w/.:])(/[^\s`\"'<>()\[\]{_ZW_TERMINATORS}]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    rf"(?=[\s`\"'<>()\[\].,;:!?/{_ZW_TERMINATORS}]|$)",
    re.IGNORECASE | re.UNICODE,
)

CORPUS = [
    # 1-10: commit-sha / git shortlog style.
    "vault сохранён, 3 файла, sha=abc1234",
    "vault сохранён, 3 файла, commit_sha=abc1234def567",
    "готово: commit abc1234def on main",
    "3 files changed, 10 insertions(+), 2 deletions(-)",
    " M data/vault/note.md",
    "?? data/vault/new.md",
    "A  data/vault/added.md",
    "data/vault/note.md | 3 +++",
    "data/vault/note.md | 3 +++\n 1 file changed, 3 insertions(+)",
    "* main abc1234def commit message",

    # 11-20: JSON pass-through.
    '{"ok":true,"commit_sha":"abc1234","files_changed":3}',
    '{"ok":false,"error":"remote has diverged"}',
    '{"url":"https://github.com/owner/repo/issues/42","number":42}',
    '{"url":"https://github.com/owner/repo/pull/15","state":"open"}',
    '{"title":"bug report","body":"see attached","labels":[]}',
    '[{"number":1,"title":"open"},{"number":2,"title":"done"}]',
    '{"ok":false,"error":"ssh_key_error","path":"~/.ssh/id_vault"}',
    '{"error":"repo_not_allowed"}',
    '{"commit_sha":"deadbeef12345","files_changed":0}',
    '{"timestamp":"2026-04-19T03:00:00Z","status":"sent"}',

    # 21-30: Russian prose from scheduler-turn model.
    "ежедневный бэкап vault завершён",
    "сохранил vault, получил SHA abc1234",
    "запушил изменения в vault-backup",
    "vault без изменений, ничего не коммичу",
    "remote расходится, нужен ручной разбор",
    "deploy key не найден: ~/.ssh/id_vault",
    "issue создан: https://github.com/c0manch3/0xone-assistant/issues/42",
    "PR #15 открыт: проверь merge-status",
    "ветка main на remote, два коммита впереди",
    "сделал git add data/vault, коммит, git push",

    # 31-40: error strings from gh CLI / git.
    "gh: couldn't authenticate; run `gh auth login`",
    "error: failed to push some refs to 'git@github.com:owner/vault.git'",
    "hint: Updates were rejected because the tip of your current branch is behind",
    "To git@github.com:owner/vault-backup.git\n ! [rejected]        main -> main (non-fast-forward)",
    "fatal: not a git repository (or any of the parent directories): .git",
    "Everything up-to-date",
    "Enumerating objects: 5, done.\nCounting objects: 100% (5/5), done.",
    "Writing objects: 100% (3/3), 520 bytes | 520.00 KiB/s, done.",
    "Total 3 (delta 1), reused 0 (delta 0), pack-reused 0",
    "To github.com:owner/repo.git\n   abc123..def456  main -> main",

    # 41-50: tricky near-miss strings (URLs, hash-like tokens, extensions
    # that are NOT outbox-pathed).
    "https://github.com/owner/repo/blob/main/README.md",
    "https://github.com/owner/repo/actions/runs/12345.png",
    "см. docs/ops/github-setup.md",
    "откройте ~/.ssh/id_vault.pub",
    "put a file at data/vault/images/chart.png if you want, but this one is no-op",
    "/Users/me/Projects/x.md is only referenced inside a .git internal",
    "Q: что-то.txt тоже пойдёт в commit?",
    "сгенерю диаграмму позже (chart.png)",
    "logs from http://example.com/stuff.pdf — не наш artefact",
    "relative path ./data/vault/note.md",
]


def main() -> int:
    results: list[dict[str, object]] = []
    for idx, text in enumerate(CORPUS, 1):
        matches = ARTEFACT_RE.findall(text)
        results.append({"idx": idx, "text": text, "matches": matches})

    false_positives = [r for r in results if r["matches"]]
    report = {
        "corpus_size": len(CORPUS),
        "false_positive_count": len(false_positives),
        "false_positives": false_positives,
        "all_results": results,
    }

    out_json = Path(__file__).with_name("spike_artefact_re_corpus_report.json")
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    out_corpus = Path(__file__).with_name("spike_artefact_re_corpus.txt")
    out_corpus.write_text("\n".join(CORPUS), encoding="utf-8")
    print(
        f"corpus={len(CORPUS)} false_positives={len(false_positives)}"
    )
    if false_positives:
        for fp in false_positives:
            print("FP:", fp)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
