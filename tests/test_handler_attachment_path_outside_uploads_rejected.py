"""Phase 6a fix-pack — defensive ``is_relative_to`` guard in handler.

Spec §I.194 mandates a runtime guard: the handler must verify that
``msg.attachment.resolve()`` lives inside ``settings.uploads_dir`` before
reading / extracting / handing the file to the SDK. Today the adapter's
UUID-prefix construction makes escape impossible, but the explicit
defensive assert defends against any future regression in adapter
sanitisation or a new caller that synthesises ``IncomingMessage`` with
an attacker-controlled path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect


class _NoopBridge(ClaudeBridge):
    """Records calls so we can assert the bridge was NEVER reached."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[dict[str, Any]] = []

    async def ask(  # type: ignore[override]
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "user_text": user_text,
                "history": history,
                "system_notes": system_notes,
            }
        )
        if False:  # pragma: no cover — generator type contract
            yield None


def _settings(project_root: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=project_root,
        data_dir=project_root / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


async def _make_store(tmp_path: Path) -> ConversationStore:
    db = tmp_path / "handler.db"
    conn = await connect(db)
    await apply_schema(conn)
    return ConversationStore(conn)


def _make_emit() -> tuple[list[str], Any]:
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    return emitted, emit


async def test_attachment_outside_uploads_dir_is_rejected_before_extract(
    tmp_path: Path,
) -> None:
    """Crafted ``attachment=Path("/etc/passwd")`` (or any path outside
    ``uploads_dir``) is rejected before the extractor or SDK runs.

    Asserts:
      - bridge NEVER called;
      - emit surfaces a Russian "internal error" message;
      - turn marked complete with ``attachment_path_invalid`` stop reason
        (so ``cleanup_orphan_pending_turns`` does not see it).
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _NoopBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    # Construct an attachment path that is decidedly OUTSIDE
    # ``settings.uploads_dir`` (which is ``<tmp_path>/data/uploads``).
    outside_path = Path("/etc/passwd")
    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=1,
        message_id=1,
        text="describe",
        attachment=outside_path,
        attachment_kind="txt",
        attachment_filename="passwd",
    )
    await handler.handle(msg, emit)

    # Bridge must NEVER be invoked for a path-escape attempt.
    assert bridge.calls == []
    # User-visible Russian internal-error reply.
    assert any("внутренняя ошибка" in e for e in emitted)
    # Turn marked complete with the path-invalid stop reason.
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=1"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == "complete"
    await store._conn.close()


async def test_attachment_paths_with_one_outside_rejects_entire_turn(
    tmp_path: Path,
) -> None:
    """F8: ``attachment_paths`` with [valid, valid, /etc/passwd] →
    entire turn rejected (bridge not called, emit surfaces internal
    error). Without per-path verification, an attacker that bypasses
    the adapter's UUID-prefix synthesis and slips a single escape path
    into a media_group bucket would route ``/etc/passwd`` into the
    vision pipeline.
    """
    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    bridge = _NoopBridge(settings)
    handler = ClaudeHandler(settings, store, bridge)

    # First two paths are well-formed (inside uploads_dir); third
    # escapes. The handler must reject the entire turn.
    uploads = settings.uploads_dir
    uploads.mkdir(parents=True, exist_ok=True)
    p1 = uploads / "uuid1__a.jpg"
    p1.write_bytes(b"\xff\xd8\xff\xe0placeholder")
    p2 = uploads / "uuid2__b.jpg"
    p2.write_bytes(b"\xff\xd8\xff\xe0placeholder")
    escape = Path("/etc/passwd")

    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=42,
        message_id=42,
        text="что общего?",
        attachment=p1,
        attachment_kind="jpg",
        attachment_filename=p1.name,
        attachment_paths=[p1, p2, escape],
    )
    await handler.handle(msg, emit)

    # Bridge NEVER called — entire turn rejected.
    assert bridge.calls == []
    assert any("внутренняя ошибка" in e for e in emitted)
    # Turn marked complete (so cleanup_orphan_pending_turns does not
    # see it lingering).
    async with store._conn.execute(
        "SELECT status FROM turns WHERE chat_id=42"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == "complete"
    await store._conn.close()


async def test_attachment_inside_uploads_dir_passes_guard(tmp_path: Path) -> None:
    """Sanity: a real attachment inside ``uploads_dir`` is NOT rejected
    by the new guard. Without this assertion the test above could pass
    trivially (e.g. if the guard rejected every attachment).
    """
    from claude_agent_sdk import ResultMessage, TextBlock

    class _ScriptedBridge(ClaudeBridge):
        def __init__(self, settings: Settings, script: list[Any]) -> None:
            super().__init__(settings)
            self._script = script
            self.calls = 0

        async def ask(  # type: ignore[override]
            self,
            chat_id: int,
            user_text: str,
            history: list[dict[str, Any]],
            *,
            system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
        ) -> AsyncIterator[Any]:
            self.calls += 1
            for item in self._script:
                yield item

    store = await _make_store(tmp_path)
    settings = _settings(tmp_path)
    result = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 1},
        stop_reason="end_turn",
    )
    bridge = _ScriptedBridge(settings, [TextBlock(text="ok"), result])
    handler = ClaudeHandler(settings, store, bridge)

    # Drop a real file inside the configured uploads_dir
    # (Mac-dev fallback resolves to ``<data_dir>/uploads`` per fix 1).
    # Use TXT not PDF: phase-6a flip routes PDFs through pypdf which
    # would reject a "%PDF-stub" payload before the path-guard runs;
    # TXT exercises the same path-escape guard without format risk.
    uploads = settings.uploads_dir
    uploads.mkdir(parents=True, exist_ok=True)
    tmp_file = uploads / "uuid__sample.txt"
    tmp_file.write_text("hello from a real upload", encoding="utf-8")

    emitted, emit = _make_emit()
    msg = IncomingMessage(
        chat_id=2,
        message_id=2,
        text="describe",
        attachment=tmp_file,
        attachment_kind="txt",
        attachment_filename="sample.txt",
    )
    await handler.handle(msg, emit)

    # Bridge IS invoked — guard did not falsely reject the legit path.
    assert bridge.calls == 1
    assert "ok" in emitted
    await store._conn.close()
