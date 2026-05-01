"""Phase 8 fix-pack F7 — AC#16 GIT_SSH_COMMAND scoped to subprocess only.

Devil w1 H-3 closure: GIT_SSH_COMMAND is passed via the env=
parameter to asyncio.create_subprocess_exec so it lives ONLY in
the subprocess scope. The daemon's process-wide os.environ must
NOT mutate.

This test exercises the scope by mocking
asyncio.create_subprocess_exec to record the env= kwarg, then
asserts the daemon's os.environ is untouched after the call.

Bonus: F8 — paths with embedded spaces are shlex.quote-d.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any

import pytest

from assistant.vault_sync.git_ops import build_ssh_command, git_push


@pytest.fixture
def captured_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch asyncio.create_subprocess_exec to record the env kwarg
    and return a fake-success process."""
    captured: dict[str, Any] = {"env": None, "args": None}

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return 0

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


@pytest.mark.asyncio
async def test_ac16_git_ssh_command_in_subprocess_env_only(
    tmp_path: Path,
    captured_env: dict[str, Any],
) -> None:
    """AC#16 — GIT_SSH_COMMAND lives in subprocess env=, not the
    daemon's process env."""
    key = tmp_path / "vault_deploy"
    key.write_text("dummy")
    kh = tmp_path / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAA")
    vault = tmp_path / "vault"
    vault.mkdir()
    assert "GIT_SSH_COMMAND" not in os.environ
    await git_push(
        vault,
        remote="git@github.com:c0manch3/0xone-vault.git",
        branch="main",
        ssh_key_path=key,
        known_hosts_path=kh,
        timeout_s=5.0,
    )
    sub_env = captured_env["env"]
    assert sub_env is not None
    assert "GIT_SSH_COMMAND" in sub_env
    cmd = sub_env["GIT_SSH_COMMAND"]
    assert "ssh -i" in cmd
    assert "StrictHostKeyChecking=yes" in cmd
    assert "GIT_SSH_COMMAND" not in os.environ


def test_f8_shlex_quote_paths_with_spaces() -> None:
    """F8 — paths with shell metachars are shlex-quoted."""
    key = Path("/some path with spaces/vault deploy")
    kh = Path("/another weird/path; rm -rf /")
    cmd = build_ssh_command(ssh_key_path=key, known_hosts_path=kh)
    expected_key = shlex.quote(str(key))
    expected_kh = shlex.quote(str(kh))
    assert expected_key in cmd
    assert expected_kh in cmd
    # The bare path must NOT appear unquoted.
    assert " /some path with spaces/" not in cmd


def test_f8_simple_paths_no_unnecessary_quoting() -> None:
    """For boring ASCII paths, shlex.quote returns them unchanged."""
    cmd = build_ssh_command(
        ssh_key_path=Path("/home/0xone/.ssh/vault_deploy"),
        known_hosts_path=Path("/home/0xone/.ssh/known_hosts_vault"),
    )
    assert "/home/0xone/.ssh/vault_deploy" in cmd
    assert "/home/0xone/.ssh/known_hosts_vault" in cmd


@pytest.mark.asyncio
async def test_f8_quoted_path_still_authenticates(
    tmp_path: Path,
    captured_env: dict[str, Any],
) -> None:
    """A path with spaces still produces a valid GIT_SSH_COMMAND."""
    weird_dir = tmp_path / "with space"
    weird_dir.mkdir()
    key = weird_dir / "vault_deploy"
    key.write_text("dummy")
    kh = weird_dir / "known_hosts_vault"
    kh.write_text("github.com ssh-ed25519 AAAA")
    vault = tmp_path / "vault"
    vault.mkdir()
    await git_push(
        vault,
        remote="git@github.com:c0manch3/0xone-vault.git",
        branch="main",
        ssh_key_path=key,
        known_hosts_path=kh,
        timeout_s=5.0,
    )
    sub_env = captured_env["env"]
    assert sub_env is not None
    cmd = sub_env["GIT_SSH_COMMAND"]
    # The space-containing path must be properly quoted.
    assert "'" in cmd or '"' in cmd
