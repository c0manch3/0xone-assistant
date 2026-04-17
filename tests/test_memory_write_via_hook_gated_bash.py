"""B-CRIT-1 E2E: write-first-then-invoke pattern clears the Bash hook.

The phase 4 v1 contract `echo "..." | memory write --body -` was
unreachable from the model because `_SHELL_METACHARS` rejects `|`. The
review fix introduces `--body-file` and the two-step pattern:

    1. Write(file_path="data/run/memory-stage/stage-<id>.md", content="...")
    2. Bash(command="python tools/memory/main.py write inbox/x.md
            --title X --body-file data/run/memory-stage/stage-<id>.md")

This test walks the bash hook (real phase-2 validator) and then invokes
the CLI as a subprocess to prove the full flow lands a note.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _PROJECT_ROOT / "tools" / "memory" / "main.py"


@pytest.fixture
def stage_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Use the real project-root stage dir so `--body-file` path-guard
    resolves to the same allowlisted location the model would use.

    We clean up just the specific file we write; any other stage files
    the daemon (or another test) may have are untouched.
    """
    stage = _PROJECT_ROOT / "data" / "run" / "memory-stage"
    stage.mkdir(parents=True, exist_ok=True, mode=0o700)
    return stage


def test_bash_hook_allows_write_first_pattern(stage_dir: Path) -> None:
    """Assertion 1: the bash argv for `memory write --body-file` passes
    the phase-2 allowlist exactly as emitted by the model.
    """
    stage_file = stage_dir / "stage-hook-test.md"
    stage_file.write_text("body with | pipes | inside", encoding="utf-8")
    try:
        cmd = (
            "python tools/memory/main.py write inbox/test.md "
            "--title Test --body-file data/run/memory-stage/stage-hook-test.md"
        )
        reason = check_bash_command(cmd, _PROJECT_ROOT)
        assert reason is None, f"hook rejected clean command: {reason}"
    finally:
        stage_file.unlink(missing_ok=True)


def test_write_first_pattern_end_to_end(
    tmp_path: Path, stage_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assertion 2: stage + subprocess → note created, stage auto-cleaned."""
    stage_file = stage_dir / "stage-e2e.md"
    stage_file.write_text("remember this fact", encoding="utf-8")

    vault = tmp_path / "vault"
    idx = tmp_path / "idx.db"
    env = {
        **_inherit_env(),
        "MEMORY_VAULT_DIR": str(vault),
        "MEMORY_INDEX_DB_PATH": str(idx),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(_MAIN),
            "write",
            "inbox/e2e.md",
            "--title",
            "E2E",
            "--body-file",
            "data/run/memory-stage/stage-e2e.md",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True
    note = vault / "inbox" / "e2e.md"
    assert note.exists()
    assert "remember this fact" in note.read_text(encoding="utf-8")
    # Stage file was unlinked on success.
    assert not stage_file.exists(), "stage file should have been auto-cleaned"


def test_bash_hook_rejects_pipe_form(tmp_path: Path) -> None:
    """Regression: the old pipe form `echo ... | memory write --body -`
    still has to be rejected by the bash hook — confirming why we need
    the write-first pattern in the first place.
    """
    cmd = "echo hello | python tools/memory/main.py write inbox/a.md --title T --body -"
    reason = check_bash_command(cmd, _PROJECT_ROOT)
    assert reason is not None
    assert "|" in reason or "metacharacter" in reason.lower()


def test_body_file_outside_stage_dir_rejected(tmp_path: Path, stage_dir: Path) -> None:
    """The CLI's --body-file must be inside data/run/memory-stage/.

    Feeding an absolute path elsewhere (e.g. /tmp) should exit 3
    (validation), protecting against model-driven arbitrary file reads.
    """
    stray = tmp_path / "stray.md"
    stray.write_text("should not be read", encoding="utf-8")
    env = {
        **_inherit_env(),
        "MEMORY_VAULT_DIR": str(tmp_path / "v"),
        "MEMORY_INDEX_DB_PATH": str(tmp_path / "i.db"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(_MAIN),
            "write",
            "inbox/a.md",
            "--title",
            "T",
            "--body-file",
            str(stray),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    err = json.loads(proc.stderr.strip())
    assert "must live under" in err["error"]


def test_body_group_missing_exits_usage(tmp_path: Path) -> None:
    """Neither --body nor --body-file → exit 2 (usage)."""
    env = {
        **_inherit_env(),
        "MEMORY_VAULT_DIR": str(tmp_path / "v"),
        "MEMORY_INDEX_DB_PATH": str(tmp_path / "i.db"),
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(_MAIN),
            "write",
            "inbox/a.md",
            "--title",
            "T",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)


def _inherit_env() -> dict[str, str]:
    """Keep PATH/HOME/etc so uv-managed Python can find site-packages."""
    import os

    return dict(os.environ)
