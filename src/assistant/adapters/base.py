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
    # Phase 6c: audio kinds. ``ogg`` covers Telegram native voice (always
    # OGG/Opus); the rest are common attachment formats. They route
    # through ``assistant.services.transcription`` rather than the
    # ``EXTRACTORS`` dispatch table.
    "ogg", "mp3", "m4a", "wav", "opus",
]

# Phase 6b: image-only subset, used by the handler to branch into the
# vision pipeline before the extract dispatch.
IMAGE_KINDS: frozenset[str] = frozenset(
    {"jpg", "jpeg", "png", "webp", "heic", "heif"}
)

# Phase 6c: audio-only subset. The handler's audio branch fires when
# ``attachment_kind in AUDIO_KINDS`` OR when ``url_for_extraction`` is
# non-None (yt-dlp URL extraction). Routed BEFORE the image / extract
# branches in ``_handle_locked``.
AUDIO_KINDS: frozenset[str] = frozenset(
    {"ogg", "mp3", "m4a", "wav", "opus"}
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
#
# Phase 6 (research RQ8): ``"picker"`` is the SubagentRequestPicker
# origin. Picker dispatches go through ``ClaudeHandler`` so the
# resulting Task-tool delegation lands in ``conversations`` for owner
# forensics — but they SHOULD NOT trigger the scheduler-origin notice
# branch (no scheduler trigger row exists for them).
Origin = Literal["telegram", "scheduler", "picker"]


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
    # Phase 6c: audio metadata. Set by the Telegram adapter's voice /
    # audio / audio-document handlers. ``audio_duration`` comes from
    # ``message.voice.duration`` / ``message.audio.duration``; for the
    # ``audio-document`` route Telegram does not expose a duration so
    # the field is ``None`` (handler later reads it from the Whisper
    # response). ``audio_mime_type`` defaults to ``"audio/ogg"`` for
    # voice messages and is ``None`` when unknown.
    audio_duration: int | None = None
    audio_mime_type: str | None = None
    # Phase 6c: when set, the handler's audio branch routes through the
    # Mac sidecar's ``/extract`` endpoint (yt-dlp + Whisper) instead of
    # the standard ``/transcribe``. ``attachment`` MUST be ``None`` in
    # this case — there is no local file to upload.
    url_for_extraction: str | None = None

    def __post_init__(self) -> None:
        """Phase 6c F10 (fix-pack): enforce mutually-exclusive fields.

        ``attachment`` (a downloaded local file) and
        ``url_for_extraction`` (a remote URL fetched server-side via
        yt-dlp) cannot both be set on the same turn — they represent
        two different audio sources and the handler's audio branch
        would otherwise have to guess which to honour. Fail fast at
        construction to catch any future caller that violates the
        contract.

        Phase 6e (CRIT-3 close): scheduler-origin turns cannot carry
        audio. The audio branch dispatches a bg task and returns the
        per-chat lock; the scheduler dispatcher's ``revert_to_pending``
        / dead-letter machinery has no caller waiting on the bg task
        to finish, so a scheduler-origin audio turn would be
        permanently divorced from its trigger row. Reject at
        construction; F7 (phase 6c fix-pack scheduler-note injection
        for audio path) is explicitly reverted by this check.

        Fix-pack F9 (QA H-2): an audio ``attachment_kind`` must be
        accompanied by a concrete source — either ``attachment`` (local
        file) or ``url_for_extraction`` (remote URL). Constructing an
        audio kind with neither would crash deep inside
        ``_run_audio_job`` with a confusing AssertionError; surface the
        contract violation at the IncomingMessage boundary instead.

        Fix-pack F10: tighten the audio-origin check from
        ``!= 'scheduler'`` to ``== 'telegram'``. Picker-origin audio
        is just as broken (bg dispatch + no caller for the picker's
        ledger to wait on); only Telegram-originated audio turns are
        meaningful.
        """
        if self.attachment is not None and self.url_for_extraction is not None:
            raise AssertionError(
                "attachment and url_for_extraction are mutually exclusive"
            )

        is_audio_kind = (
            self.attachment_kind is not None
            and self.attachment_kind in AUDIO_KINDS
        )
        if (
            is_audio_kind
            and self.attachment is None
            and self.url_for_extraction is None
        ):
            raise AssertionError(
                "audio attachment_kind requires attachment OR "
                "url_for_extraction"
            )

        is_audio = is_audio_kind or self.url_for_extraction is not None
        if is_audio and self.origin != "telegram":
            raise AssertionError(
                "non-telegram audio/URL turns are not supported "
                f"(got origin={self.origin!r})"
            )


class MessengerAdapter(ABC):
    """Phase-1 ABC kept verbatim — phase-5 scheduler will inject outbound
    messages via the adapter (no handler in scope at that time).

    Phase 9 (W2-CRIT-1 Option A): :meth:`send_document` is a
    NON-abstract default impl that raises :class:`NotImplementedError`.
    Existing 5b/6e/8 test fixtures (``_FakeAdapter``, etc.) keep
    instantiating without override; HIGH-6 handler-resilience catches
    the runtime ``NotImplementedError`` for non-Telegram adapters.
    Future adapters MUST override per convention (documented in
    ``skills/render_doc/SKILL.md`` + reviewer checklist), NOT enforced
    at @abstractmethod.
    """

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        suggested_filename: str | None = None,
    ) -> None:
        """Phase 9 outbound document path. Default raises
        :class:`NotImplementedError` — subclasses MUST override to
        deliver the artefact (e.g.
        :meth:`assistant.adapters.telegram.TelegramAdapter.send_document`).
        """
        raise NotImplementedError(
            "adapter has no document-out path"
        )


class Handler(Protocol):
    """Phase-2 handler contract: receive an incoming message, emit text chunks.

    Phase 6e: ``emit_direct`` is an optional second emit channel used by
    the audio bg-task path. The lock-time ``emit`` keeps its phase-2
    chunks-then-flush semantics; ``emit_direct`` short-circuits to
    ``adapter.send_text(...)`` so a bg task firing seconds-to-minutes
    AFTER ``handle()`` returns can still deliver owner-visible output.
    Defaults to ``None`` for backward-compat with the scheduler
    dispatcher and existing Handler tests that pre-date 6e.

    Fix-pack F2: ``typing_lifecycle`` is an async-context-manager
    factory used by the bg audio path so the Telegram typing indicator
    stays visible across the entire transcribe + bridge.ask body
    (multiple minutes for a long-form voice). Defaults to ``None`` —
    the audio job uses a no-op lifecycle when nothing is supplied so
    inline tests and non-audio paths see no behaviour change.

    Phase 9 fix-pack F1: ``flush_text`` is an optional callable that
    joins-and-sends the adapter's accumulated chunks BEFORE the next
    ``send_document`` call. Required for the AC#19 ordering invariant
    (text₁ → doc₁ → text₂ → doc₂) under the chunks-buffering Telegram
    adapter; without it, all text accumulates and ships AFTER all
    artefacts. Defaults to ``None`` for adapters/tests that don't need
    interim flushing.
    """

    async def handle(
        self,
        msg: IncomingMessage,
        emit: Emit,
        emit_direct: Emit | None = None,
        typing_lifecycle: Any | None = None,
        flush_text: Callable[[], Awaitable[None]] | None = None,
    ) -> None: ...
