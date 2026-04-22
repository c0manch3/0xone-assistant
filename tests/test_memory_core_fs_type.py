"""Filesystem-type detection — C2.1 space-safe parsing + unsafe warnings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog

from assistant.tools_sdk import _memory_core as core


def test_memory_fs_type_check_space_in_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2.1: mount-point with a space (``/Volumes/Google Chrome``) must
    parse correctly.
    """
    fake_mount = (
        "/dev/disk1s5 on / (apfs, local, read-only)\n"
        "/dev/disk4s2 on /Volumes/Google Chrome (hfs, local, nobrowse)\n"
    )
    captured = {}

    def fake_run(*args: Any, **kwargs: Any) -> Any:
        class R:
            stdout = fake_mount

        captured["called"] = True
        return R()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    class FakeUname:
        sysname = "Darwin"

    monkeypatch.setattr(core.os, "uname", lambda: FakeUname())
    probe = Path("/Volumes/Google Chrome/some/nested/vault")
    # Cannot actually .resolve() a non-existent path safely; monkeypatch
    # the method to return self.
    monkeypatch.setattr(
        core.Path, "resolve", lambda self, strict=False: self
    )
    fs = core._detect_fs_type(probe)
    assert fs == "hfs"
    assert captured["called"]


def test_memory_fs_type_check_icloud_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path-prefix warning fires even if the detected FS is apfs."""
    # Force a path under ~/Library/Mobile Documents
    probe = Path("~/Library/Mobile Documents/com~apple~CloudDocs/vault").expanduser()
    # stub _detect_fs_type to return apfs so the "unsafe_fs" warning
    # would NOT fire; we only want the path-prefix warning.
    monkeypatch.setattr(core, "_detect_fs_type", lambda p: "apfs")
    with structlog.testing.capture_logs() as records:
        core._fs_type_check(probe)
    events = [rec.get("event") for rec in records]
    assert "memory_vault_cloud_sync_path" in events


def test_memory_fs_type_check_unsafe_smbfs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unsafe FS triggers a WARNING."""
    monkeypatch.setattr(core, "_detect_fs_type", lambda p: "smbfs")
    with structlog.testing.capture_logs() as records:
        core._fs_type_check(tmp_path)
    events = [rec.get("event") for rec in records]
    assert "memory_vault_unsafe_fs" in events


def test_memory_fs_type_check_apfs_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Allowed FS → no warning emitted."""
    monkeypatch.setattr(core, "_detect_fs_type", lambda p: "apfs")
    with structlog.testing.capture_logs() as records:
        core._fs_type_check(tmp_path)
    # No warning-level record.
    warnings = [r for r in records if r.get("log_level") == "warning"]
    assert warnings == []


def test_memory_fs_type_regex_matches_darwin_line() -> None:
    """Regex covers the common Darwin mount formats."""
    line = "/dev/disk1s5 on / (apfs, local, journaled)"
    m = core._DARWIN_MOUNT_RE.match(line)
    assert m is not None
    assert m.group(1) == "/"
    assert m.group(2).lower() == "apfs"
    # Spaces
    line2 = "/dev/disk4s2 on /Volumes/Google Chrome (hfs, local, ...)"
    m2 = core._DARWIN_MOUNT_RE.match(line2)
    assert m2 is not None
    assert m2.group(1) == "/Volumes/Google Chrome"
    assert m2.group(2).lower() == "hfs"
