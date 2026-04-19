"""I-8.1 / I-8.8 — ``git add`` must NEVER pick up paths outside ``vault_dir``.

**CRITICAL**: this is the security-equivalent of the phase-7 outbox path
guard. If it ever fails, the owner's ``data/assistant.db`` (which
contains private scheduler + dispatch state) or ``data/media/outbox/``
(which contains user-shared photos/docs) could leak to the backup remote.

The test uses **real git**, no subprocess mocks for git — the entire
pipeline runs. The bare repo is a ``file://`` local receiver; after the
push we inspect the remote's HEAD tree to prove ONLY vault files landed.

Decoy files seeded outside ``vault_dir`` (but inside ``data_dir``):

- ``data/media/outbox/leak.png`` — should NEVER appear.
- ``data/assistant.db`` — should NEVER appear.
- ``data/run/tmp/junk.txt`` — should NEVER appear.

Decoy files inside ``vault_dir`` but excluded by ``.gitignore`` (SF-D7):

- ``vault/.tmp/scratch.md`` — the memory indexer's scratch dir.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers.gh_vault import install_file_remote
from tools.gh import main as gh_main


def _tree_files_at_head(bare_repo: Path, branch: str = "main") -> set[str]:
    """Return the set of paths tracked by ``refs/heads/<branch>`` in ``bare_repo``.

    We use ``git ls-tree -r --name-only`` so the assertion sees the
    actual object paths, not porcelain output which could include
    untracked or ignored files.
    """
    proc = subprocess.run(  # noqa: S603 — trusted git binary
        [
            "git", "-C", str(bare_repo),
            "ls-tree", "-r", "--name-only", f"refs/heads/{branch}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in proc.stdout.splitlines() if line.strip()}


def test_path_isolation_real_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No file outside ``vault_dir`` (or inside ``vault_dir/.tmp/``) reaches the bare repo."""
    env = install_file_remote(monkeypatch, tmp_path)

    # Seed the vault with a legitimate file AND a SF-D7 scratch file.
    env.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (env.vault_dir / "note.md").write_text("legit vault content\n")

    vault_tmp = env.vault_dir / ".tmp"
    vault_tmp.mkdir(parents=True, exist_ok=True)
    (vault_tmp / "scratch.md").write_text("memory-indexer scratch — MUST NOT ship\n")

    # Seed decoys OUTSIDE vault_dir but INSIDE data_dir. These are the
    # paths an attacker (or a bug) could smuggle onto the remote if the
    # handler forgot `git -C <vault_dir>`.
    media_outbox = env.data_dir / "media" / "outbox"
    media_outbox.mkdir(parents=True, exist_ok=True)
    (media_outbox / "leak.png").write_bytes(b"\x89PNG\r\n\x1a\nLEAKED BYTES")

    (env.data_dir / "assistant.db").write_bytes(b"SQLite\x00LEAKED\x00DB")

    run_tmp = env.data_dir / "run" / "tmp"
    run_tmp.mkdir(parents=True, exist_ok=True)
    (run_tmp / "junk.txt").write_text("leaked junk\n")

    # Also seed a file ABOVE data_dir — testing the project-root escape
    # class (I-8.8). If someone ever removes `git -C <vault_dir>` the
    # test would catch an escape to the monkeypatched working dir.
    above_dir = tmp_path / "rogue_project"
    above_dir.mkdir()
    (above_dir / "secret.md").write_text("DO NOT LEAK\n")

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 0, f"expected OK (0), got {rc}"
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True

    # Inspect the bare repo's HEAD tree — this is what the backup
    # actually holds.
    tracked = _tree_files_at_head(env.bare_repo, env.settings.vault_branch)

    # Positive assertion: `note.md` AND `.gitignore` (bootstrap seeded)
    # should be present.
    assert "note.md" in tracked, f"note.md missing from tracked set: {tracked!r}"
    assert ".gitignore" in tracked, (
        f".gitignore missing from tracked set: {tracked!r}"
    )

    # Negative assertions: none of the decoy paths can appear, in any
    # form (neither as-is, nor as a `../`-prefixed escape, nor as the
    # basename only).
    for forbidden in (
        "leak.png",
        "assistant.db",
        "junk.txt",
        "secret.md",
        # Full relative path attempts (would be the shape if a `git add
        # -A` somehow ran from data_dir instead of vault_dir):
        "media/outbox/leak.png",
        "run/tmp/junk.txt",
        "../media/outbox/leak.png",
        "../assistant.db",
    ):
        assert forbidden not in tracked, (
            f"I-8.1 VIOLATED: {forbidden!r} leaked into commit. tracked={tracked!r}"
        )

    # SF-D7 assertion: `.tmp/scratch.md` is inside vault_dir but must be
    # excluded by `.gitignore` (`.tmp/`).
    assert ".tmp/scratch.md" not in tracked, (
        f"SF-D7 VIOLATED: .tmp/scratch.md leaked. tracked={tracked!r}"
    )
