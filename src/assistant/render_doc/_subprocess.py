"""Phase 9 §2.6 + §2.11 + §2.12 (ii) — pandoc subprocess helper.

Centralises the SIGTERM signal recipe + scoped env whitelist used by
both PDF and DOCX renderers. Mirrors phase-8 ``vault_sync.git_ops``
shape (``_run_git``).

Invariants:
  - ``env=`` is always explicit; never ``None`` (which would
    inherit the daemon's full env, breaking HIGH-1).
  - On cancellation OR timeout: terminate (SIGTERM) then kill
    (SIGKILL) after configured grace, then re-raise so the caller's
    @tool body sees the cancel.
  - ``stderr`` captured + decoded best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from assistant.config import RenderDocSettings
from assistant.logger import get_logger

log = get_logger("render_doc.subprocess")


class PandocError(RuntimeError):
    """Raised when pandoc exits non-zero (or times out, or fails to
    start). Carries truncated stderr + a kebab-case ``error_code``
    the renderer maps to the audit ``error`` field."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code
        self.returncode = returncode
        self.stderr = stderr[:512]


def _pandoc_env() -> dict[str, str]:
    """Build the whitelisted env for pandoc subprocess (HIGH-1).

    Returns a fresh dict with EXACTLY the keys pandoc needs.
    NEVER includes TELEGRAM_BOT_TOKEN / GH_TOKEN / ANTHROPIC_* /
    CLAUDE_*.

    Fix-pack F10 (DH-4): explicitly pin ``LANG`` and ``LC_ALL`` to
    ``C.UTF-8`` (defense-in-depth — pandoc 2.17.1.1 is mostly
    locale-tolerant for UTF-8 input but citation sorting + title-case
    operations consult ``LC_COLLATE``). Without ``LC_ALL`` set,
    inheriting a host ``LANG=C`` would silently break Cyrillic
    rendering in certain pandoc filter paths (phase-6c-style
    regression). ``HOME`` falls back to ``/tmp`` only when unset; in
    that fallback case pandoc's ``$HOME/.pandoc/`` user data files
    won't be found — acceptable since the daemon runs with a real
    ``HOME`` in production.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }


async def run_pandoc(
    argv: list[str],
    *,
    timeout_s: float,
    settings: RenderDocSettings,
    cwd: str | None = None,
) -> tuple[int, bytes, bytes]:
    """Run pandoc with SIGTERM grace + scoped env.

    On timeout or cancel: terminate, drain pipes, escalate to kill +
    drain after the configured grace. Re-raises CancelledError so the
    caller's @tool body propagates cancel. Returns
    ``(returncode, stdout, stderr)``.

    Fix-pack F4 (CR-2 deadlock fix): on the post-terminate phase,
    drain stdout/stderr pipes via ``proc.communicate()`` instead of a
    bare ``proc.wait()``. Pandoc emitting >64 KiB of stderr fills the
    OS pipe buffer; without a reader the kernel won't deliver SIGTERM
    until the writer unblocks — which it can't. Replacing ``wait()``
    with ``communicate()`` (which drains both pipes concurrently while
    waiting) closes the deadlock.
    """
    env = _pandoc_env()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except (asyncio.CancelledError, TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        # F4: drain pipes during the SIGTERM grace window — bare
        # ``proc.wait()`` here would leak the >64 KiB-stderr deadlock
        # the grace window is supposed to handle gracefully.
        try:
            await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.pandoc_sigterm_grace_s,
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            try:
                await asyncio.wait_for(
                    proc.communicate(),
                    timeout=settings.pandoc_sigkill_grace_s,
                )
            except TimeoutError:
                log.error(
                    "render_doc_pandoc_kill_failed",
                    pid=proc.pid,
                )
        except asyncio.CancelledError:
            # Outer cancel during the grace drain — last-ditch SIGKILL
            # then let the cancel propagate.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        raise
    # F-CH-2: surface signal-killed processes (negative returncode)
    # rather than coercing to 0 via ``or 0``. Returning rc==0 for a
    # OOM-killed (-9) pandoc would let the renderer interpret the
    # missing output file as success.
    rc = proc.returncode if proc.returncode is not None else -1
    return (rc, stdout_b, stderr_b)
