"""Phase 9 §2.2 — :class:`RenderDocSubsystem` (the central class).

A daemon-owned subsystem holding:

  - ``_artefacts: dict[Path, ArtefactRecord]`` — in-memory live-set
    ledger of rendered artefacts. Sweeper-safe via ``_artefacts_lock``
    (asyncio.Lock).
  - ``_artefacts_lock: asyncio.Lock`` — protects all ledger mutations
    (W2-HIGH-2).
  - ``_render_sem: asyncio.Semaphore`` — bounds concurrent renders to
    ``render_max_concurrent`` (default 2; protects WeasyPrint peak
    RSS).
  - ``_pending: set[asyncio.Task]`` — drain set the daemon awaits in
    ``Daemon.stop`` BEFORE ``_bg_tasks`` cancel (CRIT-4 §2.12).
  - ``_force_disabled: bool`` + ``force_disabled_formats: set[str]``
    — startup_check populates per-format toggles (HIGH-5).

Does NOT own scheduler-style cron — the only background loop is the
TTL sweeper, spawned via :meth:`assistant.main.Daemon._spawn_bg_supervised`
when the subsystem is enabled and not fully force-disabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from assistant.adapters.base import MessengerAdapter
from assistant.config import RenderDocSettings
from assistant.logger import get_logger
from assistant.render_doc._validate_paths import (
    FilenameInvalid,
)
from assistant.render_doc.audit import write_audit_row
from assistant.render_doc.docx_renderer import render_docx
from assistant.render_doc.pdf_renderer import (
    PDFRenderError,
    WeasyPrintImportError,
    render_pdf,
)
from assistant.render_doc.xlsx_renderer import render_xlsx

__all__ = [
    "ArtefactBlock",
    "ArtefactRecord",
    "FilenameInvalid",
    "RenderDocSubsystem",
    "RenderResult",
]

log = get_logger("render_doc.subsystem")

ALL_FORMATS = frozenset({"pdf", "docx", "xlsx"})


@dataclass
class ArtefactRecord:
    """One row in :attr:`RenderDocSubsystem._artefacts`.

    ``in_flight=True`` until handler calls ``mark_delivered``; sweeper
    skips ``in_flight=True`` records regardless of TTL (CRIT-3).
    """

    path: Path
    fmt: str
    suggested_filename: str
    created_at: float
    in_flight: bool = True
    delivered_at: float | None = None


@dataclass
class ArtefactBlock:
    """Bridge → handler envelope (yielded AFTER ToolResultBlock).

    Mirrors ``TextBlock`` / ``ToolUseBlock`` SDK block shape; handler
    accumulates these into a per-iteration ``pending_artefacts`` list
    and drains on every ``ResultMessage`` (CRIT-1 per-iteration flush
    barrier).
    """

    path: Path
    fmt: str
    suggested_filename: str
    tool_use_id: str


@dataclass
class RenderResult:
    """Outcome of a single :meth:`RenderDocSubsystem.render` call.

    The @tool body wraps this into the JSON envelope per spec §2.3.
    ``ok=False`` paths carry a ``reason`` + ``error`` (kebab-case
    machine-parseable code).
    """

    ok: bool
    fmt: str
    suggested_filename: str
    path: Path | None = None
    bytes_out: int = 0
    duration_ms: int = 0
    reason: str | None = None
    error: str | None = None


class RenderDocSubsystem:
    """Phase 9 ``render_doc`` subsystem.

    Constructed once by :meth:`assistant.main.Daemon.start` when
    ``settings.render_doc.enabled=True``. The daemon also drains
    :attr:`pending_set` (its own ``_render_doc_pending`` field) inside
    :meth:`Daemon.stop` BEFORE cancelling ``_bg_tasks`` (§2.12).
    """

    def __init__(
        self,
        *,
        artefact_dir: Path,
        settings: RenderDocSettings,
        adapter: MessengerAdapter | None,
        owner_chat_id: int,
        run_dir: Path,
        pending_set: set[asyncio.Task[Any]],
    ) -> None:
        self._artefact_dir = artefact_dir
        self._staging_dir = artefact_dir / ".staging"
        self._settings = settings
        self._adapter = adapter
        self._owner_chat_id = owner_chat_id
        self._run_dir = run_dir
        self._pending_set = pending_set
        self._audit_path = run_dir / "render-doc-audit.jsonl"
        self._artefacts: dict[Path, ArtefactRecord] = {}
        self._artefacts_lock = asyncio.Lock()
        self._render_sem = asyncio.Semaphore(
            settings.render_max_concurrent
        )
        self._force_disabled: bool = False
        self.disabled_reason: str | None = None
        self.force_disabled_formats: set[str] = set()
        self._notified_force_disable: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def startup_check(self) -> None:
        """Populate :attr:`force_disabled_formats` based on host env.

        - pandoc missing → ``{"pdf", "docx"}`` added.
        - ``import weasyprint`` failing (ImportError OR OSError for
          missing shared libs) → ``{"pdf"}`` added.
        - openpyxl always available (pure-Python wheel) → "xlsx"
          never blocked from startup_check.

        Fully force-disabled (= all 3 formats blocked) sets
        :attr:`_force_disabled` True; the @tool stays unregistered
        and a one-time Telegram notify fires.
        """
        if not self._settings.enabled:
            self._force_disabled = True
            self.disabled_reason = "settings_disabled"
            self.force_disabled_formats = set(ALL_FORMATS)
            return

        pandoc_present = shutil.which("pandoc") is not None
        if not pandoc_present:
            self.force_disabled_formats.update({"pdf", "docx"})
            log.warning(
                "render_doc_format_force_disabled",
                format="pdf",
                reason="pandoc-missing",
            )
            log.warning(
                "render_doc_format_force_disabled",
                format="docx",
                reason="pandoc-missing",
            )

        weasyprint_ok = True
        try:
            import weasyprint  # type: ignore[import-untyped]  # noqa: F401
        except (ImportError, OSError) as exc:
            weasyprint_ok = False
            log.warning(
                "render_doc_format_force_disabled",
                format="pdf",
                reason="weasyprint-import-failed",
                error=repr(exc),
            )
        if not weasyprint_ok:
            self.force_disabled_formats.add("pdf")

        if self.force_disabled_formats >= ALL_FORMATS:
            self._force_disabled = True
            reasons = []
            if not pandoc_present:
                reasons.append("pandoc-missing")
            if not weasyprint_ok:
                reasons.append("weasyprint-import-failed")
            self.disabled_reason = ",".join(reasons) or "unknown"
            log.error(
                "render_doc_force_disabled",
                reason=self.disabled_reason,
            )
        else:
            log.info(
                "render_doc_startup_check_passed",
                pandoc_present=pandoc_present,
                weasyprint_ok=weasyprint_ok,
                force_disabled_formats=sorted(self.force_disabled_formats),
            )

    @property
    def force_disabled(self) -> bool:
        return self._force_disabled

    def get_inflight_count(self) -> int:
        """Return live ledger size (W2-LOW-1 — RSS observer field).

        ``len(dict)`` is a single CPython opcode (ma_used read);
        no lock needed for size-only reads.
        """
        return len(self._artefacts)

    # ------------------------------------------------------------------
    # Ledger
    # ------------------------------------------------------------------
    async def register_artefact(
        self,
        path: Path,
        *,
        fmt: str,
        suggested_filename: str,
    ) -> None:
        """Add a new ledger row (W2-HIGH-2 lock acquired)."""
        async with self._artefacts_lock:
            self._artefacts[path] = ArtefactRecord(
                path=path,
                fmt=fmt,
                suggested_filename=suggested_filename,
                created_at=time.monotonic(),
                in_flight=True,
            )
        log.info(
            "render_doc_artefact_registered",
            path_basename=path.name,
            fmt=fmt,
        )

    async def mark_delivered(self, path: Path) -> None:
        """Flip ``in_flight=False`` + record ``delivered_at`` (W2-HIGH-2
        lock acquired)."""
        async with self._artefacts_lock:
            rec = self._artefacts.get(path)
            if rec is None:
                return
            rec.in_flight = False
            rec.delivered_at = time.monotonic()
        log.info(
            "render_doc_artefact_delivered",
            path_basename=path.name,
        )

    async def mark_orphans_delivered_at_shutdown(self) -> None:
        """Daemon.stop helper: flip ``in_flight=False`` on every
        record so next-boot mtime cleanup picks them up.

        Avoids the "handler crashed mid-delivery → record stuck
        in_flight forever" leak. Safe because next boot starts with
        an empty ledger — nothing observable to lose.
        """
        async with self._artefacts_lock:
            now = time.monotonic()
            for rec in self._artefacts.values():
                if rec.in_flight:
                    rec.in_flight = False
                    rec.delivered_at = now

    # ------------------------------------------------------------------
    # Sweeper
    # ------------------------------------------------------------------
    async def _sweep_iteration(self) -> None:
        """One sweep pass — extracted for testability + W2-HIGH-2 lock
        discipline.

        PHASE 1 (under lock): snapshot delete candidates.
        PHASE 2 (outside lock): disk I/O.
        PHASE 3 (under lock): pop deleted paths from ledger.
        """
        ttl = self._settings.artefact_ttl_s
        now = time.monotonic()

        async with self._artefacts_lock:
            snapshot: list[tuple[Path, ArtefactRecord]] = [
                (path, rec)
                for path, rec in self._artefacts.items()
                if not rec.in_flight
                and rec.delivered_at is not None
                and now - rec.delivered_at > ttl
            ]

        deleted_paths: list[Path] = []
        for path, _rec in snapshot:
            try:
                path.unlink(missing_ok=True)
                deleted_paths.append(path)
                log.info(
                    "render_doc_artefact_expired",
                    path_basename=path.name,
                )
            except OSError as exc:
                log.warning(
                    "render_doc_sweep_unlink_failed",
                    path=str(path),
                    error=repr(exc),
                )

        async with self._artefacts_lock:
            for path in deleted_paths:
                rec = self._artefacts.get(path)
                if rec is not None and not rec.in_flight:
                    self._artefacts.pop(path, None)

    async def _sweep_loop(self) -> None:
        """Supervised TTL sweeper — runs forever, ticks every
        ``sweep_interval_s`` seconds."""
        while True:
            await asyncio.sleep(self._settings.sweep_interval_s)
            try:
                await self._sweep_iteration()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("render_doc_sweep_iteration_error")

    # Compatibility alias for daemon spawning.
    async def loop(self) -> None:
        """Supervised loop entry point (matches phase-8
        ``vault_sync.loop`` shape)."""
        if self._force_disabled:
            log.info(
                "render_doc_loop_skipped_force_disabled",
                reason=self.disabled_reason,
            )
            return
        await self._sweep_loop()

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------
    def _default_filename(self, fmt: str) -> str:
        """Return ``f"{fmt}-<utc-iso-clean>"`` when caller passes None."""
        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        # Use ``-`` instead of ``:`` to keep filenames Windows-friendly.
        stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
        return f"{fmt}-{stamp}"

    async def render(
        self,
        content_md: str,
        fmt: str,
        filename: str | None,
        *,
        task_handle: asyncio.Task[Any] | None,
    ) -> RenderResult:
        """Public render entry point.

        Performs:
          1. Subsystem + per-format force-disable check.
          2. Input size cap.
          3. Filename sanitisation.
          4. Semaphore acquisition (concurrency cap).
          5. Dispatch to PDF/DOCX/XLSX renderer.
          6. Audit row append.
          7. Ledger registration.

        ``task_handle`` is the @tool body's ``asyncio.current_task()``
        — registered in :attr:`_pending_set` so ``Daemon.stop`` can
        drain in-flight renders BEFORE ``_bg_tasks`` cancel.
        """
        start = time.monotonic()
        suggested_base = filename or self._default_filename(fmt)
        suggested_filename = f"{suggested_base}.{fmt}"

        # Guards (BEFORE semaphore — disabled / oversized inputs must
        # not consume a slot).
        if self._force_disabled:
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason="disabled",
                error="subsystem-not-configured",
                start=start,
            )
        if fmt in self.force_disabled_formats:
            err = (
                f"format-{fmt}-unavailable-"
                f"{self.disabled_reason or 'binary-missing'}"
            )
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason="disabled",
                error=err,
                start=start,
            )
        if len(content_md.encode("utf-8")) > self._settings.max_input_bytes:
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason="input_too_large",
                error="content-md-over-cap",
                start=start,
            )

        # Register the in-flight task so Daemon.stop drain observes it.
        if task_handle is not None:
            self._pending_set.add(task_handle)
            task_handle.add_done_callback(self._pending_set.discard)

        async with self._render_sem:
            try:
                result = await self._dispatch(
                    content_md=content_md,
                    fmt=fmt,
                    suggested_filename=suggested_filename,
                    start=start,
                )
            except asyncio.CancelledError:
                # Fix-pack F5 (CR-3 ledger leak on timeout-cancel).
                # ``asyncio.wait_for`` may cancel the inner task at the
                # ``register_artefact`` await point or just after. The
                # ``_dispatch`` finally already unlinks ``final_path``
                # for in-progress renders; this branch covers the
                # narrower window where ``register_artefact`` ran but
                # the cancel hit before this method returned. Pop any
                # ledger row that snuck in and unlink the on-disk file
                # so the timeout doesn't leave a stale ``in_flight=True``
                # record + orphan artefact until daemon restart.
                async with self._artefacts_lock:
                    leaked: list[Path] = []
                    for path, rec in list(self._artefacts.items()):
                        if rec.in_flight and rec.suggested_filename == suggested_filename:
                            leaked.append(path)
                    for path in leaked:
                        self._artefacts.pop(path, None)
                for path in leaked:
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                raise
            finally:
                if task_handle is not None:
                    self._pending_set.discard(task_handle)
        return result

    def _fail(
        self,
        *,
        fmt: str,
        suggested_filename: str,
        reason: str,
        error: str,
        start: float,
    ) -> RenderResult:
        """Build a failed :class:`RenderResult` + write audit row."""
        duration_ms = int((time.monotonic() - start) * 1000)
        result = RenderResult(
            ok=False,
            fmt=fmt,
            suggested_filename=suggested_filename,
            duration_ms=duration_ms,
            reason=reason,
            error=error,
        )
        self._record_audit(
            fmt=fmt,
            result_str=("disabled" if reason == "disabled" else "failed"),
            filename=suggested_filename,
            bytes_out=None,
            duration_ms=duration_ms,
            error=error,
        )
        log.warning(
            "render_doc_failed",
            fmt=fmt,
            reason=reason,
            error=error,
            duration_ms=duration_ms,
        )
        return result

    async def _dispatch(
        self,
        *,
        content_md: str,
        fmt: str,
        suggested_filename: str,
        start: float,
    ) -> RenderResult:
        """Per-format renderer dispatch (called inside semaphore).

        On success: registers the artefact + writes audit + returns
        ``ok=True`` :class:`RenderResult`.

        On :class:`PDFRenderError`: maps reason/error to envelope +
        writes audit ``result="failed"``.

        Other exceptions: maps to ``render_failed_internal`` (MED-3).
        """
        self._artefact_dir.mkdir(parents=True, exist_ok=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        uid = uuid4().hex
        final_path = self._artefact_dir / f"{uid}.{fmt}"

        log.info(
            "render_doc_started",
            fmt=fmt,
            filename=suggested_filename,
            content_md_len=len(content_md),
        )

        try:
            if fmt == "pdf":
                bytes_out = await render_pdf(
                    content_md,
                    final_path=final_path,
                    staging_dir=self._staging_dir,
                    settings=self._settings,
                )
            elif fmt == "docx":
                bytes_out = await render_docx(
                    content_md,
                    final_path=final_path,
                    staging_dir=self._staging_dir,
                    settings=self._settings,
                )
            elif fmt == "xlsx":
                bytes_out = await render_xlsx(
                    content_md,
                    final_path=final_path,
                    settings=self._settings,
                )
            else:
                # SDK enum should reject; defensive branch (MED-2).
                with contextlib.suppress(OSError):
                    final_path.unlink(missing_ok=True)
                return self._fail(
                    fmt=fmt,
                    suggested_filename=suggested_filename,
                    reason="render_failed_internal",
                    error="format-unknown",
                    start=start,
                )
        except PDFRenderError as exc:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason=exc.reason,
                error=exc.error_code,
                start=start,
            )
        except WeasyPrintImportError as exc:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason="render_failed_internal",
                error=f"weasyprint-import-failed: {exc!s}"[:96],
                start=start,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            log.exception("render_doc_internal_error", fmt=fmt)
            return self._fail(
                fmt=fmt,
                suggested_filename=suggested_filename,
                reason="render_failed_internal",
                error=f"{type(exc).__name__}",
                start=start,
            )

        await self.register_artefact(
            final_path,
            fmt=fmt,
            suggested_filename=suggested_filename,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        result = RenderResult(
            ok=True,
            fmt=fmt,
            suggested_filename=suggested_filename,
            path=final_path,
            bytes_out=bytes_out,
            duration_ms=duration_ms,
        )
        self._record_audit(
            fmt=fmt,
            result_str="ok",
            filename=suggested_filename,
            bytes_out=bytes_out,
            duration_ms=duration_ms,
            error=None,
        )
        log.info(
            "render_doc_rendered",
            fmt=fmt,
            bytes_out=bytes_out,
            duration_ms=duration_ms,
            path_basename=final_path.name,
        )
        return result

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def _record_audit(
        self,
        *,
        fmt: str,
        result_str: str,
        filename: str,
        bytes_out: int | None,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """Append one JSONL audit row with date-stamped rotation."""
        row: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC)
            .replace(microsecond=0)
            .isoformat(),
            "format": fmt,
            "result": result_str,
            "filename": filename,
            "bytes": bytes_out,
            "duration_ms": duration_ms,
            "error": error,
            "schema_version": 1,
        }
        max_bytes = self._settings.audit_log_max_size_mb * 1024 * 1024
        try:
            write_audit_row(
                self._audit_path,
                row,
                max_size_bytes=max_bytes,
                keep_last_n=self._settings.audit_log_keep_last_n,
                truncate_chars=self._settings.audit_field_truncate_chars,
            )
        except OSError as exc:
            log.warning(
                "render_doc_audit_write_failed",
                path=str(self._audit_path),
                error=repr(exc),
            )

    # ------------------------------------------------------------------
    # Boot-time notify (HIGH-2)
    # ------------------------------------------------------------------
    async def notify_force_disabled_if_needed(self) -> None:
        """One-time Telegram notify to owner when the subsystem is
        fully force-disabled at boot.

        Wraps the send in ``asyncio.wait_for(timeout=10s)`` per phase-8
        F9 precedent so a slow / dead Telegram doesn't block the rest
        of ``Daemon.start``.
        """
        if not self._force_disabled:
            return
        if self._notified_force_disable:
            return
        if self._adapter is None:
            return
        text = (
            "render_doc subsystem force-disabled "
            f"({self.disabled_reason or 'unknown'}). "
            "PDF/DOCX/XLSX rendering unavailable until pandoc + "
            "WeasyPrint are present on the host."
        )
        try:
            await asyncio.wait_for(
                self._adapter.send_text(self._owner_chat_id, text),
                timeout=10.0,
            )
            self._notified_force_disable = True
        except (TimeoutError, Exception) as exc:
            log.warning(
                "render_doc_force_disable_notify_failed",
                error=repr(exc),
            )
