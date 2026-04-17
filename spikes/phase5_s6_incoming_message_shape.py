"""Spike S-6: IncomingMessage shape + origin enum reality.

Question: Plan §5.4 builds `IncomingMessage(origin="scheduler", meta={...})`.
Does IncomingMessage have a `meta` field? Does the `Origin` Literal
already include "scheduler"? Does ClaudeHandler._run_turn currently
branch on origin?

Pass criterion: emit a table of current fields vs needed changes; enumerate
the exact code deltas required.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))


def run() -> dict:
    from assistant.adapters.base import IncomingMessage, Origin  # noqa: F401

    fields = [
        {
            "name": f.name,
            "type": str(f.type),
            "default": repr(f.default) if f.default is not dataclasses.MISSING else "REQUIRED",
            "default_factory": repr(f.default_factory)
            if f.default_factory is not dataclasses.MISSING
            else None,
        }
        for f in dataclasses.fields(IncomingMessage)
    ]

    # Try constructing with origin="scheduler" and without meta to prove
    # the Literal accepts it at runtime (mypy only enforces at type-check time).
    ok_without_meta = False
    err_without_meta: str | None = None
    try:
        msg = IncomingMessage(chat_id=1, text="t", origin="scheduler")
        ok_without_meta = True
        _ = msg  # unused
    except Exception as exc:  # noqa: BLE001
        err_without_meta = repr(exc)

    # Try with meta={} — must fail because no such field exists yet.
    ok_with_meta = False
    err_with_meta: str | None = None
    try:
        msg = IncomingMessage(chat_id=1, text="t", origin="scheduler", meta={"trigger_id": 42})  # type: ignore[call-arg]
        ok_with_meta = True
    except TypeError as exc:
        err_with_meta = repr(exc)
    except Exception as exc:  # noqa: BLE001
        err_with_meta = repr(exc)

    # Grep for handler origin usage — done by static inspection, we read
    # the file and check the substrings.
    handler_path = _ROOT / "src" / "assistant" / "handlers" / "message.py"
    handler_src = handler_path.read_text(encoding="utf-8")
    origin_mentions = handler_src.count("msg.origin")
    branches_on_origin = (
        "if msg.origin" in handler_src
        or "match msg.origin" in handler_src
        or "elif msg.origin" in handler_src
    )

    # Grep bridge/claude.py to confirm `ask(... system_notes=...)` signature.
    bridge_path = _ROOT / "src" / "assistant" / "bridge" / "claude.py"
    bridge_src = bridge_path.read_text(encoding="utf-8")
    has_system_notes_param = "system_notes: list[str] | None" in bridge_src
    iterates_notes = "for note in system_notes" in bridge_src

    return {
        "incoming_message_fields": fields,
        "origin_literal_accepts_scheduler": ok_without_meta,
        "origin_literal_err": err_without_meta,
        "meta_field_exists": ok_with_meta,
        "meta_field_err": err_with_meta,
        "handler_origin_mentions_count": origin_mentions,
        "handler_branches_on_origin": branches_on_origin,
        "bridge_ask_has_system_notes_param": has_system_notes_param,
        "bridge_iterates_notes_in_order": iterates_notes,
        "code_deltas_required": {
            "adapters/base.py::IncomingMessage": (
                "add field `meta: dict[str, Any] | None = None` (field 5)"
            ),
            "handlers/message.py::_run_turn": (
                "insert scheduler-note branch before url_note assembly; when "
                "msg.origin == 'scheduler' prepend scheduler_note to "
                "system_notes; keep url_note after."
            ),
            "bridge/claude.py": "docstring only — merge-order already deterministic",
        },
    }


def main() -> None:
    print(json.dumps(run(), indent=2, default=str))


if __name__ == "__main__":
    main()
