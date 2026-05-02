"""Phase 9 §2.11 + HIGH-1 + AC#23 — pandoc subprocess env whitelist.

The ``_pandoc_env`` helper builds a fresh dict with EXACTLY the four
keys pandoc needs (PATH, LANG, LC_ALL, HOME — fix-pack F10 added
``LC_ALL`` for Cyrillic locale defense-in-depth). Test asserts the
helper does NOT leak Telegram bot tokens, GH_TOKEN, ANTHROPIC_API_KEY,
or any other secret-bearing env var.
"""

from __future__ import annotations

import os

import pytest

from assistant.render_doc._subprocess import _pandoc_env


def test_pandoc_env_only_whitelisted_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env keys ⊆ {PATH, LANG, LC_ALL, HOME}."""
    # Plant secrets in os.environ.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "sensitive123")
    monkeypatch.setenv("GH_TOKEN", "ghs_xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", "oauth-xxx")
    env = _pandoc_env()
    # Fix-pack F10: ``LC_ALL`` joined the whitelist for Cyrillic locale
    # defense-in-depth (DH-4).
    assert set(env.keys()) == {"PATH", "LANG", "LC_ALL", "HOME"}
    # Sanity: secrets we planted MUST not appear in any value.
    for v in env.values():
        assert "sensitive123" not in v
        assert "ghs_xxx" not in v
        assert "sk-ant-" not in v


def test_pandoc_env_path_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/custom/bin:/usr/bin")
    env = _pandoc_env()
    assert env["PATH"] == "/custom/bin:/usr/bin"


def test_pandoc_env_lang_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANG", raising=False)
    env = _pandoc_env()
    assert env["LANG"] == "C.UTF-8"


def test_pandoc_env_lc_all_default_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix-pack F10 — LC_ALL falls back to ``C.UTF-8`` so pandoc's
    citation sort + title-case ops never see a host's stray ``C``
    locale (Cyrillic regression risk)."""
    monkeypatch.delenv("LC_ALL", raising=False)
    env = _pandoc_env()
    assert env["LC_ALL"] == "C.UTF-8"


def test_pandoc_env_lc_all_inherited_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LC_ALL", "ru_RU.UTF-8")
    env = _pandoc_env()
    assert env["LC_ALL"] == "ru_RU.UTF-8"


def test_pandoc_env_returns_fresh_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating returned dict MUST NOT mutate ``os.environ``."""
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _pandoc_env()
    env["PATH"] = "/tmp/evil"
    assert os.environ["PATH"] == "/usr/bin"
