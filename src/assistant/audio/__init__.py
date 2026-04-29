"""Phase 6e — bg audio dispatch types.

Holds the :class:`AudioJob` payload that the handler hands off to the
daemon's bg-task pool. The actual job body lives on
:class:`assistant.handlers.message.ClaudeHandler` (``_run_audio_job``)
because it needs first-class access to ``ConversationStore`` /
``TranscriptionService`` / vault-save helpers that are already
encapsulated as handler methods. Daemon glue is the spawn site only;
the job class is public for clean type signatures and easy test fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from assistant.adapters.base import IncomingMessage

# ``Emit`` is the bg-time direct send channel. Re-aliased here to keep
# the audio module's public surface self-contained — adapters/base
# already exposes the same type as ``Emit`` for the lock-time path.
EmitDirect = Callable[[str], Awaitable[None]]

# Fix-pack F2: typing-lifecycle hook. The adapter constructs an async
# context-manager FACTORY that drives ``send_chat_action`` on a
# periodic loop while the bg task is in flight, so the owner sees the
# indicator from pre-lock-ack until the final result message lands.
# ``None`` keeps the old test-time behaviour (no typing).
TypingLifecycle = Callable[[], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def _noop_typing_lifecycle_impl() -> AsyncIterator[None]:
    """Default lifecycle factory used when the adapter doesn't provide one.

    Yields immediately. Tests / inline callers that don't need typing
    indicators rely on this so the bg task body remains structurally
    identical regardless of provenance.
    """
    yield


def _noop_typing_lifecycle() -> AbstractAsyncContextManager[None]:
    return _noop_typing_lifecycle_impl()


@dataclass(frozen=True)
class AudioJob:
    """Lock-to-bg handoff payload for one voice / audio / URL turn.

    Constructed inside the per-chat lock by ``_dispatch_audio_turn``
    (handlers/message.py); consumed in ``Daemon._bg_tasks`` by
    ``ClaudeHandler._run_audio_job``.

    Frozen because the dispatcher must NOT mutate it after handoff —
    the bg task owns every field for the duration of the job. The
    ``audio_bg_sem`` reference is the SAME semaphore object across
    every job for a daemon (see ``Daemon._audio_bg_sem``); jobs queue
    on it FIFO, mirroring the Mac whisper-server's hard
    ``Semaphore(1)`` so client-side parallelism never overruns the
    sidecar.

    Fix-pack F2: ``typing_lifecycle`` is an async-context-manager factory
    the bg task wraps around the entire transcribe + bridge.ask body so
    Telegram shows the typing indicator throughout. Defaults to a
    no-op for tests; the production adapter passes a real periodic
    ``send_chat_action`` loop.
    """

    chat_id: int
    turn_id: str
    msg: IncomingMessage
    emit_direct: EmitDirect
    audio_bg_sem: asyncio.Semaphore
    typing_lifecycle: TypingLifecycle = field(
        default=_noop_typing_lifecycle
    )
