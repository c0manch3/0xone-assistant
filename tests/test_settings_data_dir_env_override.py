"""Regression test for data_dir ASSISTANT_DATA_DIR env override.

Bug: ``Settings(BaseSettings)`` does not declare ``env_prefix``, so the
unprefixed ``data_dir`` field does NOT read ``ASSISTANT_DATA_DIR`` via
pydantic-settings. The fallback path (``_default_data_dir``) therefore
had to honor the env var explicitly, otherwise the daemon resolved
``~/.local/share/0xone-assistant`` even when the operator set
``ASSISTANT_DATA_DIR=/app/data`` (bind-mounted into the container).

The inline ``_data_dir()`` helper used by every ``tools/*/main.py``
already honors ``ASSISTANT_DATA_DIR`` first; this regression test
locks in the matching behavior for the daemon's ``_default_data_dir``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_assistant_data_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ASSISTANT_DATA_DIR is set, _default_data_dir returns it."""
    from assistant.config import _default_data_dir

    target = tmp_path / "custom-data"
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(target))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    result = _default_data_dir()
    assert result == target


def test_assistant_data_dir_falls_back_to_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without ASSISTANT_DATA_DIR, XDG_DATA_HOME is used."""
    from assistant.config import _default_data_dir

    monkeypatch.delenv("ASSISTANT_DATA_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    result = _default_data_dir()
    assert result == xdg / "0xone-assistant"


def test_assistant_data_dir_final_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any env var, ~/.local/share/0xone-assistant is returned."""
    from assistant.config import _default_data_dir

    monkeypatch.delenv("ASSISTANT_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    result = _default_data_dir()
    assert result == Path.home() / ".local" / "share" / "0xone-assistant"


def test_settings_data_dir_honors_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: Settings() picks up ASSISTANT_DATA_DIR via the factory."""
    target = tmp_path / "custom-data"
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(target))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("OWNER_CHAT_ID", "1")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    from assistant.config import Settings

    s = Settings()  # type: ignore[call-arg]
    assert s.data_dir == target
