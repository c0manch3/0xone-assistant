# Phase 6 — детальный план (pending native rewrite)

Plan agent draft was produced but save-transport failed on transcript retrieval. Description.md is authoritative for orchestrator workflow. Detailed-plan content covers: §0 Mental model (SDK does/we do), §1 phase-5 invariants preserved, §2 Spike S-6-0 (8 empirical Qs + per-Q fallbacks), §3 AgentDefinition design (3 kinds general/worker/researcher), §4 Hooks (SubagentStart/SubagentStop factories), §5 DB schema v4 (12-column ledger), §6 SubagentStore methods, §7 CLI tools/task/main.py (spawn/list/status/cancel/wait), §8 Skill, §9 Bash hook gate, §10 Daemon integration, §11-§20 risks/QA decisions/file tree/tests/invariants/skeptical notes/exit checklist.

LOC ~1300 (vs roll-our-own ~3300; 60% reduction by leveraging SDK native).

Critical files:
- /Users/agent2/Documents/0xone-assistant/src/assistant/main.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/state/db.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/config.py
