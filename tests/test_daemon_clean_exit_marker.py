"""H-2 / M2.6 / M2.7: clean-exit marker round-trip.

Covers:
  - classify_boot uses mtime (not just existence) — an ancient marker
    falls through to ``suspend-or-crash``.
  - unlink_clean_exit_marker is idempotent + survives missing file.
  - write_clean_exit_marker produces JSON with ``ts`` + ``pid``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

from assistant.scheduler.store import (
    SchedulerStore,
    unlink_clean_exit_marker,
    write_clean_exit_marker,
)
from assistant.state.db import apply_schema, connect


async def _store(tmp_path: Path) -> SchedulerStore:
    db = tmp_path / "sched.db"
    conn = await connect(db)
    await apply_schema(conn)
    return SchedulerStore(conn)


async def test_first_boot_no_marker(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    marker = tmp_path / ".last_clean_exit"
    got = await st.classify_boot(marker_path=marker, max_age_s=120)
    assert got == "first-boot"


async def test_clean_deploy_recent_marker(tmp_path: Path) -> None:
    st = await _store(tmp_path)
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    got = await st.classify_boot(marker_path=marker, max_age_s=120)
    assert got == "clean-deploy"


async def test_suspend_or_crash_old_marker(tmp_path: Path) -> None:
    """M2.6: an ancient marker (old mtime) must classify as
    suspend-or-crash, not clean-deploy."""
    st = await _store(tmp_path)
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    # Force mtime 10 minutes into the past.
    past = time.time() - 600
    os.utime(marker, (past, past))
    got = await st.classify_boot(marker_path=marker, max_age_s=120)
    assert got == "suspend-or-crash"


async def test_m2_7_unlink_after_classification(tmp_path: Path) -> None:
    """M2.7: after classify_boot we unlink the marker, so a restart 10
    minutes later is still classified correctly (suspend-or-crash)."""
    st = await _store(tmp_path)
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    await st.classify_boot(marker_path=marker, max_age_s=120)
    unlink_clean_exit_marker(marker)
    assert not marker.exists()
    # Second classification after unlink → first-boot.
    again = await st.classify_boot(marker_path=marker, max_age_s=120)
    assert again == "first-boot"


def test_write_marker_payload_shape(tmp_path: Path) -> None:
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert "ts" in body and "pid" in body
    # ts parses as ISO.
    dt.datetime.fromisoformat(body["ts"].replace("Z", "+00:00"))


def test_unlink_missing_file_noop(tmp_path: Path) -> None:
    unlink_clean_exit_marker(tmp_path / "nope")  # must not raise
