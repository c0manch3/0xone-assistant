from __future__ import annotations

import asyncio
import contextlib
import re as _re
import shutil
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import Emit, IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.files.extract import (
    EXTRACTORS,
    POST_EXTRACT_CHAR_CAP,
    ExtractionError,
)
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore

log = get_logger("handlers.message")

# ---------------------------------------------------------------------------
# URL detector (phase 3)
#
# B9 fix (wave-2): trailing punctuation was captured into the URL, so
# "see https://github.com/foo/bar." would yield the literal string
# "https://github.com/foo/bar." (trailing dot) — which then fails
# GitHub routing and confuses the system-note hint to the model.
# Approach: keep the broad ``\S+`` match, then strip trailing punctuation
# characters that are almost never part of a real URL.
# ---------------------------------------------------------------------------
_URL_RE = _re.compile(r"https?://\S+|git@[^\s:]+:\S+", _re.IGNORECASE)
# S7 wave-3: backtick added — markdown inline code like ``\`https://foo\```
# previously emitted ``https://foo`` ``with trailing backtick intact``,
# which fails downstream URL routing. Backtick is not a valid trailing
# character in any URL form we accept.
_TRAILING_PUNCT = ".,;:!?)\\]\"'`"


def _detect_urls(text: str) -> list[str]:
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        if u:
            urls.append(u)
    return urls


# Phase 6a discriminator: PDF goes through the SDK Read tool's
# multimodal payload (Option C); every other whitelisted format is
# pre-extracted via ``assistant.files.extract`` (Option B). If the
# in-container live RQ1 probe ever fails (model can't read PDFs over
# the OAuth-CLI auth path), flipping this to ``return False`` makes
# the dispatch fall through to ``EXTRACTORS["pdf"]`` (pypdf-uniform).
def _is_pdf_native_read(kind: str | None) -> bool:
    """Return True iff this kind should use the SDK Read tool.

    Single decision point for the hybrid Option C / Option B split.

    **Phase 6a live probe FAILED 2026-04-27 (CI run cefca88):**
    claude-opus-4-7 ignored the imperative "use Read" system-note and
    went straight to Bash (`which pdftotext`) which the allowlist hook
    denied. The model treats the SDK Read tool as not-for-PDF despite
    the multimodal contract. Flipped to ``return False`` — all PDFs
    now go through ``EXTRACTORS["pdf"]`` (pypdf, Option B uniform).
    No regressions: pypdf already shipped + tested in 6a fix-pack.
    """
    return False  # was: kind == "pdf"


