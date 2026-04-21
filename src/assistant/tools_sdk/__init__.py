"""Home for @tool-decorator SDK custom tools (Q-D1=c pivot).

Phase 3 ships ``installer`` (7 tools). Phase 4+ adds ``memory``; phase 8+
adds ``gh``. Each submodule defines its own ``create_sdk_mcp_server(...)``
instance, exported under a descriptive constant (e.g. ``INSTALLER_SERVER``).
``ClaudeBridge._build_options`` imports and merges those constants into
``ClaudeAgentOptions.mcp_servers``.

Rationale: SKILL.md body-instruction compliance on Opus 4.7 is
unreliable (GH issues #39851, #41510). Moving the long tail of tool
logic to first-class SDK tools removes that compliance dependency
entirely. See ``plan/phase2/known-debt.md#D1`` for history.
"""
