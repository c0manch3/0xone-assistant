from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    text: str


Emit = Callable[[str], Awaitable[None]]


class Handler(Protocol):
    """Contract: `handle` drives one turn and pushes text fragments via `emit`.

    The adapter decides how to deliver the accumulated text (Telegram: split &
    send; scheduler: push to owner chat). The handler never calls adapter
    methods directly — this keeps phase-5 scheduler pluggable.
    """

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None: ...


class MessengerAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...
