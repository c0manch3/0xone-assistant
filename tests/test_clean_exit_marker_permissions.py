"""Fix 15 / DevOps §2: ``write_clean_exit_marker`` chmods the marker
to 0o600 after the atomic tmp+rename write.

The marker carries no secret (``ts`` + ``pid``) but ``<data_dir>``
convention is owner-only — matching the audit log + vault files is
less surprising for an operator reviewing the directory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from assistant.scheduler.store import write_clean_exit_marker


def test_marker_mode_is_0o600(tmp_path: Path) -> None:
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    assert marker.is_file()
    mode = stat.S_IMODE(os.stat(marker).st_mode)
    assert mode == 0o600, f"expected 0o600; got {oct(mode)}"


def test_marker_mode_stays_0o600_on_overwrite(tmp_path: Path) -> None:
    """If the daemon restarts cleanly twice, the second write must
    also end at 0o600 (atomic rename uses the tmp file's default mode
    until we chmod).
    """
    marker = tmp_path / ".last_clean_exit"
    write_clean_exit_marker(marker)
    # Deliberately wrong mode to prove the next write restores it.
    os.chmod(marker, 0o644)
    write_clean_exit_marker(marker)
    mode = stat.S_IMODE(os.stat(marker).st_mode)
    assert mode == 0o600
