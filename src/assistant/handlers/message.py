from __future__ import annotations

from assistant.adapters.base import IncomingMessage, MessengerAdapter
from assistant.state.conversations import ConversationStore


class EchoHandler:
    def __init__(self, conv: ConversationStore, adapter: MessengerAdapter) -> None:
        self._conv = conv
        self._adapter = adapter

    async def handle(self, msg: IncomingMessage) -> None:
        turn = ConversationStore.new_turn_id()
        await self._conv.append(
            msg.chat_id,
            turn,
            "user",
            [{"type": "text", "text": msg.text}],
            meta={"message_id": msg.message_id},
        )
        reply = f"echo: {msg.text}"
        await self._conv.append(
            msg.chat_id,
            turn,
            "assistant",
            [{"type": "text", "text": reply}],
        )
        await self._adapter.send_text(msg.chat_id, reply)
