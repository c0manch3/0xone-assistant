"""Phase 7 / commit 17 — E2E document → extract_doc → summary.

This file asserts the cross-commit glue between (a) the Telegram
document ingress path (adapter builds a `MediaAttachment(kind='document',
...)`), (b) the handler envelope that surfaces a system-note pointing
the model at `tools/extract_doc/`, and (c) the real `tools/extract_doc/
main.py` CLI the model invokes via Bash to pull text out of a PDF /
DOCX / TXT blob.

Scenarios:

  * **Handler surfaces the extract_doc hint.** Document attachment →
    system-note naming the file + `tools/extract_doc/`. No image
    block, because documents are text artefacts.
  * **CLI extracts a TXT document end-to-end.** We skip PDF / DOCX
    generation here because each of those paths has a dedicated unit
    file; the E2E surface is enough to prove the glue is intact. TXT
    is the simplest format that exercises the full exit-code /
    stdout-JSON contract.
  * **CLI extracts a PDF document.** Uses fpdf2 to generate a real
    PDF fixture (fpdf2 + pypdf are both root deps since commit 2b,
    so the fixture generation costs nothing at runtime).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, MediaSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

_EXTRACT_CLI = (
    Path(__file__).resolve().parents[1] / "tools" / "extract_doc" / "main.py"
)


class _SpyBridge:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, image_blocks
        self.last_system_notes = (
            list(system_notes) if system_notes is not None else None
        )
        for item in self._items:
            yield item


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(),
    )


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
        result="ok",
        uuid="u",
    )


def _run_extract(
    *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI via subprocess, mirroring the Bash allowlist path."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_EXTRACT_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


async def test_document_attachment_surfaces_extract_hint(tmp_path: Path) -> None:
    """Handler builds a system-note that names the document + CLI.

    This is what the model sees; it decides whether to invoke the CLI
    inline (`Bash("python tools/extract_doc/main.py /path/doc.pdf
    --max-chars 50000")`) or delegate to a worker subagent for long
    PDFs.
    """
    conn = await connect(tmp_path / "e2e_doc.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    inbox = tmp_path / "data" / "media" / "inbox"
    inbox.mkdir(parents=True)
    doc = inbox / "memo.txt"
    doc.write_bytes(b"hello, document content here\n")

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="summarising"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=55,
        text="summarise this",
        attachments=(
            MediaAttachment(
                kind="document",
                local_path=doc,
                mime_type="text/plain",
                file_size=doc.stat().st_size,
                filename_original="memo.txt",
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_system_notes is not None
    doc_notes = [n for n in bridge.last_system_notes if "document" in n]
    assert doc_notes, bridge.last_system_notes
    assert "memo.txt" in doc_notes[0]
    assert str(doc) in doc_notes[0]
    # The note MUST point the model at the underscore package name —
    # the hyphenated form (`tools/extract-doc/`) would hit the Bash
    # allowlist reject branch (pitfall #11).
    assert "tools/extract_doc" in doc_notes[0]

    await conn.close()


def test_extract_cli_pulls_text_from_plain_txt(tmp_path: Path) -> None:
    """CLI happy path on TXT. Stdout carries a single JSON line with
    `{"ok": true, "text": ..., "format": "txt", ...}` per §2.9."""
    doc = tmp_path / "note.txt"
    doc.write_text("Resolver returned path: /absolute/outbox/x.png\n", encoding="utf-8")

    proc = _run_extract(str(doc))
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True
    assert payload["format"] == "txt"
    assert "Resolver returned path" in payload["text"]
    # size_bytes must reflect the source; chars is the post-truncate count.
    assert payload["size_bytes"] == doc.stat().st_size
    assert payload["chars"] > 0


def test_extract_cli_pulls_text_from_pdf(tmp_path: Path) -> None:
    """CLI happy path on PDF — exercises the pypdf dispatch branch.

    fpdf2 is a root dep (commit 2b) so the fixture costs nothing. This
    guards against a regression where the handler's note hints at
    `tools/extract_doc/` but the CLI crashes on the simplest possible
    PDF a production run would produce.
    """
    from fpdf import FPDF

    doc = tmp_path / "report.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(0, 10, "PhaseSevenSummary: this is the extract end-to-end probe.")
    pdf.output(str(doc))

    proc = _run_extract(str(doc))
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is True
    assert payload["format"] == "pdf"
    assert "PhaseSevenSummary" in payload["text"]
    # One-page document → pages==1.
    assert payload["units"] == 1


def test_extract_cli_rejects_missing_path(tmp_path: Path) -> None:
    """Exit code 3 for a non-existent input — matches §2.9 contract.

    The handler's system-note is best-effort; if the sweeper evicted
    the file between ingress and the model's Bash call, the CLI MUST
    fail cleanly with a structured JSON error rather than silently
    emitting an empty transcript.
    """
    missing = tmp_path / "gone.txt"
    proc = _run_extract(str(missing))
    assert proc.returncode == 3
    # stderr carries `{"ok": false, "error": ...}`.
    payload = json.loads(proc.stderr.strip())
    assert payload["ok"] is False
    err = payload["error"].lower()
    assert "path resolve failed" in err or "not a regular file" in err
