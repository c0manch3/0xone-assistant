"""Fix 8 / H5-W3 — ``Daemon.start()`` wraps ``configure_memory`` in a
helpful exit.

A read-only vault_dir, a quota-full disk, or a stuck index lock would
otherwise crash the daemon with a raw ``OSError`` traceback — the
owner would see a generic stderr dump in Telegram-side silence. The
wrapper converts these to a structured log + ``sys.exit(4)`` with a
clear hint pointing at the ``MEMORY_*`` env vars.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant import main as main_mod
from assistant.config import Settings
from assistant.main import Daemon


@pytest.mark.asyncio
async def test_daemon_configure_memory_oserror_exits_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    settings = Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        data_dir=data_dir,
    )

    daemon = Daemon(settings)

    async def _ok_preflight() -> None:
        return None

    # Skip the claude CLI preflight — orthogonal to what this test checks.
    monkeypatch.setattr(daemon, "_preflight_claude_auth", _ok_preflight)
    # Skip the project-root / skills symlink guard — the test doesn't
    # need a real repo layout.
    monkeypatch.setattr(
        main_mod,
        "assert_no_custom_claude_settings",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        main_mod, "ensure_skills_symlink", lambda *args, **kwargs: None
    )

    # Stub installer config — orthogonal.
    monkeypatch.setattr(
        main_mod._installer_mod,
        "configure_installer",
        lambda **kwargs: None,
    )

    def _boom(**kwargs: object) -> None:
        raise OSError("simulated read-only vault")

    monkeypatch.setattr(main_mod._memory_mod, "configure_memory", _boom)

    with pytest.raises(SystemExit) as excinfo:
        await daemon.start()
    assert excinfo.value.code == 4
