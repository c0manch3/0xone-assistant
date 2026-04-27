"""RQ0 — multimodal envelope spike for phase 6b vision.

Goal: verify whether claude-agent-sdk + the bundled `claude` ELF (the same
binary the daemon uses) reliably propagates an image content-block from
streaming-input mode through to the model.

Run inside the live VPS container so it exercises the EXACT SDK + CLI
(and OAuth session) path the bot uses:

    docker exec 0xone-assistant python /tmp/probe.py

Outputs a single PASS/FAIL verdict + the model's text response. If model
echoes content awareness ("I see a red pixel" / "the image is solid red")
=> envelope propagation works. If it says "I cannot see images" or
"please share the image", or the SDK emits Unknown message-type warnings
=> the streaming-input list[dict] form does NOT propagate images, and
the spec must pivot (Альт A OCR / Альт B Mac sidecar).
"""

from __future__ import annotations

import asyncio
import base64

from claude_agent_sdk import (  # type: ignore[import-not-found]
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

# 1×1 solid-red JPEG. Smallest valid JPEG; sufficient for envelope
# propagation test. If model says "red" or "single pixel" — the image
# reached it. If model says "no image" — the envelope was dropped.
_RED_PIXEL_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAAB"
    "AAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAyKADAAQAAAABAAAAyAAA"
    "AAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZ"
    "jwCyBOmACZjs+EJ+/8AAEQgAyADIAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAA"
    "AAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFB"
    "BhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNE"
    "RUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqi"
    "o6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz"
    "9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIB"
    "AgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy"
    "0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpz"
    "dHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUD"
    "AwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8P"
    "Dw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQADf/aAAwDAQACEQMRAD8A+L6K"
    "KK/lM/38CiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAoo"
    "ry3xt41+x+Zo2jyf6Rys0yn/AFfqqn+96n+HoPm+7+l+FHhRnHGWcU8nyeneT1lJ"
    "/DTj1nN9EvvbtGKbaR+feJniZlXCmVTzXNZ2itIxXxTl0jFdW/uSu20k2Hjbxr9j"
    "8zRtHk/0jlZplP8Aq/VVP971P8PQfN93y3/hINf/AOglc/8Af5/8ax6K/wB3vDH6"
    "NPCfDWT0srWCp15LWdSrThKU5O137yfKtLRinZLu7yf+L/iH9IPifiDNamYvFzox"
    "ekadOcoxjFXstGuZ66yerfZWS//Q+L6KKK/lM/38CiiigAooooAKKKKACiiigAoo"
    "ooAKKKKACiiigAooooAKKKKACiivLfG3jX7H5mjaPJ/pHKzTKf8AV+qqf73qf4eg"
    "+b7v6X4UeFGccZZxTyfJ6d5PWUn8NOPWc30S+9u0YptpH594meJmVcKZVPNc1naK"
    "0jFfFOXSMV1b+5K7bSTYeNvGv2PzNG0eT/SOVmmU/wCr9VU/3vU/w9B833fFaKK/"
    "3+8FfBXJ+BsnjleVxvJ2dSo171SXd9ktVGKdoru3KT/xD8XfF3NeMc1lmOYytFXV"
    "Omn7tOPZd2/tS3k+ySSKKKK/Xz8sP//R+L6KKK/lM/38CiiigAooooAKKKKACiii"
    "gAooooAKKKKACiiigAooooAKKK8t8beNfsfmaNo8n+kcrNMp/wBX6qp/vep/h6D5"
    "vu/pfhR4UZxxlnFPJ8np3k9ZSfw049ZzfRL727Rim2kfn3iZ4mZVwplU81zWdorS"
    "MV8U5dIxXVv7krttJNh428a/Y/M0bR5P9I5WaZT/AKv1VT/e9T/D0Hzfd8Voor/f"
    "7wV8Fcn4GyeOV5XG8nZ1KjXvVJd32S1UYp2iu7cpP/EPxd8Xc14xzWWY5jK0VdU6"
    "afu049l3b+1LeT7JJIooor9fPywKKKKAP//S+L6KKK/lM/38CiiigAooooAKKKKA"
    "CiiigAooooAKKKKACiiigAoory3xt41+x+Zo2jyf6Rys0yn/AFfqqn+96n+HoPm+"
    "7+l+FHhRnHGWcU8nyeneT1lJ/DTj1nN9EvvbtGKbaR+feJniZlXCmVTzXNZ2itIx"
    "XxTl0jFdW/uSu20k2Hjbxr9j8zRtHk/0jlZplP8Aq/VVP971P8PQfN93xWiiv9/v"
    "BXwVyfgbJ45XlcbydnUqNe9Ul3fZLVRinaK7tyk/8Q/F3xdzXjHNZZjmMrRV1Tpp"
    "+7Tj2Xdv7Ut5Pskkiiiiv18/LAooooAKKKKAP//T+L6KKK/lM/38CiiigAooooAK"
    "KKKACiiigAooooAKKKKACiivLfG3jX7H5mjaPJ/pHKzTKf8AV+qqf73qf4eg+b7v"
    "6X4UeFGccZZxTyfJ6d5PWUn8NOPWc30S+9u0YptpH594meJmVcKZVPNc1naK0jFf"
    "FOXSMV1b+5K7bSTYeNvGv2PzNG0eT/SOVmmU/wCr9VU/3vU/w9B833fFaKK/3+8F"
    "fBXJ+BsnjleVxvJ2dSo171SXd9ktVGKdoru3KT/xD8XfF3NeMc1lmOYytFXVOmn7"
    "tOPZd2/tS3k+ySSKKKK/Xz8sCiiigAooooAKKKKAP//U+L6KKK/lM/38CiiigAoo"
    "ooAKKKKACiiigAooooAKKK8t8beNfsfmaNo8n+kcrNMp/wBX6qp/vep/h6D5vu/p"
    "fhR4UZxxlnFPJ8np3k9ZSfw049ZzfRL727Rim2kfn3iZ4mZVwplU81zWdorSMV8U"
    "5dIxXVv7krttJNh428a/Y/M0bR5P9I5WaZT/AKv1VT/e9T/D0Hzfd8Voor/f7wV8"
    "Fcn4GyeOV5XG8nZ1KjXvVJd32S1UYp2iu7cpP/EPxd8Xc14xzWWY5jK0VdU6afu0"
    "49l3b+1LeT7JJIooor9fPywKKKKACiiigAooooAKKKKAP//V+L6KKK/lM/38Ciii"
    "gAooooAKKKKACiiigAoory3xt41+x+Zo2jyf6Rys0yn/AFfqqn+96n+HoPm+7+l+"
    "FHhRnHGWcU8nyeneT1lJ/DTj1nN9EvvbtGKbaR+feJniZlXCmVTzXNZ2itIxXxTl"
    "0jFdW/uSu20k2Hjbxr9j8zRtHk/0jlZplP8Aq/VVP971P8PQfN93xWiiv9/vBXwV"
    "yfgbJ45XlcbydnUqNe9Ul3fZLVRinaK7tyk/8Q/F3xdzXjHNZZjmMrRV1Tpp+7Tj"
    "2Xdv7Ut5Pskkiiiiv18/LAooooAKKKKACiiigAooooAKKKKAP//W+L6KKK/lM/38"
    "CiiigAooooAKKKKACiivLfG3jX7H5mjaPJ/pHKzTKf8AV+qqf73qf4eg+b7v6X4U"
    "eFGccZZxTyfJ6d5PWUn8NOPWc30S+9u0YptpH594meJmVcKZVPNc1naK0jFfFOXS"
    "MV1b+5K7bSTYeNvGv2PzNG0eT/SOVmmU/wCr9VU/3vU/w9B833fFaKK/3+8FfBXJ"
    "+BsnjleVxvJ2dSo171SXd9ktVGKdoru3KT/xD8XfF3NeMc1lmOYytFXVOmn7tOPZ"
    "d2/tS3k+ySSKKKK/Xz8sCiiigAooooAKKKKACiiigAooooAKKKKAP//X+L6KKK/l"
    "M/38CiiigAooooAKKK8t8beNfsfmaNo8n+kcrNMp/wBX6qp/vep/h6D5vu/pfhR4"
    "UZxxlnFPJ8np3k9ZSfw049ZzfRL727Rim2kfn3iZ4mZVwplU81zWdorSMV8U5dIx"
    "XVv7krttJNh428a/Y/M0bR5P9I5WaZT/AKv1VT/e9T/D0Hzfd8Voor/f7wV8Fcn4"
    "GyeOV5XG8nZ1KjXvVJd32S1UYp2iu7cpP/EPxd8Xc14xzWWY5jK0VdU6afu049l3"
    "b+1LeT7JJIooor9fPywKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP//Q+L6K"
    "KK/lM/38CiiigAoory3xt41+x+Zo2jyf6Rys0yn/AFfqqn+96n+HoPm+7+l+FHhR"
    "nHGWcU8nyeneT1lJ/DTj1nN9EvvbtGKbaR+feJniZlXCmVTzXNZ2itIxXxTl0jFd"
    "W/uSu20k2Hjbxr9j8zRtHk/0jlZplP8Aq/VVP971P8PQfN93xWiiv9/vBXwVyfgb"
    "J45XlcbydnUqNe9Ul3fZLVRinaK7tyk/8Q/F3xdzXjHNZZjmMrRV1Tpp+7Tj2Xdv"
    "7Ut5Pskkiiiiv18/LAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP//R"
    "+L6KKK/lM/38CiivLfG3jX7H5mjaPJ/pHKzTKf8AV+qqf73qf4eg+b7v6X4UeFGc"
    "cZZxTyfJ6d5PWUn8NOPWc30S+9u0YptpH594meJmVcKZVPNc1naK0jFfFOXSMV1b"
    "+5K7bSTYeNvGv2PzNG0eT/SOVmmU/wCr9VU/3vU/w9B833fFaKK/3+8FfBXJ+Bsn"
    "jleVxvJ2dSo171SXd9ktVGKdoru3KT/xD8XfF3NeMc1lmOYytFXVOmn7tOPZd2/t"
    "S3k+ySSKKKK/Xz8sCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKA"
    "P//S+L6KK8t8beNfsfmaNo8n+kcrNMp/1fqqn+96n+HoPm+7+PeFHhRnHGWcU8ny"
    "eneT1lJ/DTj1nN9EvvbtGKbaR/t34meJmVcKZVPNc1naK0jFfFOXSMV1b+5K7bST"
    "YeNvGv2PzNG0eT/SOVmmU/6v1VT/AHvU/wAPQfN93xWiiv8Af7wV8Fcn4GyeOV5X"
    "G8nZ1KjXvVJd32S1UYp2iu7cpP8AxD8XfF3NeMc1lmOYytFXVOmn7tOPZd2/tS3k"
    "+ySSKKKK/Xz8sCiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igD/0/zd8beNfsfmaNo8n+kcrNMp/wBX6qp/vep/h6D5vu+K1seIf+Q/qX/XzN/6"
    "Gax6/wBffo0+GWT8NcJ4JZXStKvTp1ak3rKcpQUtXZaRu1GK0S83Jv2/pB+Iea8Q"
    "cT4t5jUvGjOdOnFaRjGMmtF3la8nu35JJFFFFfv5+IBRRRQAUUUUAFFFFABRRRQA"
    "UUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAf/Z"
)


