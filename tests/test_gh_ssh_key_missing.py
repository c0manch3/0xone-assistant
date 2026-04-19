"""SSH key validation path of ``vault-commit-push`` (phase-8 C4 / SF-F3).

Covers:

- ``GH_VAULT_SSH_KEY_PATH`` pointing at a nonexistent file → exit 10
  (``SSH_KEY_ERROR``) with an actionable JSON payload including the
  path and ``detail`` explaining which check failed.
- ``GH_VAULT_SSH_KEY_PATH`` pointing at a directory (not a file) →
  exit 10 similarly.
- An existing file with permissive mode (world-readable) → exit 0 on
  the dry-run path AFTER emitting a stderr warning. Permissive mode is
  not fatal — matches openssh client semantics (warns but accepts).

``--dry-run`` is sufficient to exercise the ssh-key path because the
check happens BEFORE the dry-run bypass — we want the key probe to run
whether or not we actually push.

B-A2: CLI works without ``TELEGRAM_BOT_TOKEN`` / ``OWNER_CHAT_ID`` in
env. We ``delenv`` to prove the direct ``GitHubSettings()`` path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.gh import main as gh_main


def _setup_allowlist_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Baseline env for a GH settings that would otherwise validate.

    We target a `git@github.com:...` URL so the SSH branch of
    ``_cmd_vault_commit_push`` is exercised (``file://`` URLs skip the
    ssh probe entirely).
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OWNER_CHAT_ID", raising=False)
    # Isolate the data dir so the test never pollutes the owner's vault.
    monkeypatch.setenv("ASSISTANT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "data" / "vault"))
    monkeypatch.setenv("GH_ALLOWED_REPOS", "owner/vault")
    monkeypatch.setenv("GH_VAULT_REMOTE_URL", "git@github.com:owner/vault.git")


def test_ssh_key_nonexistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pointing ``GH_VAULT_SSH_KEY_PATH`` at a nonexistent path → exit 10."""
    _setup_allowlist_env(monkeypatch, tmp_path)
    missing = tmp_path / "does" / "not" / "exist" / "id_vault"
    monkeypatch.setenv("GH_VAULT_SSH_KEY_PATH", str(missing))

    rc = gh_main.main(["vault-commit-push"])

    assert rc == 10, f"expected SSH_KEY_ERROR (10), got {rc}"
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["error"] == "ssh_key_error"
    assert payload["path"] == str(missing)
    # Actionable detail: the operator can see which check failed.
    assert "not a file" in payload["detail"]


def test_ssh_key_is_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory at the key path is also rejected (``is_file()`` returns False)."""
    _setup_allowlist_env(monkeypatch, tmp_path)
    dir_as_key = tmp_path / "keys"
    dir_as_key.mkdir()
    monkeypatch.setenv("GH_VAULT_SSH_KEY_PATH", str(dir_as_key))

    rc = gh_main.main(["vault-commit-push"])
    assert rc == 10
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["error"] == "ssh_key_error"
    assert "not a file" in payload["detail"]


def test_ssh_key_permissive_mode_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """World-readable mode emits a warning but doesn't block ``--dry-run``.

    We drive the `--dry-run` branch so there's no actual push — the key
    readability check still runs and must accept the mode-permissive
    key while logging a stderr warning.
    """
    _setup_allowlist_env(monkeypatch, tmp_path)
    key = tmp_path / "id_vault_permissive"
    key.write_text("fake-key-material\n")
    key.chmod(0o644)  # world-readable — the paranoid openssh default warns
    monkeypatch.setenv("GH_VAULT_SSH_KEY_PATH", str(key))

    rc = gh_main.main(["vault-commit-push", "--dry-run"])

    # dry-run on a fresh vault → `would_bootstrap=True` payload, rc 0.
    assert rc == 0
    captured = capsys.readouterr()
    assert "permissive_mode" in captured.err, (
        f"expected stderr warning, got: {captured.err!r}"
    )
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True
    assert payload["dry_run"] is True
