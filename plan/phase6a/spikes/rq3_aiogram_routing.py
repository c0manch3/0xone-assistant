"""RQ3 — aiogram F.document coexists with F.text + catch-all.

Verifies the planned 3-handler dispatch order (text → document → catch-all)
without touching the network. Uses aiogram's ``Dispatcher.feed_update``
plus synthetic ``Update`` payloads to drive each handler in-process.

PASS: each synthetic update fires ONLY its intended handler.
FAIL: investigate ``message.register`` ordering.

Run:
    /tmp/.spike6a-venv/bin/python plan/phase6a/spikes/rq3_aiogram_routing.py
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Chat, Document, Message, Update, User

OWNER_CHAT_ID = 12345
BOT_ID = 7777

# Capture which handlers fired.
fired: list[str] = []


async def on_text(message: Message) -> None:
    fired.append(f"text:{message.text!r}")


async def on_document(message: Message) -> None:
    assert message.document is not None
    fired.append(
        f"document:{message.document.file_name!r}"
        f":caption={message.caption!r}"
    )


async def on_catchall(message: Message) -> None:
    # ``message.content_type`` is an enum in aiogram 3.x; coerce to ``.value``.
    ct = getattr(message.content_type, "value", message.content_type)
    fired.append(f"catchall:{ct}")


def make_dp() -> Dispatcher:
    dp = Dispatcher()
    dp.message.filter(F.chat.id == OWNER_CHAT_ID)
    # Order matches the plan: text → document → catch-all.
    dp.message.register(on_text, F.text)
    dp.message.register(on_document, F.document)
    dp.message.register(on_catchall)
    return dp


def mk_user() -> User:
    return User(id=42, is_bot=False, first_name="Owner")


def mk_chat() -> Chat:
    return Chat(id=OWNER_CHAT_ID, type="private")


def mk_doc(file_name: str, mime: str, size: int = 1024) -> Document:
    return Document(
        file_id=f"file_{file_name}",
        file_unique_id=f"u_{file_name}",
        file_name=file_name,
        mime_type=mime,
        file_size=size,
    )


def mk_text_update(uid: int, text: str) -> Update:
    msg = Message(
        message_id=uid,
        date=0,  # epoch placeholder — aiogram parses but we don't care
        chat=mk_chat(),
        from_user=mk_user(),
        text=text,
    )
    return Update(update_id=uid, message=msg)


def mk_doc_update(
    uid: int, file_name: str, mime: str, *, caption: str | None = None
) -> Update:
    msg = Message(
        message_id=uid,
        date=0,
        chat=mk_chat(),
        from_user=mk_user(),
        document=mk_doc(file_name, mime),
        caption=caption,
    )
    return Update(update_id=uid, message=msg)


def mk_voice_update(uid: int) -> Update:
    """Voice arrives without text/document — should hit catch-all."""
    from aiogram.types import Voice

    msg = Message(
        message_id=uid,
        date=0,
        chat=mk_chat(),
        from_user=mk_user(),
        voice=Voice(
            file_id="voice1",
            file_unique_id="u_voice1",
            duration=3,
            mime_type="audio/ogg",
        ),
    )
    return Update(update_id=uid, message=msg)


async def main() -> int:
    dp = make_dp()
    # A real Bot is required for feed_update — token validation is offline
    # if format is plausible. We never call any HTTP API.
    bot = Bot(
        token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        default=DefaultBotProperties(parse_mode=None),
    )

    # Cases.
    cases: list[tuple[str, Update, list[str]]] = [
        ("plain text", mk_text_update(1, "hello"), ["text:'hello'"]),
        (
            "PDF no caption",
            mk_doc_update(2, "report.pdf", "application/pdf"),
            ["document:'report.pdf':caption=None"],
        ),
        (
            "DOCX with caption",
            mk_doc_update(
                3, "doc.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                caption="summarize this",
            ),
            ["document:'doc.docx':caption='summarize this'"],
        ),
        ("voice", mk_voice_update(4), ["catchall:voice"]),
    ]

    failed = 0
    for name, update, expected in cases:
        fired.clear()
        await dp.feed_update(bot, update)
        ok = fired == expected
        marker = "PASS" if ok else "FAIL"
        print(f"  [{marker}] {name}: fired={fired}  expected={expected}")
        if not ok:
            failed += 1

    await bot.session.close()
    print()
    print(f"VERDICT: {'PASS' if failed == 0 else 'FAIL'} ({failed} failed of {len(cases)})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