async def _stream():
    """Minimal streaming-input prompt with one user message containing
    text + image content-blocks (the unverified `list[dict]` form per
    bridge/claude.py:241).
    """
    yield {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Опиши что ты видишь на этом изображении. "
                        "Если изображения нет, скажи 'НЕТ ИЗОБРАЖЕНИЯ'."
                    ),
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": _RED_PIXEL_JPEG_B64,
                    },
                },
            ],
        },
        "parent_tool_use_id": None,
        "session_id": "rq0-multimodal-spike",
    }


async def main() -> int:
    # Bare-bones options: NO MCP servers, NO hooks, NO skills — isolate
    # the envelope-propagation question from any project-side surface.
    opts = ClaudeAgentOptions(
        cwd="/app",
        max_turns=1,
        allowed_tools=[],  # NO tools — model must answer from envelope alone.
    )

    text_chunks: list[str] = []
    saw_assistant = False
    saw_result = False
    init_seen = False
    last_model: str | None = None

    print("=" * 70)
    print("RQ0 multimodal envelope probe — phase 6b vision")
    print("=" * 70)

    try:
        async with asyncio.timeout(60):
            async for message in query(prompt=_stream(), options=opts):
                msg_type = type(message).__name__
                print(f"[trace] {msg_type}")

                if isinstance(message, SystemMessage) and message.subtype == "init":
                    init_seen = True
                    print(f"  init.model={message.data.get('model')}")
                    continue

                if isinstance(message, AssistantMessage):
                    saw_assistant = True
                    last_model = getattr(message, "model", None) or last_model
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            text_chunks.append(block.text)
                            print(f"  text-block: {block.text[:200]}")
                        else:
                            print(f"  other-block: {type(block).__name__}")
                    continue

                if isinstance(message, ResultMessage):
                    saw_result = True
                    print(f"  result.stop_reason={getattr(message, 'stop_reason', None)}")
                    print(f"  result.cost_usd={message.total_cost_usd}")
                    print(f"  result.usage={message.usage}")
                    continue

    except TimeoutError:
        print("[FATAL] timeout 60s — SDK never reached ResultMessage.")
        return 2
    except Exception as exc:  # pragma: no cover — diagnostic
        print(f"[FATAL] exception: {type(exc).__name__}: {exc}")
        return 3

    print("=" * 70)
    full_text = " ".join(text_chunks).lower()

    print(f"init_seen={init_seen} assistant_seen={saw_assistant} result_seen={saw_result}")
    print(f"last_model={last_model}")
    print(f"full_response_chars={len(full_text)}")

    # Heuristic: did the model acknowledge image content?
    saw_image_signal = any(
        kw in full_text
        for kw in (
            "red", "красн", "пиксел", "pixel", "color", "цвет",
            "solid", "одноцветн", "single", "одиноч", "image", "изображен",
        )
    )
    saw_no_image_signal = any(
        kw in full_text
        for kw in (
            "нет изображения", "no image", "cannot see", "не вижу",
            "не могу прочитать", "не вижу изображ", "share the image",
        )
    )

    if not saw_result:
        print("VERDICT: FAIL — no ResultMessage; SDK swallowed/crashed.")
        return 4
    if saw_no_image_signal:
        print("VERDICT: FAIL — model explicitly says it does NOT see the image.")
        return 5
    if saw_image_signal:
        print("VERDICT: PASS — model acknowledged image content.")
        return 0
    print("VERDICT: AMBIGUOUS — no clear signal in either direction; review text above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
