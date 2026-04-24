"""Phase-5 scheduler package.

Deliberately empty (no re-exports) to avoid circular imports:
:class:`SchedulerDispatcher` imports :class:`ClaudeHandler`, which
imports :class:`ClaudeBridge`, which imports the ``scheduler`` MCP
server, which (if we re-exported here) would pull the dispatcher back
in at package-init time.

Callers import directly from the intended submodule:

    from assistant.scheduler.cron import parse_cron, is_due
    from assistant.scheduler.store import SchedulerStore
    from assistant.scheduler.loop import SchedulerLoop, RealClock
    from assistant.scheduler.dispatcher import SchedulerDispatcher
"""

from __future__ import annotations
