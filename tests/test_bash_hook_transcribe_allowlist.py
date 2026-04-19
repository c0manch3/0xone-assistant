"""Phase 7 / commit 11 — Bash hook allowlist for the transcribe CLI.

Two layers compose:
  1. `_validate_python_invocation` enforces the `tools/` prefix and no
     `..` traversal (inherited from phase 3).
  2. `_validate_transcribe_argv` enforces structural shape: exactly one
     positional `<path>` (absolute), `--language`/`--format` enums,
     `--timeout-s` range, `--endpoint` loopback-only, no duplicate flags.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "transcribe").mkdir(parents=True)
    (tmp_path / "tools" / "transcribe" / "main.py").write_text("# stub\n")
    return tmp_path


# ---------------------------------------------------------------- ALLOW

ALLOW_CASES = [
    "python tools/transcribe/main.py /abs/audio.ogg",
    "python tools/transcribe/main.py /abs/audio.ogg --language ru",
    "python tools/transcribe/main.py /abs/audio.ogg --language en --format text",
    "python tools/transcribe/main.py /abs/audio.ogg --timeout-s 60 --format segments",
    "python tools/transcribe/main.py /abs/audio.ogg --endpoint http://127.0.0.1:9100/transcribe",
    "python tools/transcribe/main.py /abs/audio.ogg --endpoint http://localhost:9100/transcribe",
    "python tools/transcribe/main.py /abs/audio.ogg --endpoint http://[::1]:9100/transcribe",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
def test_allowlist_allow(cmd: str, project_root: Path) -> None:
    reason = check_bash_command(cmd, project_root)
    assert reason is None, f"{cmd!r}: expected ALLOW, got DENY: {reason!r}"


# ---------------------------------------------------------------- DENY


def test_deny_missing_positional(project_root: Path) -> None:
    reason = check_bash_command("python tools/transcribe/main.py --language ru", project_root)
    assert reason is not None
    assert "positional" in reason


def test_deny_relative_positional(project_root: Path) -> None:
    reason = check_bash_command("python tools/transcribe/main.py audio.ogg", project_root)
    assert reason is not None
    assert "absolute" in reason


def test_deny_dotdot_positional(project_root: Path) -> None:
    reason = check_bash_command("python tools/transcribe/main.py /abs/../etc/passwd", project_root)
    assert reason is not None
    assert ".." in reason


def test_deny_unknown_language(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --language fr", project_root
    )
    assert reason is not None
    assert "--language" in reason


def test_deny_unknown_format(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --format json", project_root
    )
    assert reason is not None
    assert "--format" in reason


def test_deny_timeout_out_of_range(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --timeout-s 5", project_root
    )
    assert reason is not None
    assert "--timeout-s" in reason


def test_deny_non_loopback_endpoint(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --endpoint http://evil.example/x",
        project_root,
    )
    assert reason is not None
    assert "loopback" in reason


def test_deny_duplicate_flag(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --language ru --language en",
        project_root,
    )
    assert reason is not None
    assert "duplicate" in reason


def test_deny_unknown_flag(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg --model whisper",
        project_root,
    )
    assert reason is not None


def test_deny_shell_metachar(project_root: Path) -> None:
    reason = check_bash_command(
        "python tools/transcribe/main.py /abs/a.ogg ; rm -rf /",
        project_root,
    )
    assert reason is not None
