"""Phase 7 fix-pack D3 — reject `data_dir` under a cloud-sync folder.

The media sweeper ``unlink``s expired files under
``<data_dir>/media/{inbox,outbox}`` every tick. If ``data_dir``
resolves under iCloud / Dropbox / Yandex / OneDrive / GDrive, the
sweep races the sync agent: the sync replicates the unlink, and the
file vanishes from every device. Worse, once a backup restore brings
it back, the sync overwrites the restore with the most-recent
(empty) state.

The guard runs inside ``Daemon.start`` before ``ensure_media_dirs``
and either:

* resolves the real path of ``data_dir``, matches against the
  curated list of known sync roots, and calls
  ``sys.exit(DATA_DIR_SYNC_GUARD_FAIL_EXIT)`` (4); OR
* if a ``<data_dir>/.nosync`` sentinel exists, logs a warning and
  proceeds (opt-out for advanced users + CI).

Tests here target the pure helper function rather than the full
``Daemon.start`` flow so the assertion is focused on the match /
bypass logic itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from assistant.main import (
    DATA_DIR_SYNC_GUARD_FAIL_EXIT,
    _check_data_dir_not_in_cloud_sync,
)


class _StubLog:
    """Minimal structlog-shaped logger that swallows every call.

    The real ``structlog.stdlib.BoundLogger`` accepts keyword
    arguments on every level method (``error(msg, **kwargs)``);
    stdlib ``logging.Logger`` does NOT, which would produce a
    ``TypeError`` on the helper's calls. A tiny duck-type stand-in
    keeps the test focused on the guard's control-flow.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("debug", event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("info", event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("warning", event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.calls.append(("error", event, kwargs))


def _stub_log() -> _StubLog:
    return _StubLog()


def test_non_sync_path_passes(tmp_path: Path) -> None:
    """Control: an ordinary ``tmp_path`` (macOS TMPDIR) is not under
    any sync root, so the guard is a silent no-op."""
    # Must not raise, must not sys.exit.
    _check_data_dir_not_in_cloud_sync(tmp_path, _stub_log())  # type: ignore[arg-type]


def test_sync_root_without_sentinel_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``data_dir`` whose resolved path lives under a patched sync
    root triggers ``sys.exit(DATA_DIR_SYNC_GUARD_FAIL_EXIT)``.

    We patch the tuple of known sync roots to include our ``tmp_path``
    so the test is filesystem-independent: it does NOT require an
    actual iCloud / Dropbox install on the CI machine.
    """
    # Create a fake "cloud" root and a data_dir under it.
    fake_cloud = tmp_path / "fake_icloud"
    fake_cloud.mkdir()
    data_dir = fake_cloud / "0xone-assistant"
    data_dir.mkdir()

    monkeypatch.setattr(
        "assistant.main._CLOUD_SYNC_ROOTS",
        ((str(fake_cloud), "fake iCloud"),),
    )

    with pytest.raises(SystemExit) as exc_info:
        _check_data_dir_not_in_cloud_sync(data_dir, _stub_log())  # type: ignore[arg-type]

    assert exc_info.value.code == DATA_DIR_SYNC_GUARD_FAIL_EXIT


def test_sync_root_with_sentinel_allows_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.nosync`` sentinel inside ``data_dir`` lets the guard
    proceed even when the parent is a known sync root — opt-out
    escape hatch for advanced users + CI."""
    fake_cloud = tmp_path / "fake_dropbox"
    fake_cloud.mkdir()
    data_dir = fake_cloud / "0xone-assistant"
    data_dir.mkdir()
    sentinel = data_dir / ".nosync"
    sentinel.write_text("opt-out", encoding="utf-8")

    monkeypatch.setattr(
        "assistant.main._CLOUD_SYNC_ROOTS",
        ((str(fake_cloud), "fake Dropbox"),),
    )

    # Must not raise / exit.
    _check_data_dir_not_in_cloud_sync(data_dir, _stub_log())  # type: ignore[arg-type]


def test_symlink_into_sync_root_still_trips_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink-based detour into a sync root must still trip the
    guard — the check uses ``resolve()`` which follows symlinks,
    mirroring what the media sweeper's unlink would experience."""
    fake_cloud = tmp_path / "fake_yandex"
    fake_cloud.mkdir()
    real_data = fake_cloud / "data"
    real_data.mkdir()

    # The "nominal" data_dir is a symlink that lives OUTSIDE the
    # sync root but points INTO it. Daemon.start resolves data_dir
    # via Path().resolve() → sync root match must fire.
    outside = tmp_path / "outside"
    outside.mkdir()
    link = outside / "data_link"
    link.symlink_to(real_data)

    monkeypatch.setattr(
        "assistant.main._CLOUD_SYNC_ROOTS",
        ((str(fake_cloud), "fake Yandex"),),
    )

    with pytest.raises(SystemExit) as exc_info:
        _check_data_dir_not_in_cloud_sync(link, _stub_log())  # type: ignore[arg-type]
    assert exc_info.value.code == DATA_DIR_SYNC_GUARD_FAIL_EXIT


def test_sync_root_matches_by_configured_label_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: data_dir OUTSIDE every configured sync root passes
    even when the sync root list is non-empty — a spurious match
    based on the fact that the list exists would be a regression."""
    fake_cloud = tmp_path / "fake_onedrive"
    fake_cloud.mkdir()
    # data_dir under `tmp_path` but NOT under `fake_cloud`.
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.setattr(
        "assistant.main._CLOUD_SYNC_ROOTS",
        ((str(fake_cloud), "fake OneDrive"),),
    )

    _check_data_dir_not_in_cloud_sync(data_dir, _stub_log())  # type: ignore[arg-type]


def test_missing_sync_root_on_fs_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured sync root that does not exist on the current
    filesystem is silently skipped (the user simply does not have
    that sync provider installed). The guard must not false-positive
    against unrelated paths when the root is absent."""
    monkeypatch.setattr(
        "assistant.main._CLOUD_SYNC_ROOTS",
        (("~/ThisDirectoryDefinitelyDoesNotExist", "nonexistent"),),
    )
    data_dir = tmp_path / "normal"
    data_dir.mkdir()
    _check_data_dir_not_in_cloud_sync(data_dir, _stub_log())  # type: ignore[arg-type]
