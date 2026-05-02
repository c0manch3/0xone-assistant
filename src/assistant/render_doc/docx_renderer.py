"""Phase 9 §2.7 — DOCX renderer (pandoc native).

Pipeline:
  1. Stage ``content_md`` to ``<artefact_dir>/.staging/<uuid>.md``.
  2. ``pandoc -f <variant> -t docx -o <final_path> <staging_md>``.
  3. Always cleanup ``.staging/`` in ``finally`` (CRIT-4).
  4. Output cap ``docx_max_bytes`` checked post-render.

Same markdown variant as PDF (CRIT-2 consistency — pandoc parses
input the same way; raw HTML in input still produces unwanted side-
effects in DOCX output).

Image references (``![](...)``) are NOT supported in v1 — pandoc
emits broken-image placeholder. See §5 Явно НЕ #3.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from uuid import uuid4

from assistant.config import RenderDocSettings
from assistant.logger import get_logger
from assistant.render_doc._subprocess import PandocError, run_pandoc
from assistant.render_doc.pdf_renderer import PDFRenderError as _PDFRenderError

log = get_logger("render_doc.docx_renderer")


_DOCX_MARKDOWN_VARIANT = (
    "markdown"
    "-raw_html"
    "-raw_tex"
    "-raw_attribute"
    "-tex_math_dollars"
    "-tex_math_single_backslash"
    "-yaml_metadata_block"
)


# Reuse the same exception type as the PDF renderer for uniform error
# handling at the @tool body level. The single PDFRenderError type was
# named at PDF time; rename in phase 10 to RenderError when a third
# renderer joins.
DOCXRenderError = _PDFRenderError


async def render_docx(
    content_md: str,
    *,
    final_path: Path,
    staging_dir: Path,
    settings: RenderDocSettings,
) -> int:
    """Render ``content_md`` to DOCX at ``final_path``. Returns bytes
    written. Raises :class:`DOCXRenderError` on any failure."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    uid = uuid4().hex
    staging_md = staging_dir / f"{uid}.md"

    try:
        staging_md.write_text(content_md, encoding="utf-8")

        rc, _stdout, stderr_b = await run_pandoc(
            [
                "pandoc",
                "-f",
                _DOCX_MARKDOWN_VARIANT,
                "-t",
                "docx",
                "-o",
                str(final_path),
                str(staging_md),
            ],
            timeout_s=settings.docx_pandoc_timeout_s,
            settings=settings,
        )
        if rc != 0:
            stderr = stderr_b.decode("utf-8", "replace")[:256]
            raise DOCXRenderError(
                "render_failed_input_syntax",
                f"pandoc-exit-{rc}",
                message=stderr,
            )

        if not final_path.exists():
            raise DOCXRenderError(
                "render_failed_internal",
                "pandoc-no-output",
                message=f"pandoc rc=0 but {final_path} missing",
            )

        bytes_out = final_path.stat().st_size
        if bytes_out > settings.docx_max_bytes:
            with contextlib.suppress(OSError):
                final_path.unlink(missing_ok=True)
            raise DOCXRenderError(
                "render_failed_output_cap",
                "docx-too-large",
                message=f"{bytes_out} > {settings.docx_max_bytes}",
            )
        return bytes_out
    except PandocError as exc:
        raise DOCXRenderError(
            "render_failed_input_syntax",
            exc.error_code,
            message=exc.stderr,
        ) from exc
    finally:
        try:
            staging_md.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "render_doc_docx_staging_cleanup_failed",
                path=str(staging_md),
                error=repr(exc),
            )
