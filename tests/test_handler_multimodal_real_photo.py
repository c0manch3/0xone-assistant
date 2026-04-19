"""Phase 7 commit 13 — C-2 real-photo integration.

Fixture choice
--------------
`tests/fixtures/phase7/real_photo_3mb.jpg` is generated programmatically
via Pillow (see the sibling `_generate_real_photo.py` script). The image
is a 2048x1536 RGB photo-like scene: seeded Mersenne-Twister noise per
pixel, superimposed with a checkerboard and random diagonals to keep
high-frequency entropy above JPEG's DCT floor. Saved at quality=92 with
subsampling=0, the on-disk size lands around 5 MB — comfortably above
the 3 MB floor C-2 demands, below the 10 MB `photo_download_max_bytes`
cap, and small enough to live in-repo.

Null-padded fixtures are FORBIDDEN per C-2 (padded-COM JPEGs wire-
transfer at ~1.33x their base64 size thanks to HTTP/2 HPACK + gzip; a
real 5 MB photo wire-transfers at roughly 1:1, stressing the SDK's
multimodal path the way production will).

Why we didn't use a CC0 stock photo
-----------------------------------
Network fetches inside test collection are forbidden (reliability + CI
offline runs). The generator is deterministic (seeded PRNG) and
Pillow is already a hard dep via fpdf2, so generation is free.

Gating
------
The pure envelope-construction path is always exercised (no RUN_SDK_INT
flag). A second test, gated on `RUN_SDK_INT=1`, actually invokes the
SDK via `ClaudeBridge.ask` and asserts the multimodal turn reaches
`ResultMessage`. That test skips cleanly without the env flag or the
OAuth session.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

_FIXTURE = Path(__file__).parent / "fixtures" / "phase7" / "real_photo_3mb.jpg"
_MIN_BYTES = 3 * 1024 * 1024  # 3 MB C-2 floor.


def _require_fixture() -> Path:
    if not _FIXTURE.exists():
        pytest.skip(
            f"missing fixture {_FIXTURE}; generate via "
            "'uv run python tests/fixtures/phase7/_generate_real_photo.py'"
        )
    size = _FIXTURE.stat().st_size
    if size < _MIN_BYTES:
        pytest.skip(
            f"fixture {_FIXTURE} is {size} B, below 3 MB C-2 floor; "
            "regenerate"
        )
    return _FIXTURE


class _SpyBridge:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_image_blocks: list[dict[str, Any]] | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, user_text, history, system_notes
        self.last_image_blocks = (
            list(image_blocks) if image_blocks is not None else None
        )
        for item in self._items:
            yield item


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
        result="ok",
        uuid="u",
    )


async def test_real_photo_roundtrips_through_handler_envelope(tmp_path: Path) -> None:
    """Handler base64-encodes the real JPEG end-to-end without truncation."""
    fixture = _require_fixture()

    conn = await connect(tmp_path / "rp.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    # Settings with photo_max_inline_bytes raised ABOVE the fixture size
    # so the inline branch is taken end-to-end.
    from assistant.config import MediaSettings

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(photo_max_inline_bytes=fixture.stat().st_size + 1),  # type: ignore[arg-type]
    )

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="seen"),
            _result(),
        ]
    )
    handler = ClaudeHandler(settings, conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=321,
        text="describe this",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=fixture,
                mime_type="image/jpeg",
                file_size=fixture.stat().st_size,
                width=2048,
                height=1536,
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_image_blocks is not None
    assert len(bridge.last_image_blocks) == 1
    block = bridge.last_image_blocks[0]
    # Base64 expansion is ~4/3 of the raw; verify no truncation.
    expected_b64 = base64.b64encode(fixture.read_bytes()).decode("ascii")
    assert block["source"]["data"] == expected_b64
    assert block["source"]["media_type"] == "image/jpeg"

    await conn.close()


@pytest.mark.skipif(
    os.environ.get("RUN_SDK_INT") != "1",
    reason="RUN_SDK_INT=1 required to hit the SDK with real bytes",
)
async def test_real_photo_end_to_end_via_real_bridge(tmp_path: Path) -> None:
    """C-2 integration: real JPEG through `ClaudeBridge.ask`.

    This path requires an OAuth-authenticated `claude` CLI session on the
    host; with `RUN_SDK_INT=1` set but no session it will raise a bridge
    error and still fail the test — which is correct: an SDK-int flag
    without a working session is an operator bug.
    """
    fixture = _require_fixture()

    from assistant.bridge.claude import ClaudeBridge
    from assistant.config import MediaSettings

    conn = await connect(tmp_path / "rp-int.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=120, max_turns=2),
        media=MediaSettings(photo_max_inline_bytes=fixture.stat().st_size + 1),  # type: ignore[arg-type]
    )
    # Minimal system-prompt template + skills dir so ClaudeBridge._render_system_prompt
    # does not KeyError.
    (tmp_path / "src" / "assistant" / "bridge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "assistant" / "bridge" / "system_prompt.md").write_text(
        "project_root={project_root} vault_dir={vault_dir} "
        "skills_manifest={skills_manifest}\n",
        encoding="utf-8",
    )
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)

    bridge = ClaudeBridge(settings)
    handler = ClaudeHandler(settings, conv, turns, bridge)

    sent: list[str] = []

    async def emit(t: str) -> None:
        sent.append(t)

    msg = IncomingMessage(
        chat_id=777,
        text="Опиши фотографию в одном предложении.",
        attachments=(
            MediaAttachment(
                kind="photo",
                local_path=fixture,
                mime_type="image/jpeg",
                file_size=fixture.stat().st_size,
                width=2048,
                height=1536,
            ),
        ),
    )
    await handler.handle(msg, emit)

    # At minimum, the turn must complete (status='complete' in turns).
    async with conn.execute(
        "SELECT status FROM turns WHERE chat_id = ?", (777,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "complete"
    # And the model must have emitted at least one text fragment.
    assert sent, "SDK returned no assistant text; multimodal turn failed"

    await conn.close()
