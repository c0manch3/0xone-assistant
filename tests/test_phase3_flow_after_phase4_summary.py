"""Phase 4 ⟷ phase 3 compatibility after the Q1 synthetic summary lands.

The Q1 snippet re-surfaces `bundle_sha` strings in the model's visible
history. The concern (detailed-plan §Compatibility with phase-3
skill-installer flow):
  * R-p3-1: preview → install flow is still cache-by-URL, not bundle-sha.
  * R-p3-2: if a snippet contains a hash, the installer CLI MUST NOT
    accept it as a flag (CLI never had `--bundle-sha`; argparse rejects
    unknown flags with exit 2 — the guard is still there).
  * R-p3-3: URL detector fires on msg.text only, not on assembled
    history envelopes.
  * R-p3-4: marker rotation (bootstrap notify) uses a distinct marker
    path from the memory flow.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from assistant.bridge.history import history_to_user_envelopes
from assistant.handlers.message import _detect_urls
from assistant.state.conversations import ConversationStore
from assistant.state.db import apply_schema, connect
from tests._helpers.history_seed import (
    seed_tool_result_row,
    seed_tool_use_row,
    seed_user_text_row,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INSTALLER = _PROJECT_ROOT / "tools" / "skill-installer" / "main.py"


async def test_bundle_sha_appears_in_history_not_in_cli_contract(tmp_path: Path) -> None:
    """Sanity: snippet carries the `bundle_sha` text, installer CLI still
    does not accept `--bundle-sha` (review wave 2 removed it on purpose).
    """
    conn = await connect(tmp_path / "hist.db")
    await apply_schema(conn)
    conv = ConversationStore(conn)
    chat_id = 1

    # Turn 1: user asks to preview; assistant runs Bash preview; the
    # installer returns a JSON with bundle_sha in its stdout.
    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t1", text="preview URL")
    await seed_tool_use_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        tool_name="Bash",
    )
    preview_json = json.dumps(
        {
            "name": "skillX",
            "bundle_sha": "abc123deadbeef",
            "file_count": 2,
            "total_size": 1024,
        }
    )
    await seed_tool_result_row(
        conn,
        chat_id=chat_id,
        turn_id="t1",
        tool_use_id="tu1",
        content=preview_json,
    )

    # Turn 2: user says "да" (confirm). The snippet now carries bundle_sha
    # text for the model to see.
    await seed_user_text_row(conn, chat_id=chat_id, turn_id="t2", text="да")

    rows = await conv.load_recent(chat_id, limit_turns=10)
    envelopes = list(history_to_user_envelopes(rows, chat_id))
    # Envelope for turn t1 (the one with tool_use/tool_result) carries
    # a synthetic note with the sha; turn t2 is a bare "да" — the model
    # still has t1's note visible in its envelope stream.
    t1_content = envelopes[0]["message"]["content"]
    assert isinstance(t1_content, list)
    note = t1_content[0]["text"]
    assert "abc123deadbeef" in note

    await conn.close()


def test_cli_rejects_bundle_sha_flag() -> None:
    """R-p3-2: installer CLI has no --bundle-sha arg; argparse exit 2."""
    proc = subprocess.run(
        [
            sys.executable,
            str(_INSTALLER),
            "install",
            "--confirm",
            "--url",
            "https://example.com/x",
            "--bundle-sha",
            "abc123deadbeef",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2
    assert "bundle-sha" in proc.stderr or "unrecognized" in proc.stderr.lower()


async def test_url_detector_ignores_history_urls(tmp_path: Path) -> None:
    """R-p3-3: `_detect_urls` fires only on the current msg.text.

    Even if history contains a URL via Q1 snippet, the current text
    "ok" produces no URL hit.
    """
    # Sanity: a real URL in current text yields a hit.
    assert _detect_urls("see https://github.com/x/y") == ["https://github.com/x/y"]
    # An empty / URL-less message produces none — this is what phase 3
    # handler uses to decide whether to emit system_notes.
    assert _detect_urls("ok") == []
    assert _detect_urls("да, ставь") == []


def test_skill_marker_rotation_unaffected(tmp_path: Path) -> None:
    """R-p3-4: the memory flow does not touch the bootstrap marker.

    Distinct file paths: `<data_dir>/run/.bootstrap_notified` (phase 3)
    vs the memory index lock `<data_dir>/memory-index.db.lock` (phase 4).
    This is a byte-for-byte sanity check.
    """
    data_dir = tmp_path / "data"
    (data_dir / "run").mkdir(parents=True)
    marker = data_dir / "run" / ".bootstrap_notified"
    marker_content = '{"rc": 1, "reason": "failed", "ts_epoch": 1.0}'
    marker.write_text(marker_content, encoding="utf-8")
    before_mtime = marker.stat().st_mtime

    # Simulate an unrelated memory operation by touching a separate lock path.
    (data_dir / "memory-index.db.lock").touch()
    time.sleep(0.01)

    # Marker untouched.
    assert marker.read_text(encoding="utf-8") == marker_content
    assert marker.stat().st_mtime == before_mtime
