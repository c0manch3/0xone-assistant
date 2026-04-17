"""Phase 4: MemorySettings defaults + env override surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import MemorySettings, Settings


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
    )


def test_defaults_under_data_dir(tmp_path: Path) -> None:
    s = _make_settings(tmp_path)
    assert s.vault_dir == tmp_path / "data" / "vault"
    assert s.memory_index_path == tmp_path / "data" / "memory-index.db"
    assert s.memory.fts_tokenizer == "porter unicode61 remove_diacritics 2"
    assert s.memory.history_tool_result_truncate_chars == 2000
    assert s.memory.max_body_bytes == 1_048_576


def test_env_override_vault_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "custom-vault"
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(custom))
    # Construct MemorySettings directly so the env_file defaults don't
    # clobber the value under user config.
    m = MemorySettings()
    assert m.vault_dir == custom


def test_env_override_truncate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_HISTORY_TOOL_RESULT_TRUNCATE_CHARS", "512")
    m = MemorySettings()
    assert m.history_tool_result_truncate_chars == 512


def test_env_override_max_body_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_MAX_BODY_BYTES", "2048")
    m = MemorySettings()
    assert m.max_body_bytes == 2048


def test_settings_with_explicit_memory_vault_dir(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-vault"
    s = Settings(
        telegram_bot_token="test",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        memory=MemorySettings(vault_dir=explicit),
    )
    assert s.vault_dir == explicit
