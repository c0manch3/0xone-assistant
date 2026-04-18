"""Phase-6 subagent package — SDK-native thin layer.

Exports the ledger store, the ContextVar used for picker → Start-hook
correlation, and the AgentDefinition registry builder. Sub-modules are
imported lazily where needed to keep package import cheap (the CLI at
`tools/task/main.py` imports only `store` for example).
"""

from __future__ import annotations

from assistant.subagent.context import CURRENT_REQUEST_ID
from assistant.subagent.store import SubagentJob, SubagentStore

__all__ = [
    "CURRENT_REQUEST_ID",
    "SubagentJob",
    "SubagentStore",
]
