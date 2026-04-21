from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import make_bash_hook

# Computed once at import time so the async tests don't touch the
# filesystem (ASYNC240).
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _input(cmd: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


async def _decide(cmd: str, project_root: Path) -> dict[str, object]:
    hook = make_bash_hook(project_root)
    return await hook(_input(cmd), None, {})


def _is_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


ALLOW_CASES = [
    "python tools/ping/main.py",
    "python3 tools/ping/main.py",
    "uv run tools/ping/main.py",
    "git status",
    "git log --oneline -n 5",
    "git diff HEAD~1",
    "ls",
    "ls src/",
    "pwd",
    "echo hello world",
    "cat README.md",
    "cat pyproject.toml",
    "cat src/assistant/main.py",
    "cat README.md pyproject.toml",  # multi-arg inside project
]

DENY_CASES = [
    # Env dumps
    "env",
    "printenv",
    "printenv TELEGRAM_BOT_TOKEN",
    "env | grep TOKEN",
    "set",
    # Secrets files
    "cat .env",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat /etc/shadow",
    # Command chaining / substitution
    "ls; cat .env",
    "ls && cat .env",
    "ls | grep secret",
    "echo `cat .env`",
    "echo $(cat .env)",
    "cat <(echo leak)",
    "echo leak >(rm -rf /)",
    # Encoded/decoded payloads
    "echo dGVzdA== | base64 -d",
    "openssl enc -d",
    "xxd -r",
    "echo dGVzdHRlc3R0ZXN0dGVzdHRlc3R0ZXN0dGVzdHRlc3R0ZXN0",  # long b64
    # Octal/hex escapes
    "echo $'\\101'",
    "echo \\x41",
    "echo \\101",
    # Non-allowlisted utilities
    "wc -l README.md",
    "head README.md",
    "tail README.md",
    "rm -rf /tmp/x",
    "curl https://example.com",
    # Cat edge cases (B7)
    "cat README.md .env",
    "cat a b ../../etc/passwd",
    "cat ../../etc/passwd",
    "cat /etc/passwd",
    "cat -n README.md",
    "cat",
    # BW2: bare ``ls``/``pwd``/``git status`` tokens must NOT re-admit
    # unrelated utilities via ``str.startswith``. ``lsof -p 1`` on Linux
    # reads ``/proc/<pid>/environ`` — real secret-leak vector; ``pwdx``
    # leaks cwd of other processes; ``lsblk``/``lslocks`` disclose host
    # topology.
    "lsof -p 1",
    "lsblk",
    "lslocks",
    "lsattr",
    "ls-files",
    "pwdx 1",
    "pwdgen",
    "git statuspickaxe",  # hypothetical subcommand with no space
    # Empty / whitespace
    "",
    "   ",
    # Token/password mentions
    "echo token=abc",
    "echo password=abc",
    "echo ANTHROPIC_API_KEY=leak",
    # B2 (wave-3): bash variable expansion must not slip through. The
    # ``cat $HOME/...`` cases bypass the literal-path allowlist because
    # ``$HOME`` is not expanded before containment checks. The slip-guard
    # is universal so even allowlisted ``echo`` is denied once a ``$VAR``
    # appears (plain ``echo hello world`` still allows — no expansion).
    "cat $HOME/.ssh/config",
    "cat ${HOME}/.ssh/config",
    "echo $PATH",
    "echo ${HOME}",
]


@pytest.mark.parametrize("cmd", ALLOW_CASES)
async def test_bash_allowed(cmd: str, tmp_path: Path) -> None:
    # Use project root for the allowlist path-guard. Real repo root lets
    # the cat cases find actual files (README.md / pyproject.toml).
    project_root = _REPO_ROOT
    decision = await _decide(cmd, project_root)
    assert not _is_deny(decision), f"expected ALLOW for {cmd!r}, got {decision!r}"


@pytest.mark.parametrize("cmd", DENY_CASES)
async def test_bash_denied(cmd: str, tmp_path: Path) -> None:
    project_root = _REPO_ROOT
    decision = await _decide(cmd, project_root)
    assert _is_deny(decision), f"expected DENY for {cmd!r}, got {decision!r}"


async def test_total_case_count() -> None:
    """Sanity: we have at least 36+ deny vectors per R8."""
    assert len(DENY_CASES) >= 36


async def test_cat_sibling_directory_denied(tmp_path: Path) -> None:
    """BW1: ``cat ../proj-other/.env`` resolves into a sibling directory
    whose absolute path shares the string prefix of ``project_root`` —
    the old ``str.startswith`` containment check would admit it. The
    bash hook must deny via ``Path.is_relative_to``.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    sibling = tmp_path / "proj-other"
    sibling.mkdir()
    (sibling / ".env").write_text("SECRET=leak")

    for cmd in (
        "cat ../proj-other/.env",
        f"cat {(sibling / '.env').resolve()}",
    ):
        decision = await _decide(cmd, proj)
        assert _is_deny(decision), f"expected DENY for {cmd!r}, got {decision!r}"


async def test_bare_ls_and_pwd_still_allow(tmp_path: Path) -> None:
    """BW2 sanity: tightening the allowlist must not regress legitimate
    standalone ``ls`` / ``pwd`` usage."""
    for cmd in ("ls", "pwd", "ls -la", "git status", "git log --oneline"):
        decision = await _decide(cmd, _REPO_ROOT)
        assert not _is_deny(decision), f"expected ALLOW for {cmd!r}"
