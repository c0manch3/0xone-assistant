"""Phase 9: ``render_doc`` PDF/DOCX/XLSX generation subsystem.

Mirror of phase-8 ``vault_sync`` package shape — opt-in subsystem with a
single MCP @tool (``mcp__render_doc__render_doc``) under
``settings.render_doc``. The owner's request "сгенерь PDF/DOCX/XLSX
отчёт ..." flows through the @tool, lands a file under
``<data_dir>/artefacts/``, the bridge yields an :class:`ArtefactBlock`,
the handler delivers via ``adapter.send_document``, and the TTL sweeper
cleans the artefact 10 minutes after delivery.

Public surface:
  - :class:`RenderDocSubsystem` — daemon-owned subsystem.
  - :class:`ArtefactBlock` — bridge → handler envelope.
  - :func:`_cleanup_stale_artefacts` — boot-time hygiene (mirrors
    phase-6a ``_boot_sweep_uploads`` + phase-8
    ``_cleanup_stale_vault_locks``).
"""

from __future__ import annotations

from assistant.render_doc.boot import _cleanup_stale_artefacts
from assistant.render_doc.subsystem import (
    ArtefactBlock,
    ArtefactRecord,
    FilenameInvalid,
    RenderDocSubsystem,
    RenderResult,
)

__all__ = [
    "ArtefactBlock",
    "ArtefactRecord",
    "FilenameInvalid",
    "RenderDocSubsystem",
    "RenderResult",
    "_cleanup_stale_artefacts",
]
