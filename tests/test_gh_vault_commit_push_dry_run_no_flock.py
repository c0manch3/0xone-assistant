"""SF-B3 — ``vault-commit-push --dry-run`` bypasses the flock entirely.

Rationale: dry-run is a read-only operation (reports porcelain + the
commit message it WOULD send). Holding the flock during dry-run would
mean the owner can't inspect the vault state whenever the scheduler has
a long-running commit in flight — poor UX for a diagnostic command.

Test design: spawn a child process holding the flock for ≥5 s, then from
the parent run ``vault-commit-push --dry-run``. The parent MUST NOT
return exit 9 (LOCK_BUSY) — it should complete normally with rc 0 and a
``dry_run: true`` JSON payload.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main
from tools.gh._lib.lock import flock_exclusive_nb


def _hold_lock(lock_path: str, ready_file: str, release_file: str) -> None:
    """Hold the flock while the parent exercises the dry-run path."""
    with flock_exclusive_nb(Path(lock_path)):
        Path(ready_file).write_text("1")
        while not Path(release_file).exists():
            time.sleep(0.01)


def test_dry_run_ignores_lock_holder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Parent runs ``--dry-run`` while child holds lock; rc must be 0, not 9."""
    env = install_file_remote(monkeypatch, tmp_path)

    # `_cmd_vault_commit_push` computes lock_path from `_data_dir()`; we
    # replicate the path here so the child's flock hits the SAME file the
    # CLI would inspect.
    lock_path = env.data_dir / "run" / "gh-vault-commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ready = tmp_path / "ready"
    release = tmp_path / "release"

    ctx = mp.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock, args=(str(lock_path), str(ready), str(release))
    )
    proc.start()
    try:
        # Wait for the child to actually hold the lock before running
        # the parent CLI — otherwise a race could let parent acquire
        # first and we'd be testing nothing.
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            if time.monotonic() > deadline:
                pytest.fail("child did not acquire lock within 5s")
            time.sleep(0.01)

        # Dry-run from the parent MUST succeed.
        rc = gh_main.main(["vault-commit-push", "--dry-run"])
        assert rc == 0, f"dry-run expected 0 even with lock held, got {rc}"

        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
    finally:
        release.write_text("1")
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)


def test_write_run_blocked_while_lock_held(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Sanity contrapositive: the NON-dry-run does exit 9 while the lock is held.

    This proves the dry-run test above is non-trivial — the lock is
    real and does block the write path.
    """
    env = install_file_remote(monkeypatch, tmp_path)
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("content\n")

    # Pre-create the run dir so the lock path lines up.
    lock_path = env.data_dir / "run" / "gh-vault-commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    ready = tmp_path / "ready"
    release = tmp_path / "release"

    ctx = mp.get_context("spawn")
    proc = ctx.Process(
        target=_hold_lock, args=(str(lock_path), str(ready), str(release))
    )
    proc.start()
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            if time.monotonic() > deadline:
                pytest.fail("child did not acquire lock within 5s")
            time.sleep(0.01)

        rc = gh_main.main(["vault-commit-push"])
        assert rc == 9, f"write-path expected LOCK_BUSY (9), got {rc}"
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["ok"] is False
        assert payload["error"] == "lock_busy"
    finally:
        release.write_text("1")
        proc.join(timeout=5.0)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2.0)
