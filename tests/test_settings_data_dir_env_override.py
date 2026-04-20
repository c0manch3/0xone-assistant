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


def test_assistant_data_dir_expands_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SF-1 align: ``~/...`` in ASSISTANT_DATA_DIR resolves to ``$HOME/...``.

    Matches the behavior of the inline ``_data_dir()`` helper in
    tools/*/main.py so operators who set the env var in .env the same way
    they would at a shell prompt do not end up with a literal ``~``
    directory under the container root.
    """
    from assistant.config import _default_data_dir

    monkeypatch.setenv("ASSISTANT_DATA_DIR", "~/mydata")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    result = _default_data_dir()
    assert result == Path.home() / "mydata"


def test_assistant_data_dir_empty_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SF-1 align: empty / whitespace-only ASSISTANT_DATA_DIR acts as unset.

    A value like ``ASSISTANT_DATA_DIR=`` (an operator left the RHS blank in
    .env) previously returned ``Path("")`` — a subtly broken path that
    later made ``Path / "assistant.db"`` produce ``/assistant.db``. The
    ``.strip()`` guard now falls through to the XDG branch so this class
    of misconfiguration is silently harmless.
    """
    from assistant.config import _default_data_dir

    monkeypatch.setenv("ASSISTANT_DATA_DIR", "")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    result = _default_data_dir()
    assert result == xdg / "0xone-assistant"


def test_assistant_data_dir_whitespace_only_falls_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Whitespace-only override also falls through (SF-1 ``.strip()``)."""
    from assistant.config import _default_data_dir

    monkeypatch.setenv("ASSISTANT_DATA_DIR", "   \t ")
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))

    result = _default_data_dir()
    assert result == xdg / "0xone-assistant"
