"""Phase 7 / commit 17 — E2E voice → transcribe → reply.

Wires together the Wave-7A adapter ingress (`_on_voice`), the Wave-7B
handler envelope (voice system-note), and the Wave-4 `tools/transcribe/
main.py` thin-client CLI. No real whisper-server is involved; the
multipart POST is intercepted at `urllib.request.urlopen` so the test
is hermetic.

Scenarios covered:

  * **Handler emits a voice system-note.** The adapter layer builds a
    `MediaAttachment(kind="voice", ...)`, drops it into the
    `IncomingMessage`, and the handler surfaces a system-note that
    names the file path + duration. Contract for the model: it sees
    the note and knows to invoke the CLI.
  * **Transcribe CLI returns upstream JSON verbatim.** We POST the
    fixture voice file at a stubbed whisper endpoint and the CLI
    writes the JSON response to stdout with exit 0. This is the piece
    a worker subagent runs on the model's behalf.
  * **SDK-gated full loop.** When `RUN_SDK_INT=1` the test skips
    cleanly (no real endpoint); without the flag the pure pieces
    still run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage, TextBlock

from assistant.adapters.base import IncomingMessage, MediaAttachment
from assistant.bridge.claude import InitMeta
from assistant.config import ClaudeSettings, MediaSettings, Settings
from assistant.handlers.message import ClaudeHandler
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from assistant.state.turns import TurnStore

_TRANSCRIBE_CLI = (
    Path(__file__).resolve().parents[1] / "tools" / "transcribe" / "main.py"
)


class _SpyBridge:
    """Capture the exact `ask(...)` kwargs the handler forwards."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.last_system_notes: list[str] | None = None
        self.last_image_blocks: list[dict[str, Any]] | None = None
        self.last_user_text: str | None = None

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
        *,
        system_notes: list[str] | None = None,
        image_blocks: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        del chat_id, history
        self.last_user_text = user_text
        self.last_system_notes = (
            list(system_notes) if system_notes is not None else None
        )
        self.last_image_blocks = (
            list(image_blocks) if image_blocks is not None else None
        )
        for item in self._items:
            yield item


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(),
        media=MediaSettings(),
    )


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


async def test_voice_attachment_surfaces_transcribe_note(tmp_path: Path) -> None:
    """Handler builds a system-note that points the model at the CLI.

    Contract: the note text contains the absolute path, the duration,
    and a hint at `tools/transcribe/`. Model sees these and decides
    inline-Bash vs. `task spawn --kind worker`. Without the note the
    model is blind — the voice bytes are NEVER fed into the envelope
    (only text + optional image blocks are).
    """
    conn = await connect(tmp_path / "e2e.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    inbox = tmp_path / "data" / "media" / "inbox"
    inbox.mkdir(parents=True)
    voice = inbox / "voice.oga"
    voice.write_bytes(b"OggS" + b"\x00" * 256)

    bridge = _SpyBridge(
        [
            InitMeta(model="m", skills=[], cwd=None, session_id=None),
            TextBlock(text="okay, transcribing"),
            _result(),
        ]
    )
    handler = ClaudeHandler(_settings(tmp_path), conv, turns, bridge)  # type: ignore[arg-type]

    async def emit(_: str) -> None:
        return None

    msg = IncomingMessage(
        chat_id=42,
        text="listen to this",
        attachments=(
            MediaAttachment(
                kind="voice",
                local_path=voice,
                mime_type="audio/ogg",
                file_size=voice.stat().st_size,
                duration_s=120,
            ),
        ),
    )
    await handler.handle(msg, emit)

    assert bridge.last_system_notes is not None
    voice_notes = [n for n in bridge.last_system_notes if "voice" in n]
    assert voice_notes, bridge.last_system_notes
    assert str(voice) in voice_notes[0]
    assert "duration=120" in voice_notes[0]
    # Model gets the tool hint so it can pick inline vs worker.
    assert "tools/transcribe" in voice_notes[0]

    await conn.close()


def test_transcribe_cli_returns_upstream_json_verbatim(tmp_path: Path) -> None:
    """CLI ships the whisper-server's JSON response to stdout unchanged.

    We stub `urlopen` at the import site inside the CLI's child process
    by pre-loading a sitecustomize shim. The shim intercepts the POST,
    records the body, and returns a canned JSON envelope. This
    mirrors the runtime contract: the subagent reads stdout, parses
    one JSON line, and forwards the `text` field in its reply.
    """
    voice = tmp_path / "voice.oga"
    voice.write_bytes(b"OggS" + b"\xab" * 256)

    # Build a sitecustomize that patches urlopen BEFORE the CLI runs.
    # This is the cleanest hermetic way to gate a stdlib-only CLI:
    # PYTHONPATH-first sitecustomize is executed automatically by
    # Python on interpreter start, before the CLI's own `main()`.
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "sitecustomize.py").write_text(
        "import io, json\n"
        "from urllib import request as _req\n"
        "\n"
        "_CANNED = {\n"
        '    "text": "привет мир",\n'
        '    "language": "ru",\n'
        '    "duration": 1.0,\n'
        "}\n"
        "\n"
        "class _Resp(io.BytesIO):\n"
        "    status = 200\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *a):\n"
        "        return False\n"
        "\n"
        "def _fake_urlopen(req, timeout=None):\n"
        "    # Accept Request and bare URL; we only need the body size + method\n"
        "    return _Resp(json.dumps(_CANNED).encode('utf-8'))\n"
        "\n"
        "_req.urlopen = _fake_urlopen\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(shim_dir) + os.pathsep + env.get("PYTHONPATH", "")
    # Keep the endpoint on 127.0.0.1 so the loopback-only guard passes.
    env["MEDIA_TRANSCRIBE_ENDPOINT"] = "http://127.0.0.1:9100/transcribe"

    proc = subprocess.run(
        [sys.executable, str(_TRANSCRIBE_CLI), str(voice), "--language", "ru"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}; stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["text"] == "привет мир"
    assert payload["language"] == "ru"


@pytest.mark.skipif(
    os.environ.get("RUN_SDK_INT") != "1",
    reason="RUN_SDK_INT=1 required for full voice→SDK roundtrip",
)
async def test_voice_turn_reaches_result_message_via_sdk(tmp_path: Path) -> None:
    """Full envelope round-trip with a real SDK session.

    The model will see a voice note + a directive to transcribe. It
    may or may not decide to actually call the CLI (depends on
    sampling), but the turn MUST reach `ResultMessage`. That is the
    smoke this gate protects.
    """
    from assistant.bridge.claude import ClaudeBridge

    inbox = tmp_path / "data" / "media" / "inbox"
    inbox.mkdir(parents=True)
    voice = inbox / "voice.oga"
    voice.write_bytes(b"OggS" + b"\x00" * 256)

    conn = await connect(tmp_path / "e2e-sdk.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    turns = TurnStore(conn, lock=conv.lock)

    settings = Settings(
        telegram_bot_token="t",
        owner_chat_id=1,
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        claude=ClaudeSettings(timeout=60, max_turns=2),
        media=MediaSettings(),
    )
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
        chat_id=333,
        text="Acknowledge receipt in one short sentence.",
        attachments=(
            MediaAttachment(
                kind="voice",
                local_path=voice,
                mime_type="audio/ogg",
                file_size=voice.stat().st_size,
                duration_s=5,
            ),
        ),
    )
    await handler.handle(msg, emit)

    async with conn.execute(
        "SELECT status FROM turns WHERE chat_id = ?", (333,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "complete", f"turn did not complete, got {row[0]}"

    await conn.close()
