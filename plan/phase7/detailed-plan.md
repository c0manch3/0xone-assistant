# Phase 7 — детальный план (pending reconstruction)

Полный черновик (~1000 строк) не смог быть сохранён из-за ограничения передачи контента между orchestrator и coder sub-agent'ом. `description.md` в этой же папке содержит authoritative scope: задачи, E2E сценарии, зависимости, риски.

План покрывал:
1. Spike 0 (BLOCKER) — SDK multimodal envelope проверка
2. Mental model — SDK/phase-6 subagent infra / phase-7 thin layer
3. AgentDefinition reuse (worker kind для media)
4. Per-CLI contracts (transcribe/genimage/extract-doc/render-doc)
5. SKILL.md templates per tool (inline vs task spawn guidance)
6. IncomingMessage.attachments extension + MessengerAdapter send methods
7. TelegramAdapter media handlers (_on_voice/photo/document/audio/video_note)
8. ClaudeHandler + bridge multimodal envelope (photo inline base64)
9. dispatch_reply shared helper (Telegram + Scheduler + SubagentStop)
10. Bash allowlist extension (_validate_media_argv + path-guards + SSRF)
11. MediaSettings config (endpoints, caps, retention, quota)
12. Retention sweeper (14d/7d + 2GB LRU)
13. _memlib refactor (Q9a closes phase-4 tech debt)
14. Phase-5 dispatcher + phase-6 subagent hooks migrated to dispatch_reply
15. Testing plan (~20 files, ~1400 LOC)
16. Risk register (15 items)
17. Invariants (11 items)
18. Open questions for Q&A (Q-7-1..Q-7-6)
19. Commit order (15 commits)
20. Acceptance checklist

LOC target: ~1900 src + ~575 modified + ~1400 tests = ~3900 total (vs ~5000 projected in pre-phase-6 plan — 22% reduction by leveraging subagent infra).

Reconstruction: rerun Plan agent for phase 7 with context "расширить description.md в полноценный detailed-plan по образцу phase-6/detailed-plan.md".

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
