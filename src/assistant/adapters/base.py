from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

# Phase 6a: whitelisted attachment kinds. Mirrors the suffix whitelist
# enforced by the Telegram adapter; adding a new kind requires a matching
# extractor in ``assistant.files.extract`` and adapter-side acceptance.
#
# Phase 6b: image kinds added (``jpg``/``jpeg``/``png``/``webp``/``heic``).
# F10 fix-pack: ``heif`` alias added — Apple iOS sometimes writes the
# ``.heif`` suffix instead of ``.heic`` while the underlying byte stream
# is identical (HEIF is the format, HEIC is just one of its profile
# brands).
# Image kinds bypass ``EXTRACTORS`` and route through the new
# ``assistant.files.vision`` pipeline (multimodal envelope) — they do
# NOT have entries in the ``EXTRACTORS`` dispatch table.
AttachmentKind = Literal[
    "pdf", "docx", "txt", "md", "xlsx",
    "jpg", "jpeg", "png", "webp", "heic", "heif",
]

# Phase 6b: image-only subset, used by the handler to branch into the
# vision pipeline before the extract dispatch.
IMAGE_KINDS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "webp", "heic", "heif"}
)

# ---------------------------------------------------------------------------
# Emit callback signature used by phase-2 ``ClaudeHandler``. The adapter
# passes a concrete emit function in; the handler calls it with each chunk
# of user-visible text as the model streams.
# ---------------------------------------------------------------------------
Emit = Callable[[str], Awaitable[None]]


# Phase 5: the handler now receives messages from two sources — the live
# Telegram adapter AND the scheduler dispatcher. ``origin`` lets the
# handler branch on provenance without sniffing ``chat_id`` or ``meta``.
Origin = Literal["telegram", "scheduler"]


@dataclass(frozen=True)
class IncomingMessage:
    """Normalized inbound message shared by every messenger adapter.

    ``message_id`` is retained from phase 1 (B4 fix): handler logs use it
    for correlation with Telegram's side of the chat, since the SDK's
    ``sdk_session_id`` is ephemeral (R10).

    Phase 5 additions (RQ1 verified safe — every construction site uses
    keyword args):
      - ``origin`` — "telegram" (owner turn) or "scheduler" (autonomous).
      - ``meta`` — optional provenance bag (trigger_id, schedule_id,
        scheduler_nonce, scheduled_for_utc). ``None`` default, NOT ``{}``:
        frozen-dataclass mutable-default caveat.
    """

    chat_id: int
    message_id: int
    text: str
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None
    # Phase 6a: file-attachment fields (END-of-class; positional-call
    # safety verified by phase-5 RQ1 — every construction site uses
    # kwargs but appending keeps positional callers stable). Invariant:
    # all three set, or all three None. Handler asserts.
    attachment: Path | None = None
    attachment_kind: AttachmentKind | None = None
    attachment_filename: str | None = None
    # Phase 6b: media-group multi-photo path. When the owner sends an
    # album, the adapter aggregates 1..MAX_PHOTOS_PER_TURN paths in a
    # single ``IncomingMessage``. The handler reads ``attachment_paths``
    # if it is non-None (vision branch) and falls back to ``attachment``
    # otherwise (single-photo or 6a document path).
    #
    # Invariant when ``attachment_paths`` is set: ``attachment`` points
    # to ``attachment_paths[0]`` (so 6a-style guards on ``attachment``
    # still cover the first path), ``attachment_kind`` ∈ ``IMAGE_KINDS``,
    # and ``attachment_filename`` is the synthesised name of the first
    # photo. Single-photo + non-image-document construction stays
    # unchanged from 6a.
    attachment_paths: list[Path] | None = None


class MessengerAdapter(ABC):
    """Phase-1 ABC kept verbatim — phase-5 scheduler will inject outbound
    messages via the adapter (no handler in scope at that time).
    """

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...


class Handler(Protocol):
    """Phase-2 handler contract: receive an incoming message, emit text chunks."""

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None: ...
