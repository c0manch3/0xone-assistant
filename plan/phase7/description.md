# Phase 7 — Media tools (pending refresh)

Plan agent draft was produced but transcript retrieval failed. Refresh focuses on leveraging phase-6 subagent infrastructure (CLI picker for async) instead of custom async pipelines. Key changes from old (pre-phase-6) plan:

- Drop custom `transcribe_jobs` DB — use phase-6 `subagent_jobs` via `task spawn --kind worker`
- Long transcribe/genimage → worker subagent delegation
- Short extract-doc inline, large → subagent
- New `dispatch_reply` shared helper used by TelegramAdapter, SchedulerDispatcher, SubagentStop hook
- `_memlib` refactor closes phase-4 tech debt
- MediaSettings + 4 thin CLI HTTP clients (VPS → Mac via SSH tunnel) + local extract/render

Critical files:
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
