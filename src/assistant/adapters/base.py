from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

# Future-proof: phase-5 scheduler injects "scheduler"-origin messages
# without a real Telegram message_id. Keeping the type tight via Literal
# means mypy catches typos at the call site.
Origin = Literal["telegram", "scheduler"]

# Phase 7: the set of Telegram media kinds the adapter can ingest.
# Kept as a Literal so mypy flags typos at the callsite (handler + adapter
# both switch on this); runtime rejection of an unknown string lives in
# the adapter's download path, not in the dataclass itself (a frozen
# `@dataclass` cannot enforce Literal at runtime without an __init__
# hook, and the adapter is the only place an "unknown kind" can be
# synthesised).
MediaKind = Literal["voice", "photo", "document", "audio", "video_note"]


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """A single media artefact ingested from the messenger.

    Immutable (frozen) + memory-tight (slots) so the handler may stash
    a tuple of these on `IncomingMessage` without surprising cost: in
    practice Telegram media_group updates fan out to <=10 items per
    incoming envelope. All optional fields default to `None` because
    aiogram's `File.file_size`, `Voice.duration`, photo `width`/`height`
    and `Document.file_name` are each independently nullable (see phase-7
    Spike 6 for the empirical map).

    `local_path` is the resolved absolute path under `<data_dir>/media/
    inbox/` AFTER streaming download completes -- never the raw
    `file_path` returned by Telegram's `getFile` (which is a relative
    server-side handle, not a local FS path).
    """

    kind: MediaKind
    local_path: Path
    mime_type: str | None = None
    file_size: int | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    filename_original: str | None = None
    telegram_file_id: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    text: str
    message_id: int | None = None
    origin: Origin = "telegram"
    # Phase 5: scheduler carries `{"trigger_id": int, "schedule_id": int}` here.
    # Telegram-origin messages leave this None (backwards-compatible).
    meta: dict[str, Any] | None = None
    # Phase 7: ordered tuple of media attachments (photos/voice/documents/
    # audio/video_notes). `None` (not `()`) preserves byte-for-byte
    # backward-compat with phase-5/6 call sites that construct
    # `IncomingMessage` without the kwarg. Adapter is responsible for
    # deduping by `local_path` before emitting (invariant I-7.6), so the
    # handler can iterate unconditionally.
    attachments: tuple[MediaAttachment, ...] | None = None


Emit = Callable[[str], Awaitable[None]]


class Handler(Protocol):
    """Contract: `handle` drives one turn and pushes text fragments via `emit`.

    The adapter decides how to deliver the accumulated text (Telegram: split &
    send; scheduler: push to owner chat). The handler never calls adapter
    methods directly -- this keeps phase-5 scheduler pluggable.
    """

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None: ...


class MessengerAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...

    # Phase 7: media out-of-band delivery. `dispatch_reply` (adapters/
    # dispatch_reply.py, Wave B commit 6) resolves an artefact path in
    # the assistant's reply text and routes it through one of these
    # three methods based on file extension. Implementations MUST be
    # idempotent-friendly (caller wraps in `_DedupLedger.mark_and_check`)
    # and MUST NOT swallow `FileNotFoundError` / `PermissionError` /
    # `OSError` on the source `path` -- those are surfaced to
    # `dispatch_reply` which logs and continues with the remaining
    # artefacts + cleaned text (invariant L-20).
    @abstractmethod
    async def send_photo(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...

    @abstractmethod
    async def send_document(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...

    @abstractmethod
    async def send_audio(
        self, chat_id: int, path: Path, *, caption: str | None = None
    ) -> None: ...
