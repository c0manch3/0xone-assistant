"""Phase-6 ContextVar plumbing — picker → SubagentStart hook correlation.

S-1 spike (`spikes/phase6_s1_contextvar_hook.py`) verified empirically that
an `asyncio.ContextVar` set in the caller's scope IS visible inside the
SDK-dispatched hook callback. The picker (`subagent/picker.py`) sets
`CURRENT_REQUEST_ID` before awaiting `bridge.ask(...)`; the on_subagent_start
hook (`subagent/hooks.py`) reads it to resolve a pending `subagent_jobs`
row to the SDK-assigned `agent_id`.

Keeping this in its own module avoids an import cycle between `hooks.py`
(which builds the hook factory) and `picker.py` (which consumes the var
in its dispatch path).
"""

from __future__ import annotations

import contextvars

# Wave-2 B-W2-4 (S-1 spike PASS).
CURRENT_REQUEST_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "phase6_current_request_id",
    default=None,
)
