"""Phase 8 §2.3 — async wrappers around the system ``git`` binary.

Every call uses ``asyncio.create_subprocess_exec`` (argv form, no
shell). Authentication for ``git push`` is via ``GIT_SSH_COMMAND``
passed through the per-subprocess ``env=`` parameter — NEVER via
``os.environ.update`` or any other mutation of the daemon's process
env (H-3 closure, AC#16).

The ``timeout_s`` budget on each call is enforced via
``asyncio.wait_for``. On timeout the process is killed and reaped
before the timeout error propagates to the caller — leaving a zombie
subprocess would burn fds on a tightly-packed VPS.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from assistant.logger import get_logger

log = get_logger("vault_sync.git_ops")


class GitOpError(RuntimeError):
    """Raised when a git subprocess returns non-zero or times out.

    Carries the truncated stderr for forensic logging. Supervisors
    correlate via the ``operation`` attribute.
    """

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        returncode: int | None = None,
    ) -> None:
        super().__init__(f"{operation}: {message}")
        self.operation = operation
        self.message = message
        self.returncode = returncode


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``git <args>`` and return ``(returncode, stdout, stderr)``.

    On timeout, the process is killed + reaped; ``GitOpError``
    propagates with ``returncode=None``. Stderr is decoded best-effort
    (``replace`` errors so no UnicodeDecodeError ever escapes).
    """
    operation = " ".join(["git", *args[:2]])
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except TimeoutError as err:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise GitOpError(
            operation,
            f"timed out after {timeout_s}s",
        ) from err
    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    return (proc.returncode or 0, stdout, stderr)


async def git_status_porcelain(
    vault_dir: Path,
    *,
    timeout_s: float,
) -> str:
    """Return the ``git status --porcelain`` output (empty string =
    clean working tree)."""
    rc, out, err = await _run_git(
        ["status", "--porcelain"],
        cwd=vault_dir,
        timeout_s=timeout_s,
    )
    if rc != 0:
        raise GitOpError(
            "status", err.strip() or f"rc={rc}", returncode=rc
        )
    return out


async def git_diff_cached_names(
    vault_dir: Path,
    *,
    timeout_s: float,
) -> list[str]:
    """Return the list of staged paths via
    ``git diff --cached --name-only``.

    Used by ``_validate_staged_paths`` after ``git add`` and before
    ``git commit`` — the per-file regex check runs in-process on this
    list (W2-H4).
    """
    rc, out, err = await _run_git(
        ["diff", "--cached", "--name-only"],
        cwd=vault_dir,
        timeout_s=timeout_s,
    )
    if rc != 0:
        raise GitOpError(
            "diff-cached", err.strip() or f"rc={rc}", returncode=rc
        )
    return [line for line in out.splitlines() if line]


async def git_add_all(
    vault_dir: Path,
    *,
    timeout_s: float,
) -> None:
    """Run ``git add -A`` from the vault working tree.

    ``-A`` stages new + modified + deleted files. The ``.gitignore``
    in the vault root keeps secret-pattern files (``*.env``,
    ``secrets/``, ...) out of the staging area; ``_validate_staged_paths``
    is the second layer of defence (devil C-2 / H-2).
    """
    rc, _out, err = await _run_git(
        ["add", "-A"],
        cwd=vault_dir,
        timeout_s=timeout_s,
    )
    if rc != 0:
        raise GitOpError(
            "add", err.strip() or f"rc={rc}", returncode=rc
        )


async def git_commit(
    vault_dir: Path,
    *,
    message: str,
    author_name: str,
    author_email: str,
    timeout_s: float,
) -> str:
    """Run ``git commit -m <message>`` with explicit user identity.

    Returns the commit SHA from ``git rev-parse HEAD`` immediately
    after — separate subprocess so a malformed commit object surfaces
    as a clear ``rev-parse`` error rather than a parse failure on the
    commit subprocess output.

    User identity is supplied via ``-c user.name=`` / ``-c user.email=``
    on the command line so the daemon does not depend on any
    pre-existing ``git config`` state in the vault dir (defensive
    against a partially-bootstrapped vault).
    """
    rc, _out, err = await _run_git(
        [
            "-c",
            f"user.name={author_name}",
            "-c",
            f"user.email={author_email}",
            "commit",
            "-m",
            message,
        ],
        cwd=vault_dir,
        timeout_s=timeout_s,
    )
    if rc != 0:
        raise GitOpError(
            "commit", err.strip() or f"rc={rc}", returncode=rc
        )
    rc2, sha_out, err2 = await _run_git(
        ["rev-parse", "HEAD"],
        cwd=vault_dir,
        timeout_s=timeout_s,
    )
    if rc2 != 0:
        raise GitOpError(
            "rev-parse", err2.strip() or f"rc={rc2}", returncode=rc2
        )
    return sha_out.strip()


def build_ssh_command(
    *,
    ssh_key_path: Path,
    known_hosts_path: Path,
) -> str:
    """Return the ``GIT_SSH_COMMAND`` value for the push subprocess.

    ``StrictHostKeyChecking=yes`` (NOT ``accept-new``) — the pinned
    known_hosts file is the authoritative trust anchor (H-4 closure).
    """
    return (
        f"ssh -i {ssh_key_path} "
        "-o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={known_hosts_path}"
    )


async def git_push(
    vault_dir: Path,
    *,
    remote: str,
    branch: str,
    ssh_key_path: Path,
    known_hosts_path: Path,
    timeout_s: float,
) -> None:
    """Run ``git push <remote> <branch>`` with per-subprocess
    ``GIT_SSH_COMMAND``.

    H-3 closure: the env dict is built fresh inside this function and
    passed via the ``env=`` parameter to the subprocess spawner. The
    daemon's process-wide ``os.environ`` is NEVER mutated. AC#16
    verifies via an unrelated subprocess after the push that the env
    var did not leak into the daemon process env.
    """
    git_ssh_command = build_ssh_command(
        ssh_key_path=ssh_key_path,
        known_hosts_path=known_hosts_path,
    )
    env = {**os.environ, "GIT_SSH_COMMAND": git_ssh_command}
    rc, _out, err = await _run_git(
        ["push", remote, branch],
        cwd=vault_dir,
        timeout_s=timeout_s,
        env=env,
    )
    if rc != 0:
        raise GitOpError(
            "push", err.strip() or f"rc={rc}", returncode=rc
        )
