"""Phase 7 / commit 11 — Bash hook allowlist for the extract_doc CLI.

Covers:
  * positional `<path>` — no `..`, relative accepted inside project_root,
    absolute accepted (CLI does its own `is_file()` check);
  * `--max-chars` integer + CLI hard cap;
  * `--pages` `^N(-M)?$` with 1-based ascending range;
  * dup-flag deny, unknown-flag deny, metachar deny.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "extract_doc").mkdir(parents=True)
    (tmp_path / "tools" / "extract_doc" / "main.py").write_text("# stub\n")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    "python tools/extract_doc/main.py /abs/file.pdf",
    "python tools/extract_doc/main.py /abs/file.docx --max-chars 1000",
    "python tools/extract_doc/main.py /abs/file.pdf --pages 3",
    "python tools/extract_doc/main.py /abs/file.pdf --pages 3-10",
    "python tools/extract_doc/main.py /abs/file.pdf --max-chars 500 --pages 1-3",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_allowlist_allow(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"{cmd!r}: expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY


def test_deny_missing_positional(project_root: Path) -> None:
    reason = check_bash_command("python tools/extract_doc/main.py --max-chars 500", project_root)
    assert reason is not None
    assert "positional" in reason


def test_deny_dotdot_in_path(project_root: Path) -> None:
    reason = check_bash_command("python tools/extract_doc/main.py /abs/../etc/passwd", project_root)
    assert reason is not None
    assert ".." in reason


def test_deny_max_chars_over_cap(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --max-chars 9999999", project_root
    )
    assert reason is not None
    assert "--max-chars" in reason


def test_deny_max_chars_non_integer(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --max-chars big", project_root
    )
    assert reason is not None
    assert "integer" in reason


def test_deny_pages_bad_format(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --pages 3..10", project_root
    )
    assert reason is not None
    assert "--pages" in reason


def test_deny_pages_descending(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --pages 10-3", project_root
    )
    assert reason is not None
    assert "ascending" in reason


def test_deny_pages_zero_based(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --pages 0-5", project_root
    )
    assert reason is not None


def test_deny_duplicate_flag(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf --pages 1 --pages 2", project_root
    )
    assert reason is not None
    assert "duplicate" in reason


def test_deny_unknown_flag(project_root: Path) -> None:
    reason = check_bash_command("python tools/extract_doc/main.py /abs/x.pdf --ocr", project_root)
    assert reason is not None


def test_deny_shell_metachar(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/extract_doc/main.py /abs/x.pdf && cat /etc/passwd",
        project_root,
    )
    assert reason is not None
