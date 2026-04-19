"""Phase 7 / commit 11 — Bash hook allowlist for the genimage CLI.

Covers:
  * required flag pair (`--prompt`/`--out`);
  * enum / range validators (size, steps, seed, timeout, daily-cap);
  * `--endpoint` loopback-only shape;
  * `--out` absolute + `.png` suffix, bound under
    `<data_dir>/media/outbox/` when `data_dir` is passed;
  * dup-flag deny.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "genimage").mkdir(parents=True)
    (tmp_path / "tools" / "genimage" / "main.py").write_text("# stub\n")
    return tmp_path


@pytest.fixture
def data_dir() -> Path:
    # The Bash slip-guard regex matches runs of 48+ [A-Za-z0-9+/] chars —
    # pytest's default tmp path (/private/var/folders/.../pytest-of-.../...)
    # easily exceeds that threshold. Tests that embed the path into a
    # command line therefore need a SHORT absolute base. We provision it
    # under /tmp/ so the full path stays well under the slip-guard threshold.
    # OS tmp reaper cleans /tmp periodically; fine for test scratch dirs.
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="p7g-", dir="/tmp"))
    d = base / "d"
    (d / "media" / "outbox").mkdir(parents=True)
    (d / "run").mkdir(parents=True)
    return d


# ---------------------------------------------------------------- ALLOW (no data_dir)


def test_allow_minimal_no_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "a cat" --out /abs/x.png',
        project_root,
    )
    assert reason is None, reason


def test_allow_full_flags_no_data_dir(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "cat" --out /abs/x.png '
        "--width 512 --height 512 --steps 8 --seed 42 --timeout-s 120 "
        "--endpoint http://127.0.0.1:9101/generate --daily-cap 2",
        project_root,
    )
    assert reason is None, reason


# ---------------------------------------------------------------- ALLOW (with data_dir)


def test_allow_out_under_outbox(project_root: Path, data_dir: Path) -> None:
    out = data_dir / "media" / "outbox" / "pic.png"
    reason = check_bash_command(
        f'python tools/genimage/main.py --prompt "p" --out {out}',
        project_root,
        data_dir=data_dir,
    )
    assert reason is None, reason


def test_deny_out_outside_outbox(project_root: Path, data_dir: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /tmp/pic.png',
        project_root,
        data_dir=data_dir,
    )
    assert reason is not None
    assert "outbox" in reason


# ---------------------------------------------------------------- DENY


def test_deny_missing_prompt(project_root: Path) -> None:
    reason = check_bash_command("python tools/genimage/main.py --out /abs/x.png", project_root)
    assert reason is not None
    assert "--prompt" in reason


def test_deny_missing_out(project_root: Path) -> None:
    reason = check_bash_command('python tools/genimage/main.py --prompt "cat"', project_root)
    assert reason is not None
    assert "--out" in reason


def test_deny_out_wrong_suffix(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /abs/x.jpg', project_root
    )
    assert reason is not None
    assert ".png" in reason


def test_deny_prompt_too_long(project_root: Path) -> None:
    big = "x" * 2000
    reason = check_bash_command(
        f'python tools/genimage/main.py --prompt "{big}" --out /abs/x.png',
        project_root,
    )
    assert reason is not None
    assert "exceeds" in reason


def test_deny_bad_size(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /abs/x.png --width 333',
        project_root,
    )
    assert reason is not None
    assert "--width" in reason


def test_deny_steps_out_of_range(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /abs/x.png --steps 99',
        project_root,
    )
    assert reason is not None
    assert "--steps" in reason


def test_deny_duplicate_prompt(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "a" --prompt "b" --out /abs/x.png',
        project_root,
    )
    assert reason is not None
    assert "duplicate" in reason


def test_deny_non_loopback_endpoint(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /abs/x.png --endpoint http://8.8.8.8/gen',
        project_root,
    )
    assert reason is not None
    assert "loopback" in reason


def test_deny_unknown_flag(project_root: Path) -> None:
    reason = check_bash_command(
        'python tools/genimage/main.py --prompt "p" --out /abs/x.png --model flux',
        project_root,
    )
    assert reason is not None
