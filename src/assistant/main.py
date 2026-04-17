from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from assistant.adapters.telegram import TelegramAdapter
from assistant.bridge.bootstrap import ensure_skills_symlink
from assistant.bridge.claude import ClaudeBridge
from assistant.config import Settings, get_settings
from assistant.handlers.message import ClaudeHandler
from assistant.logger import get_logger, setup_logging
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

AUTH_PREFLIGHT_FAIL_EXIT = 3

# Phase-3 TTLs (Q7).
_TMP_TTL_S = 3600  # 1 hour — aborted fetches
_INSTALLER_CACHE_TTL_S = 7 * 86400  # 7 days — preview -> install window
_BOOTSTRAP_TIMEOUT_S = 120.0
_BOOTSTRAP_NOTIFY_COOLDOWN_S = 7 * 86400  # re-notify if marker older than 7 d
_STOP_DRAIN_TIMEOUT_S = 5.0  # review fix #10


async def _preflight_claude_cli(log: structlog.stdlib.BoundLogger) -> None:
    """Fail-fast if the `claude` CLI is missing.

    `claude --version` is cheap (no model turn, no cost) -- it confirms the
    binary is on $PATH. Auth is *not* checked here because every available
    "real" probe (`claude --print ping`, etc.) costs a model call on every
    daemon restart. Authentication errors surface from the first user message
    via `ClaudeBridgeError` with a clear "auth/login" hint in the structured
    log, which is good enough for a single-user bot.

    Exit 3 signals "user action required" (install CLI).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.error(
            "claude_cli_missing",
            hint="Install Claude Code CLI and run `claude login`.",
        )
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error(
            "claude_cli_hanging",
            hint="`claude --version` did not respond within 10s.",
        )
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    if proc.returncode != 0:
        err = (stderr_bytes or b"").decode("utf-8", "replace")
        log.error("claude_cli_failed", stderr=err[:500], rc=proc.returncode)
        sys.exit(AUTH_PREFLIGHT_FAIL_EXIT)

    version = (stdout_bytes or b"").decode("utf-8", "replace").strip()
    log.info("auth_preflight_ok", claude_version=version[:120])


class Daemon:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._log = get_logger("main")
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None
        # Fire-and-forget task handles. `add_done_callback(self._bg_tasks.discard)`
        # keeps refs alive (CPython GCs floating tasks without warnings) and
        # lets `Daemon.stop()` drain them.
        self._bg_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------

    def _spawn_bg(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _sweep_run_dirs(self) -> None:
        """Remove `run/tmp` entries >1h and `run/installer-cache` entries >7d.

        Fire-and-forget; never fails startup on IOError. Missing directories
        are a no-op (first-run case).
        """
        now = time.time()
        data_dir = self._settings.data_dir
        buckets = (
            (data_dir / "run" / "tmp", _TMP_TTL_S),
            (data_dir / "run" / "installer-cache", _INSTALLER_CACHE_TTL_S),
        )
        removed = 0
        for base, ttl in buckets:
            if not base.is_dir():
                continue
            for entry in base.iterdir():
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age <= ttl:
                    continue
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    self._log.warning(
                        "sweep_failed",
                        entry=str(entry),
                        error=repr(exc),
                    )
        if removed:
            self._log.info("sweep_run_dirs_done", removed=removed)

    async def _bootstrap_skill_creator_bg(self) -> None:
        """Install Anthropic's `skill-creator` from the marketplace on first run.

        Fire-and-forget: `Daemon.start()` creates this task with
        `asyncio.create_task` and returns without awaiting. The internal
        120-second timeout bounds the task itself; the main flow never
        waits on GitHub.

        Failure modes (all log-only; owner notified once via
        `_bootstrap_notify_failure` when a real failure happens — not on
        the predictable `skipped_no_gh` state):

        * skill already present              -> no-op.
        * `gh` not on PATH                   -> `skipped_no_gh` warning.
        * subprocess rc != 0 / timeout / exc -> corresponding warning +
                                                one-shot Telegram notice.
        """
        skill_dir = self._settings.project_root / "skills" / "skill-creator"
        if skill_dir.exists():
            return
        if shutil.which("gh") is None:
            self._log.warning("skill_creator_bootstrap_skipped_no_gh")
            return

        self._log.info("skill_creator_bootstrap_starting")
        installer = self._settings.project_root / "tools" / "skill-installer" / "main.py"
        env = {**os.environ, "ASSISTANT_BOOTSTRAP": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(installer),
                "marketplace",
                "install",
                "skill-creator",
                "--confirm",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            self._log.warning("skill_creator_bootstrap_exception", error=repr(exc))
            await self._bootstrap_notify_failure(rc=-1, reason="exception")
            return

        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=_BOOTSTRAP_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            self._log.warning(
                "skill_creator_bootstrap_timeout",
                timeout_s=_BOOTSTRAP_TIMEOUT_S,
            )
            await self._bootstrap_notify_failure(rc=-2, reason="timeout")
            return

        stderr_bytes = b""
        if proc.stderr is not None:
            try:
                stderr_bytes = await proc.stderr.read()
            except Exception:  # pragma: no cover -- pipe already drained
                stderr_bytes = b""
        if rc != 0:
            self._log.warning(
                "skill_creator_bootstrap_failed",
                rc=rc,
                stderr=stderr_bytes.decode("utf-8", "replace")[:500],
            )
            await self._bootstrap_notify_failure(rc=rc, reason="failed")
            return
        # Success — drop the stale notification marker so a future regression
        # is allowed to notify again (review fix #11 auto-reset semantics).
        self._bootstrap_clear_notify_marker()
        self._log.info("skill_creator_bootstrap_ok")

    # ------------------------------------------------------------------
    # Review fix #7 + #11: the marker stores `{"rc", "reason", "ts"}` so we
    # can decide whether to re-notify. Creation uses `O_CREAT | O_EXCL` to
    # close a parallel-start race where two daemons would both find the
    # marker absent and both send the Telegram message.

    def _bootstrap_marker_path(self) -> Path:
        return self._settings.data_dir / "run" / ".bootstrap_notified"

    def _bootstrap_clear_notify_marker(self) -> None:
        marker = self._bootstrap_marker_path()
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover -- best-effort cleanup
            self._log.warning("bootstrap_marker_clear_failed", error=repr(exc))

    def _should_notify_bootstrap(self, rc: int) -> bool:
        """Decide whether a fresh Telegram notification should go out.

        Returns True and unlinks the existing marker (so the subsequent
        O_EXCL create succeeds) when the marker is stale:
          * absent                       — first failure,
          * older than _BOOTSTRAP_NOTIFY_COOLDOWN_S (7 d), OR
          * records a different rc        — regression / new condition.
        Returns False when the marker is fresh *and* the rc matches — a
        predictable repeat that we already told the operator about.

        The race guard stays honest: a parallel daemon between our unlink
        and our caller's `O_EXCL` will cause `FileExistsError` there,
        which the caller swallows (another process already notified).
        """
        marker = self._bootstrap_marker_path()
        if not marker.exists():
            return True
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            last_ts = float(data.get("ts_epoch", 0.0))
            last_rc_raw = data.get("rc")
            last_rc = int(last_rc_raw) if last_rc_raw is not None else None
        except (OSError, ValueError, TypeError):
            # Corrupt marker: drop it so we can rewrite cleanly.
            marker.unlink(missing_ok=True)
            return True
        stale = time.time() - last_ts > _BOOTSTRAP_NOTIFY_COOLDOWN_S or last_rc != rc
        if stale:
            # Make way for the O_EXCL create in _bootstrap_notify_failure.
            marker.unlink(missing_ok=True)
            return True
        return False

    async def _bootstrap_notify_failure(self, *, rc: int, reason: str) -> None:
        """Send a Telegram message to the owner on bootstrap failure.

        Gated by a JSON marker that records `rc`/`reason`/`ts_epoch` so:
        * Parallel daemons can't both send (O_EXCL on marker creation).
        * Persistent failure doesn't re-spam on every restart.
        * A rc-change OR a >7d-old marker unlocks a new notification.
        """
        if self._adapter is None:
            return
        if not self._should_notify_bootstrap(rc):
            return
        marker = self._bootstrap_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(
                str(marker),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_TRUNC,
                0o644,
            )
        except FileExistsError:
            # Another process beat us to the marker in the split second
            # between `_should_notify_bootstrap` and `os.open`. Honour
            # its decision — don't double-notify.
            return
        payload = {
            "rc": rc,
            "reason": reason,
            "ts_epoch": time.time(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            os.write(fd, json.dumps(payload).encode("utf-8"))
        finally:
            os.close(fd)

        msg = (
            "Автобутстрап skill-creator не удался — marketplace-установка "
            f"временно недоступна (rc={rc}, reason={reason}). "
            "Проверь `gh auth status` и логи."
        )
        try:
            await self._adapter.send_text(self._settings.owner_chat_id, msg)
        except Exception as exc:
            # Marker already written: we'd retry on the next rc-change or
            # after the cooldown expires. Either is acceptable.
            self._log.warning("bootstrap_notify_failed", error=repr(exc))

    # ------------------------------------------------------------------

    def _ensure_vault(self) -> None:
        """Create the vault root (0o700) and log a warning if it is loose.

        The memory CLI also does this on every invocation, but doing it here
        at daemon startup (a) makes the `{vault_dir}` reference in the
        system prompt point to something that exists, and (b) surfaces the
        advisory-flock failure as a Telegram notice (future phase 5) rather
        than hiding it behind a single CLI error.

        We do NOT trigger the S3 lock probe here (it's CLI-local). An
        advisory-flock filesystem will surface via `exit 5` on the first
        `memory write`, which the model is instructed to relay to the owner
        in SKILL.md.
        """
        vault_dir = self._settings.vault_dir
        try:
            vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            (vault_dir / ".tmp").mkdir(exist_ok=True, mode=0o700)
        except OSError as exc:
            self._log.warning("vault_init_failed", error=repr(exc), path=str(vault_dir))
            return
        try:
            mode = vault_dir.stat().st_mode & 0o777
        except OSError:
            return
        if mode & 0o077:
            self._log.warning(
                "vault_dir_permissions_too_open",
                path=str(vault_dir),
                mode=oct(mode),
            )

    async def start(self) -> None:
        await _preflight_claude_cli(self._log)
        ensure_skills_symlink(self._settings.project_root)

        self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self._settings.data_dir / "run").mkdir(parents=True, exist_ok=True)
        self._ensure_vault()
        self._conn = await connect(self._settings.db_path)
        await apply_schema(self._conn)

        conv = ConversationStore(self._conn)
        turns = TurnStore(self._conn, lock=conv.lock)

        # A previous process may have crashed mid-turn; promote any orphan
        # 'pending' rows to 'interrupted' so they're excluded from history.
        swept = await turns.sweep_pending()
        if swept:
            self._log.warning("startup_swept_pending_turns", count=swept)

        if shutil.which("gh") is None:
            self._log.warning(
                "gh_cli_not_found",
                hint=("marketplace features disabled. Install https://cli.github.com/ to enable."),
            )

        bridge = ClaudeBridge(self._settings)

        self._adapter = TelegramAdapter(self._settings)
        handler = ClaudeHandler(self._settings, conv, turns, bridge)
        self._adapter.set_handler(handler)
        await self._adapter.start()

        # Fire-and-forget housekeeping. `Daemon.start()` must not wait on
        # GitHub or `uv sync`; bootstrap has its own 120 s timeout internally.
        self._spawn_bg(self._sweep_run_dirs(), name="sweep_run_dirs")
        self._spawn_bg(self._bootstrap_skill_creator_bg(), name="skill_creator_bootstrap")

        self._log.info("daemon_started", owner=self._settings.owner_chat_id)

    async def stop(self) -> None:
        self._log.info("daemon_stopping")
        if self._adapter is not None:
            try:
                await self._adapter.stop()
            except Exception:
                self._log.warning("stop_step_failed", step="adapter", exc_info=True)
        if self._bg_tasks:
            # Review fix #10: wrap the drain in a 5-second timeout so a
            # hung bg-task (e.g. DNS resolution deadlocked inside the
            # bootstrap subprocess) cannot block daemon shutdown forever.
            # On timeout we cancel the remaining tasks and await them a
            # second time; the second gather is bounded by the cancel
            # response time, not the task's natural duration.
            self._log.info("daemon_draining_bg_tasks", count=len(self._bg_tasks))
            pending = list(self._bg_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=_STOP_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                self._log.warning(
                    "daemon_bg_drain_timeout",
                    count=len([t for t in pending if not t.done()]),
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                self._log.warning("stop_step_failed", step="db", exc_info=True)
        self._log.info("daemon_stopped")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    d = Daemon(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)
    try:
        await d.start()
        await stop_event.wait()
    finally:
        await d.stop()
