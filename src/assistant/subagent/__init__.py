"""Phase-6 subagent package — SDK-native thin layer.

The package owns the per-kind :class:`AgentDefinition` registry,
the ``subagent_jobs`` ledger store, the SubagentStart/Stop hooks
plumbing (with the cancel-flag PreToolUse gate), the
``SubagentRequestPicker`` consumer loop, and the notify formatter
for the Telegram delivery path.

Symbol exports kept minimal — internals (``hooks``, ``picker``,
``notify``) are imported directly by ``main.py`` / ``bridge/claude.py``
to keep the public surface explicit.
"""

from __future__ import annotations

from assistant.subagent.definitions import build_agents
from assistant.subagent.store import OrphanRecovery, SubagentJob, SubagentStore

__all__ = [
    "OrphanRecovery",
    "SubagentJob",
    "SubagentStore",
    "build_agents",
]
