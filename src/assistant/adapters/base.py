from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Emit callback signature used by phase-2 ``ClaudeHandler``. The adapter
# passes a concrete emit function in; the handler calls it with each chunk
# of user-visible text as the model streams.
# ---------------------------------------------------------------------------
Emit = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class IncomingMessage:
    """Normalized inbound message shared by every messenger adapter.

    ``message_id`` is retained from phase 1 (B4 fix): handler logs use it
    for correlation with Telegram's side of the chat, since the SDK's
    ``sdk_session_id`` is ephemeral (R10).
    """

    chat_id: int
    message_id: int
    text: str


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
