"""Phase 8 vault MCP server — single ``@tool`` for manual vault push.

Mirrors the phase 4 / 5b / 6 surface shape:

  - :data:`VAULT_SERVER` — :func:`create_sdk_mcp_server` record
    registered by :class:`~assistant.bridge.claude.ClaudeBridge`.
  - :data:`VAULT_TOOL_NAMES` — tuple of fully-qualified
    ``mcp__vault__*`` names exposed in ``allowed_tools``.

The ``vault_push_now`` handler delegates to
:meth:`~assistant.vault_sync.subsystem.VaultSyncSubsystem.push_now`.
That method enforces the 60s rate limit (W2-C4 / W2-M2 — persisted
across restart), registers the in-flight task in
``Daemon._vault_sync_pending`` for shutdown drain, and writes an audit
log row.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.tools_sdk._vault_core import (
    configure_vault,
    get_configured_subsystem,
    reset_vault_for_tests,
)

__all__ = [
    "VAULT_SERVER",
    "VAULT_TOOL_NAMES",
    "configure_vault",
    "reset_vault_for_tests",
    "vault_push_now",
]

_log = structlog.get_logger(__name__)


@tool(
    "vault_push_now",
    (
        "Manually push vault → GitHub now. ONLY for explicit owner "
        "request (\"запушь вольт\", \"сделай бэкап заметок\", "
        "\"синхронизируй vault\", \"push vault now\"). DO NOT chain "
        "memory_write → vault_push_now as a side-effect; auto-sync "
        "runs hourly without intervention. 60s rate limit between "
        "successful invocations; a second call inside the window "
        "returns reason='rate_limit' without running git ops."
    ),
    {
        "type": "object",
        "properties": {},
        "required": [],
    },
)
async def vault_push_now(args: dict[str, Any]) -> dict[str, Any]:
    """Trigger an immediate vault push.

    Returns a JSON-shaped MCP result. Possible outcomes:

      - ``{"ok": true, "result": "pushed", "files_changed": N,
            "commit_sha": "<sha>"}``
      - ``{"ok": true, "result": "noop"}``
      - ``{"ok": false, "reason": "rate_limit",
            "next_eligible_in_s": N}``
      - ``{"ok": false, "reason": "not_configured"}`` — the subsystem
        is disabled in settings or ``startup_check`` force-disabled it.
      - ``{"ok": true, "result": "lock_contention"}`` — a parallel
        memory_write held the vault lock past the timeout. Owner can
        retry shortly.
      - ``{"ok": false, "result": "failed", "error": "..."}``.
    """
    sub = get_configured_subsystem()
    if sub is None:
        payload = {
            "ok": False,
            "reason": "not_configured",
        }
        return {
            "content": [
                {"type": "text", "text": json.dumps(payload)}
            ],
            **payload,
        }
    result = await sub.push_now()
    return {
        "content": [
            {"type": "text", "text": json.dumps(result)}
        ],
        **result,
    }


# ---------------------------------------------------------------------------
# MCP server + canonical tool name tuple
# ---------------------------------------------------------------------------
VAULT_SERVER = create_sdk_mcp_server(
    name="vault",
    version="0.1.0",
    tools=[vault_push_now],
)

VAULT_TOOL_NAMES: tuple[str, ...] = ("mcp__vault__vault_push_now",)
