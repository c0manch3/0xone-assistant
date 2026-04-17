"""Phase 4: Daemon.start creates the vault with mode 0o700 and warns if loose."""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from assistant.config import ClaudeSettings, Settings
from assistant.main import Daemon


@pytest.fixture(autouse=True)
def _structlog_capture_ready() -> None:
    structlog.reset_defaults()


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path / "project",
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
    )


def test_ensure_vault_creates_dir_with_tight_mode(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    d = Daemon(settings)
    d._ensure_vault()
    assert settings.vault_dir.is_dir()
    assert (settings.vault_dir / ".tmp").is_dir()
    mode = settings.vault_dir.stat().st_mode & 0o777
    assert not mode & 0o077, f"expected tight mode, got {oct(mode)}"


def test_ensure_vault_warns_on_loose_perms(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    vault = settings.vault_dir
    vault.mkdir(parents=True, mode=0o755)
    vault.chmod(0o755)

    d = Daemon(settings)
    with capture_logs() as cap:
        d._ensure_vault()
    # Mode unchanged — operator intent preserved.
    assert vault.stat().st_mode & 0o777 == 0o755
    assert any(e["event"] == "vault_dir_permissions_too_open" for e in cap), cap


def test_ensure_vault_idempotent(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    d = Daemon(settings)
    d._ensure_vault()
    d._ensure_vault()  # second call must not fail
    assert settings.vault_dir.is_dir()
