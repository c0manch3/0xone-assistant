"""Phase 7 / commit 5 — `src/assistant/media/paths.py` contract.

Covers:
  1. Pure path builders (`inbox_dir` / `outbox_dir` / `stage_dir`)
     have NO filesystem side effects — importing them in a daemon
     bootstrap path must not create directories unexpectedly.
  2. `ensure_media_dirs` creates all three with mode 0o700,
     idempotently.
  3. A pre-existing loose-perm directory is tightened to 0o700 by
     the `chmod` follow-up.
"""

from __future__ import annotations

import os
from pathlib import Path

from assistant.media.paths import (
    ensure_media_dirs,
    inbox_dir,
    outbox_dir,
    stage_dir,
)


def test_path_builders_are_pure(tmp_path: Path) -> None:
    # Builders must not touch the FS.
    i = inbox_dir(tmp_path)
    o = outbox_dir(tmp_path)
    s = stage_dir(tmp_path)
    assert i == tmp_path / "media" / "inbox"
    assert o == tmp_path / "media" / "outbox"
    assert s == tmp_path / "run" / "render-stage"
    # Still doesn't exist -- the builders are pure.
    assert not i.exists()
    assert not o.exists()
    assert not s.exists()


async def test_ensure_media_dirs_creates_all_three(tmp_path: Path) -> None:
    await ensure_media_dirs(tmp_path)
    for path in (inbox_dir(tmp_path), outbox_dir(tmp_path), stage_dir(tmp_path)):
        assert path.is_dir()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o700, (
            f"{path} has mode {oct(mode)}, expected 0o700"
        )


async def test_ensure_media_dirs_is_idempotent(tmp_path: Path) -> None:
    await ensure_media_dirs(tmp_path)
    # Touch a file inside to ensure the second call doesn't wipe contents.
    probe = outbox_dir(tmp_path) / "probe.txt"
    probe.write_text("survive", encoding="utf-8")
    await ensure_media_dirs(tmp_path)
    assert probe.read_text(encoding="utf-8") == "survive"


async def test_ensure_media_dirs_tightens_loose_perms(tmp_path: Path) -> None:
    # Pre-create with loose perms; `ensure_media_dirs` must chmod down to 0o700.
    loose = inbox_dir(tmp_path)
    loose.mkdir(parents=True, exist_ok=True)
    os.chmod(loose, 0o755)
    assert loose.stat().st_mode & 0o777 == 0o755
    await ensure_media_dirs(tmp_path)
    assert loose.stat().st_mode & 0o777 == 0o700
