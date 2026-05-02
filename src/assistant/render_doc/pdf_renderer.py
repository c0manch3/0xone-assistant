"""Phase 9 §2.6 — PDF renderer (pandoc → HTML5 → WeasyPrint).

Pipeline:
  1. Stage ``content_md`` to ``<artefact_dir>/.staging/<uuid>.md``.
  2. ``pandoc -f <variant> -t html5 -o <staging_html> <staging_md>``
     via ``asyncio.create_subprocess_exec`` (W2-MED-1 variant subtracts
     ``raw_html``, ``raw_tex``, ``raw_attribute``, ``tex_math_dollars``,
     ``tex_math_single_backslash``, ``yaml_metadata_block``).
  3. ``WeasyPrint.HTML(string=html, url_fetcher=SafeURLFetcher())`` →
     ``write_pdf(<final_path>)`` via ``asyncio.to_thread`` (CPU-bound).
  4. Always cleanup ``.staging/`` files in ``finally`` (CRIT-4).
  5. Output cap ``pdf_max_bytes`` checked post-render.

R1.2 closure — :class:`SafeURLFetcher` raises
:class:`weasyprint.urls.FatalURLFetchingError` (extends
:class:`BaseException`) so WeasyPrint **aborts render** rather than
swallowing the IOError variant. v2 used the IOError variant (silent
placeholder); v3 propagates and aborts so AC#14 has explicit failure
semantics.

W2-MED-2: ALL schemes blocked, including ``data:`` (image embedding
of any kind is non-goal phase 9, §5 #3).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from assistant.config import RenderDocSettings
from assistant.logger import get_logger
from assistant.render_doc._subprocess import PandocError, run_pandoc

# weasyprint has no py.typed marker; we accept untyped imports here
# (lazy-loaded inside helpers anyway).
if TYPE_CHECKING:
    pass

log = get_logger("render_doc.pdf_renderer")

# Pandoc markdown variant subtracts: raw_html, raw_tex, raw_attribute
# (CRIT-2), tex_math_dollars + tex_math_single_backslash (W2-MED-1
# inline math defang), yaml_metadata_block (R1.1 closure — close YAML
# frontmatter smuggling surface).
_PDF_MARKDOWN_VARIANT = (
    "markdown"
    "-raw_html"
    "-raw_tex"
    "-raw_attribute"
    "-tex_math_dollars"
    "-tex_math_single_backslash"
    "-yaml_metadata_block"
)


class WeasyPrintImportError(RuntimeError):
    """Raised when ``import weasyprint`` fails at runtime.

    Distinguished from :class:`ImportError` so the subsystem's
    startup_check can map to ``force_disabled_formats={'pdf'}`` while
    leaving DOCX/XLSX intact (HIGH-5 partial force-disable).
    """


def _build_safe_url_fetcher() -> Any:
    """Construct a :class:`SafeURLFetcher` instance lazily.

    WeasyPrint's import chain pulls in libgobject / libpango via
    cffi at module-load time. On hosts without those shared libs
    (Mac dev box without Homebrew Pango) the import raises
    :class:`OSError` from ``cffi.api._make_ffi_library``. We defer
    to call time so the rest of the package imports cleanly.
    """
    try:
        from weasyprint.urls import (  # type: ignore[import-untyped]
            FatalURLFetchingError,
            URLFetcher,
        )
    except (ImportError, OSError) as exc:
        raise WeasyPrintImportError(
            f"weasyprint unavailable: {exc!r}"
        ) from exc

    class SafeURLFetcher(URLFetcher):  # type: ignore[misc]
        """Deny EVERY URL fetch (CRIT-2 + W2-MED-2 closure).

        Subclasses :class:`weasyprint.urls.URLFetcher` (forward-compat
        to WeasyPrint 69.0+ which removes the legacy ``def fetcher``
        shape). Raises :class:`FatalURLFetchingError` (extends
        :class:`BaseException`) so WeasyPrint aborts rendering rather
        than silently swallowing the :class:`IOError` variant.
        """

        def fetch(
            self,
            url: str,
            headers: dict[str, str] | None = None,
        ) -> Any:
            raise FatalURLFetchingError(
                f"render_doc: all url-fetches blocked "
                f"(got url={url[:64]!r})"
            )

    return SafeURLFetcher()


def _weasyprint_write_pdf_sync(
    html_str: str,
    final_path: Path,
    base_url: str,
) -> None:
    """Synchronous WeasyPrint render (called via ``asyncio.to_thread``).

    Constructed inside a thread so the cffi/cairo init on first call
    doesn't block the event loop. Errors propagate; the caller maps
    :class:`FatalURLFetchingError` to ``render_failed_input_syntax``.
    """
    # Lazy import — keeps the module-level surface clean on hosts
    # without libpango (e.g. Mac dev box).
    try:
        import weasyprint  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:  # pragma: no cover - host-specific
        raise WeasyPrintImportError(
            f"weasyprint unavailable: {exc!r}"
        ) from exc

    fetcher = _build_safe_url_fetcher()
    html = weasyprint.HTML(
        string=html_str,
        base_url=base_url,
        url_fetcher=fetcher,
    )
    html.write_pdf(target=str(final_path))


class PDFRenderError(RuntimeError):
    """Raised by :func:`render_pdf` on any failure.

    Carries an ``error_code`` (kebab-case, machine-parseable) the
    @tool body maps to the envelope ``error`` field; ``reason`` is
    one of the 3 ``render_failed_*`` reasons (MED-3).
    """

    def __init__(
        self,
        reason: str,
        error_code: str,
        *,
        message: str = "",
    ) -> None:
        super().__init__(f"{reason}: {error_code}: {message}")
        self.reason = reason
        self.error_code = error_code


async def render_pdf(
    content_md: str,
    *,
    final_path: Path,
    staging_dir: Path,
    settings: RenderDocSettings,
) -> int:
    """Render ``content_md`` to PDF at ``final_path``.

    Returns ``bytes`` written. Raises :class:`PDFRenderError` on any
    pipeline failure with ``reason`` ∈ {"render_failed_input_syntax",
    "render_failed_output_cap", "render_failed_internal"}.

    Always cleans staging files in ``finally`` (CRIT-4).
    """
    import asyncio

    staging_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex
    staging_md = staging_dir / f"{uid}.md"
    staging_html = staging_dir / f"{uid}.html"

    try:
        staging_md.write_text(content_md, encoding="utf-8")

        # Step 1: pandoc → HTML5.
        rc, _stdout, stderr_b = await run_pandoc(
            [
                "pandoc",
                "-f",
                _PDF_MARKDOWN_VARIANT,
                "-t",
                "html5",
                "-o",
                str(staging_html),
                str(staging_md),
            ],
            timeout_s=settings.pdf_pandoc_timeout_s,
            settings=settings,
        )
        if rc != 0:
            stderr = stderr_b.decode("utf-8", "replace")[:256]
            raise PandocError(
                error_code=f"pandoc-exit-{rc}",
                message=stderr,
                returncode=rc,
            )

        if not staging_html.exists():
            raise PDFRenderError(
                "render_failed_internal",
                "pandoc-no-output",
                message=f"pandoc rc=0 but {staging_html} missing",
            )

        html_str = staging_html.read_text(encoding="utf-8")

        # Step 2: WeasyPrint via asyncio.to_thread (CPU-bound).
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    _weasyprint_write_pdf_sync,
                    html_str,
                    final_path,
                    str(staging_dir),
                ),
                timeout=settings.pdf_weasyprint_timeout_s,
            )
        except WeasyPrintImportError as exc:
            raise PDFRenderError(
                "render_failed_internal",
                "weasyprint-import-failed",
                message=str(exc),
            ) from exc
        except TimeoutError as exc:
            raise PDFRenderError(
                "render_failed_internal",
                "weasyprint-timeout",
                message="WeasyPrint render exceeded timeout",
            ) from exc
        except BaseException as exc:
            # Catch FatalURLFetchingError + any cffi / pango runtime
            # explosion. ``BaseException`` because FatalURLFetchingError
            # extends BaseException (R1.2).
            tname = type(exc).__name__
            if "FatalURLFetchingError" in tname:
                raise PDFRenderError(
                    "render_failed_input_syntax",
                    "weasyprint-url-fetch-blocked",
                    message=str(exc),
                ) from exc
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise PDFRenderError(
                "render_failed_internal",
                "weasyprint-error",
                message=f"{tname}: {exc!s}"[:256],
            ) from exc

        # Step 3: post-render output cap check.
        bytes_out = final_path.stat().st_size
        if bytes_out > settings.pdf_max_bytes:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            raise PDFRenderError(
                "render_failed_output_cap",
                "pdf-too-large",
                message=f"{bytes_out} > {settings.pdf_max_bytes}",
            )
        return bytes_out
    except PandocError as exc:
        raise PDFRenderError(
            "render_failed_input_syntax",
            exc.error_code,
            message=exc.stderr,
        ) from exc
    finally:
        # CRIT-4: always cleanup staging.
        for p in (staging_md, staging_html):
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                log.warning(
                    "render_doc_pdf_staging_cleanup_failed",
                    path=str(p),
                    error=repr(exc),
                )