def _classify_block(
    item: Any,
) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Classify an SDK message/block into ``(role, payload, text_out, block_type)``.

    Contract (B5 — Anthropic tools API):
      TextBlock      → role='assistant', block_type='text', text_out=item.text
      ThinkingBlock  → role='assistant', block_type='thinking'
      ToolUseBlock   → role='assistant', block_type='tool_use'
      ToolResultBlock→ role='user',      block_type='tool_result'
        ^^^ SDK streaming-input mode requires tool_result on USER envelope,
            not 'tool'. Storing with role='tool' silently drops on replay.
      ResultMessage  → role='result' (caller uses the payload to mark the
        turn complete; no DB row is written for it).

    Unknown block types map to ``(None, ...)`` and the caller skips them.
    """
    if isinstance(item, ResultMessage):
        usage = item.usage or {}
        meta = {
            "stop_reason": getattr(item, "stop_reason", None),
            "usage": usage,
            "cost_usd": item.total_cost_usd,
            "duration_ms": getattr(item, "duration_ms", None),
            "num_turns": getattr(item, "num_turns", None),
            "sdk_session_id": getattr(item, "session_id", None),
        }
        return ("result", meta, None, None)
    if isinstance(item, TextBlock):
        return (
            "assistant",
            {"type": "text", "text": item.text},
            item.text,
            "text",
        )
    if isinstance(item, ThinkingBlock):
        return (
            "assistant",
            {
                "type": "thinking",
                "thinking": item.thinking,
                "signature": item.signature,
            },
            None,
            "thinking",
        )
    if isinstance(item, ToolUseBlock):
        return (
            "assistant",
            {
                "type": "tool_use",
                "id": item.id,
                "name": item.name,
                "input": item.input,
            },
            None,
            "tool_use",
        )
    if isinstance(item, ToolResultBlock):
        return (
            # B5: role='user' (NOT 'tool') — SDK requires ToolResultBlock on
            # a user envelope per the Anthropic tools API contract.
            "user",
            {
                "type": "tool_result",
                "tool_use_id": item.tool_use_id,
                "content": item.content,
                "is_error": item.is_error,
            },
            None,
            "tool_result",
        )
    return (None, {}, None, None)


class ClaudeHandler:
    """Orchestrates a single turn:
    start_turn → store user row → bridge.ask → persist every block →
    on ResultMessage mark turn complete; otherwise interrupt on ``finally``.
    """

    def __init__(
        self,
        settings: Settings,
        conv: ConversationStore,
        bridge: ClaudeBridge,
    ) -> None:
        self._settings = settings
        self._conv = conv
        self._bridge = bridge
        # CR-1 (phase 5, NEW): serialise concurrent turns on the same
        # chat_id. Without this, an owner turn and a scheduler-injected
        # turn both targeting ``OWNER_CHAT_ID`` would interleave block
        # writes into ``conversations`` and double-bill Claude for
        # overlapping history reads.
        #
        # RQ0 confirmed absent in phase-4 source. Single-owner deployment
        # bounds the dict to one entry; if multi-chat support is added
        # later, switch to a ``weakref.WeakValueDictionary`` or TTL.
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._locks_mutex = asyncio.Lock()

    async def _lock_for(self, chat_id: int) -> asyncio.Lock:
        """Return the lock for ``chat_id``, allocating lazily on cold path.

        Double-checked lookup so the hot path never acquires
        ``_locks_mutex``.
        """
        lk = self._chat_locks.get(chat_id)
        if lk is not None:
            return lk
        async with self._locks_mutex:
            lk = self._chat_locks.get(chat_id)
            if lk is None:
                lk = asyncio.Lock()
                self._chat_locks[chat_id] = lk
        return lk

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        lock = await self._lock_for(msg.chat_id)
        async with lock:
            await self._handle_locked(msg, emit)

    async def _handle_locked(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._conv.start_turn(msg.chat_id)
        log.info(
            "turn_started",
            turn_id=turn_id,
            chat_id=msg.chat_id,
            message_id=msg.message_id,
            origin=msg.origin,
            has_attachment=msg.attachment is not None,
        )

        # Phase 6a invariant: the three attachment fields move together.
        # Either all three are populated (Telegram doc upload) or all
        # three are None (text-only / scheduler-origin turn).
        if msg.attachment is not None:
            assert msg.attachment_kind is not None, (
                "attachment_kind must be set when attachment is set"
            )
            assert msg.attachment_filename is not None, (
                "attachment_filename must be set when attachment is set"
            )
            # Defensive path-containment guard (spec §I.194). Today the
            # adapter constructs ``<uploads>/<uuid>__<safe_stem>.<ext>``
            # — UUID prefix + sanitised stem makes escape impossible.
            # The explicit ``is_relative_to`` check exists so that any
            # future regression in adapter sanitisation (or a new caller
            # that synthesises ``IncomingMessage`` with an attacker-
            # controlled path) is caught here, before the file is read,
            # extracted, or handed to the SDK.
            uploads_root = self._settings.uploads_dir.resolve()
            try:
                tmp_resolved = msg.attachment.resolve()
            except OSError as resolve_exc:
                log.warning(
                    "attachment_path_resolve_failed",
                    path=str(msg.attachment),
                    error=repr(resolve_exc),
                )
                await emit("(внутренняя ошибка: не удалось проверить путь файла)")
                await self._conv.complete_turn(
                    turn_id,
                    meta={"stop_reason": "attachment_path_invalid", "usage": {}},
                )
                return
            if not tmp_resolved.is_relative_to(uploads_root):
                log.error(
                    "attachment_path_escape_rejected",
                    path=str(msg.attachment),
                    uploads_root=str(uploads_root),
                )
                await emit("(внутренняя ошибка: путь файла вне uploads dir)")
                await self._conv.complete_turn(
                    turn_id,
                    meta={"stop_reason": "attachment_path_invalid", "usage": {}},
                )
                return

        # User row: ORIGINAL caption (no extracted bytes leak into
        # persisted history; long-term replay sees a small marker only).
        # Phase 6a: append a `[file: NAME]` forensics marker so post-mortem
        # of ``conversations`` rows shows what was attached. The actual
        # tmp file is gone after this turn — devil M3: the marker is
        # inert at SDK replay (model sees plain text in history; no Read
        # call attempted because the path is invalid).
        persist_text = msg.text
        if msg.attachment is not None:
            persist_text = f"{msg.text}\n[file: {msg.attachment_filename}]"
        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": persist_text}],
            block_type="text",
        )
        history = await self._conv.load_recent(msg.chat_id, self._settings.claude.history_limit)
        # The current turn is still 'pending' so load_recent's 'complete'
        # filter excludes it — we won't replay our own user row to the model.

        # Phase 6a attachment branch:
        # - PDF (Option C): system-note appended; model uses Read tool
        #   directly. No pre-extract.
        # - DOCX/XLSX/TXT/MD (Option B): pre-extract via assistant.files
        #   .extract; injected into the SDK envelope. Failure → quarantine
        #   + Russian reply, turn marked complete with synthetic meta.
        user_text_for_sdk = msg.text
        attachment_notes: list[str] = []
        extraction_error: ExtractionError | None = None
        if msg.attachment is not None:
            kind = msg.attachment_kind
            assert kind is not None
            if _is_pdf_native_read(kind):
                # Option C: tell the model the file is on disk; the
                # Read tool propagates the multimodal payload over the
                # OAuth-CLI auth path.
                attachment_notes.append(
                    f"the user attached a PDF at path={msg.attachment}, "
                    f"named {msg.attachment_filename}; "
                    f"use Read(file_path={msg.attachment}) "
                    "to inspect it before answering."
                )
            else:
                try:
                    extracted, _char_count = EXTRACTORS[kind](msg.attachment)
                except ExtractionError as exc:
                    extraction_error = exc
                    extracted = ""
                if extraction_error is None:
                    # Defensive total-cap. The XLSX extractor honours
                    # the cap on a clean sheet boundary; this final
                    # substring guard handles the edge case where a
                    # single sheet exceeds the cap on its own (~6700
                    # capped rows by 30 cells by 1 char) and the DOCX
                    # extractor which has no per-doc cap of its own.
                    if len(extracted) > POST_EXTRACT_CHAR_CAP:
                        extracted = (
                            extracted[:POST_EXTRACT_CHAR_CAP]
                            + f"\n[…truncated at {POST_EXTRACT_CHAR_CAP} chars]"
                        )
                    user_text_for_sdk = (
                        f"{msg.text}\n\n"
                        f"[attached: {msg.attachment_filename}]\n\n"
                        f"{extracted}"
                    )

        # Phase 3: URL detector enriches the envelope sent to the SDK
        # without touching the persisted user row. If the owner pasted a
        # URL we tell the model that the installer @tool is a reasonable
        # first call; otherwise the envelope is unchanged.
        urls = _detect_urls(msg.text)
        url_hint = (
            (
                "the user's message contains URL(s) "
                f"{urls[:3]!r}. If one looks like a GitHub skill bundle, "
                "consider calling `mcp__installer__skill_preview(url=...)` to "
                "fetch a preview before asking the user to confirm install. "
                "Otherwise treat the URL as reference content."
            )
            if urls
            else None
        )

        # Phase 5: scheduler-origin turns get a directive note so the
        # model answers proactively without waiting on the owner.
        scheduler_note: str | None = None
        if msg.origin == "scheduler":
            trig_id = (msg.meta or {}).get("trigger_id")
            scheduler_note = (
                f"autonomous turn from scheduler id={trig_id}; "
                "owner is not active right now; answer proactively and "
                "concisely, do not ask clarifying questions"
            )

        notes: list[str] = list(attachment_notes)
        if scheduler_note is not None:
            notes.append(scheduler_note)
        if url_hint is not None:
            notes.append(url_hint)
        system_notes: list[str] | None = notes or None

        # Phase 6a: extraction failure short-circuits the bridge call.
        # Quarantine the file, reply in Russian, mark turn complete.
        if extraction_error is not None:
            try:
                await self._handle_extraction_failure(
                    msg, turn_id, extraction_error, emit
                )
            finally:
                # In the extraction-failure branch the file was renamed
                # into ``.failed/`` by ``_handle_extraction_failure`` —
                # the unlink below is a no-op (idempotent). Belt-and-
                # suspenders for any post-rename failure.
                if msg.attachment is not None:
                    with contextlib.suppress(OSError):
                        msg.attachment.unlink(missing_ok=True)
            return

        completed = False
        last_meta: dict[str, Any] | None = None
        try:
            async for item in self._bridge.ask(
                msg.chat_id,
                user_text_for_sdk,
                history,
                system_notes=system_notes,
            ):
                role, payload, text_out, block_type = _classify_block(item)
                if role == "result":
                    # Fix C (incident S13): accumulate last meta; complete
                    # only after the generator closes cleanly. With Fix A
                    # lifted from bridge.ask, the SDK may emit multiple
                    # ``ResultMessage`` instances per ``query()`` (e.g. when
                    # stream_input carries > 1 pending prompt or the model
                    # iterates via tool_use). Completing on the first one
                    # would race against subsequent block persistence.
                    last_meta = payload
                    continue
                if role is None:
                    continue
                assert block_type is not None
                await self._conv.append(
                    msg.chat_id,
                    turn_id,
                    role,
                    [payload],
                    block_type=block_type,
                )
                if text_out:
                    await emit(text_out)
            # After async-for exits cleanly, mark complete once.
            if last_meta is not None:
                await self._conv.complete_turn(turn_id, meta=last_meta)
                completed = True
                log.info(
                    "turn_complete",
                    turn_id=turn_id,
                    cost_usd=last_meta.get("cost_usd"),
                )
        except ClaudeBridgeError as exc:
            log.warning("bridge_error", turn_id=turn_id, error=str(exc))
            # Fix 3 / devil C1: on scheduler-origin turns we must
            # propagate the error so the dispatcher can
            # ``revert_to_pending`` and eventually dead-letter.
            # Swallowing the error inline makes attempts=0 forever — the
            # dead-letter threshold is unreachable for every Claude-API
            # failure on the scheduler path. User-origin turns keep the
            # legacy apology-chunk behaviour: the owner expects an
            # inline error reply, not silence or a retry. The ``finally``
            # block below interrupts the pending turn in either branch.
            if msg.origin == "scheduler":
                raise
            await emit(f"\n\n(ошибка: {exc})")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
            # Phase 6a: tmp cleanup ALWAYS — bridge success, bridge
            # error, OR mid-stream cancellation. The ``contextlib
            # .suppress`` keeps a flaky unlink from masking a more
            # interesting exception in the same finally.
            if msg.attachment is not None:
                try:
                    msg.attachment.unlink(missing_ok=True)
                except OSError as unlink_exc:
                    log.warning(
                        "attachment_unlink_failed",
                        path=str(msg.attachment),
                        error=repr(unlink_exc),
                    )

    async def _handle_extraction_failure(
        self,
        msg: IncomingMessage,
        turn_id: str,
        exc: ExtractionError,
        emit: Emit,
    ) -> None:
        """Quarantine + Russian reply + mark turn complete.

        Quarantine path: ``<uploads_dir>/.failed/<orig_tmp_filename>``.
        The tmp filename already embeds a UUID + sanitised original
        stem, so collisions in ``.failed/`` are bounded. Boot-sweep
        prunes ``.failed/`` entries older than 7 days.

        The turn is marked ``complete`` with a synthetic meta — no SDK
        call was issued, so there is no usage to bill. Leaving the turn
        ``pending`` would trip ``cleanup_orphan_pending_turns`` on the
        next daemon boot.
        """
        assert msg.attachment is not None
        quarantine_dir = self._settings.uploads_dir / ".failed"
        target = quarantine_dir / msg.attachment.name
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            msg.attachment.rename(target)
            log.info(
                "extraction_failed_quarantined",
                turn_id=turn_id,
                path=str(target),
                reason=str(exc),
            )
        except OSError as rename_exc:
            # Devil M-W2-5: ``rename`` can fail (cross-device move,
            # permissions glitch, target collision on a non-POSIX
            # filesystem). Without a fallback, the outer ``finally``
            # in ``_handle_locked`` then ``unlink``s ``msg.attachment``
            # → forensic evidence destroyed.
            #
            # Log everything BEFORE we try to recover, so even if both
            # the rename and the fallback fail, the structured log
            # carries full context for owner debugging.
            log.exception(
                "quarantine_rename_failed",
                turn_id=turn_id,
                tmp_path=str(msg.attachment),
                quarantine_target=str(target),
                error=str(rename_exc),
            )
            # Copy fallback: if we can ``copy2`` the file into
            # ``.failed/`` we preserve the evidence even though the
            # outer ``finally`` will unlink the source.
            try:
                shutil.copy2(msg.attachment, target)
                log.info(
                    "quarantine_copy_fallback_ok",
                    turn_id=turn_id,
                    path=str(target),
                )
            except OSError:
                log.exception(
                    "quarantine_copy_fallback_failed",
                    turn_id=turn_id,
                    tmp_path=str(msg.attachment),
                    quarantine_target=str(target),
                )
            # Deliberately swallow ``rename_exc`` — propagating it
            # would short-circuit the Russian reply + ``complete_turn``
            # below, leaving the user without feedback and the turn
            # stuck ``pending``.

        reply = f"не смог прочитать файл: {exc}"
        if "encrypted" in str(exc).lower():
            reply = "файл зашифрован — пришли расшифрованный"
        await emit(reply)

        # Mark turn complete with synthetic meta — no SDK roundtrip.
        await self._conv.complete_turn(
            turn_id,
            meta={"stop_reason": "extraction_error", "usage": {}},
        )
