"""Phase 6b — bridge.ask multimodal envelope tests.

Asserts the SDK envelope shape when ``image_blocks`` is supplied:

- ``content`` becomes a ``list[dict]`` with image blocks BEFORE text;
- text block carries ``user_text`` (with system-note suffix when
  applicable);
- plain-string path is preserved when ``image_blocks=None``.

We monkeypatch ``_safe_query`` (the only network surface) to capture
the streaming-input envelopes the SDK would receive.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from assistant.bridge import claude as claude_mod
from assistant.bridge.claude import ClaudeBridge
from assistant.config import ClaudeSettings, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="x" * 20,
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=30, max_concurrent=1, history_limit=5),
    )


class _CaptureSafeQuery:
    """Coroutine-style replacement for ``_safe_query``.

    Records the prompt envelopes the bridge sends, then yields a single
    ``ResultMessage`` so the bridge call completes cleanly.
    """

    def __init__(self) -> None:
        self.envelopes: list[dict[str, Any]] = []

    def __call__(self, *, prompt: Any, options: Any) -> AsyncIterator[Any]:
        del options
        recorded = self.envelopes

        async def _gen() -> AsyncIterator[Any]:
            async for env in prompt:
                recorded.append(env)
            from claude_agent_sdk import ResultMessage

            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.0,
                usage={"input_tokens": 1, "output_tokens": 1},
                stop_reason="end_turn",
            )

        return _gen()


@pytest.fixture
def bridge_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ClaudeBridge, _CaptureSafeQuery]:
    settings = _settings(tmp_path)
    bridge = ClaudeBridge(settings)
    cap = _CaptureSafeQuery()
    monkeypatch.setattr(claude_mod, "_safe_query", cap)
    # Stub system-prompt rendering; otherwise it tries to read the
    # real system_prompt.md.
    monkeypatch.setattr(
        ClaudeBridge,
        "_render_system_prompt",
        lambda self: "stub-system-prompt",
    )
    return bridge, cap


async def test_image_blocks_become_list_dict_content_before_text(
    bridge_factory: tuple[ClaudeBridge, _CaptureSafeQuery],
) -> None:
    bridge, cap = bridge_factory
    image_blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "AAAA",
            },
        }
    ]
    out: list[Any] = []
    async for item in bridge.ask(
        chat_id=42,
        user_text="что на фото?",
        history=[],
        image_blocks=image_blocks,
    ):
        out.append(item)

    # Last envelope is the live user turn (history-prelude was empty).
    user_env = cap.envelopes[-1]
    assert user_env["type"] == "user"
    content = user_env["message"]["content"]
    assert isinstance(content, list)
    # First block is the image; second is the text.
    assert content[0]["type"] == "image"
    assert content[0]["source"]["data"] == "AAAA"
    assert content[1]["type"] == "text"
    assert content[1]["text"] == "что на фото?"


async def test_image_blocks_with_system_notes_appended_to_text(
    bridge_factory: tuple[ClaudeBridge, _CaptureSafeQuery],
) -> None:
    bridge, cap = bridge_factory
    blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "BBBB",
            },
        }
    ]
    async for _ in bridge.ask(
        chat_id=1,
        user_text="опиши",
        history=[],
        system_notes=["scheduler"],
        image_blocks=blocks,
    ):
        pass

    user_env = cap.envelopes[-1]
    content = user_env["message"]["content"]
    assert isinstance(content, list)
    text_block = content[-1]
    assert text_block["type"] == "text"
    assert "[system-note: scheduler]" in text_block["text"]


async def test_no_image_blocks_keeps_plain_string_content(
    bridge_factory: tuple[ClaudeBridge, _CaptureSafeQuery],
) -> None:
    """Backward-compat: callers who omit ``image_blocks`` see the
    plain-string path the 6a code path already exercised.
    """
    bridge, cap = bridge_factory
    async for _ in bridge.ask(
        chat_id=1,
        user_text="ping",
        history=[],
    ):
        pass

    user_env = cap.envelopes[-1]
    assert isinstance(user_env["message"]["content"], str)
    assert user_env["message"]["content"] == "ping"


async def test_two_image_blocks_emitted_in_order(
    bridge_factory: tuple[ClaudeBridge, _CaptureSafeQuery],
) -> None:
    """Multi-photo media-group → both image blocks travel BEFORE text,
    in the order the handler passed them.
    """
    bridge, cap = bridge_factory
    blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "AAA",
            },
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": "BBB",
            },
        },
    ]
    async for _ in bridge.ask(
        chat_id=1,
        user_text="что общего?",
        history=[],
        image_blocks=blocks,
    ):
        pass

    user_env = cap.envelopes[-1]
    content = user_env["message"]["content"]
    assert content[0]["source"]["data"] == "AAA"
    assert content[1]["source"]["data"] == "BBB"
    assert content[2]["type"] == "text"
