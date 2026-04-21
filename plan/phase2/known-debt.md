# Phase 2 — Known Technical Debt

## D1 — Bash-from-skill-body unvalidated

**Origin:** phase 2 smoke iteration 2026-04-20/21. Opus 4.7 does not reliably follow imperative tool-invocation instructions from `SKILL.md` body (GitHub #39851, #41510 — open, no Anthropic fix). Ping skill refactored to text-generation pattern (midomis-inspired) to unblock phase 2 ship.

**What's NOT validated in phase 2 smoke:**
- Claude executes Bash command from within skill body context
- Bash allowlist `python tools/<X>/main.py` prefix enforcement end-to-end
- `tool_result` block persistence to `conversations` table during skill-invoked tool execution
- History replay of `tool_use` + `tool_result` blocks across restarts (relates to U1 in `unverified-assumptions.md`)

**What IS validated in phase 2 smoke:**
- SDK skill discovery through `setting_sources=["project"]` + `.claude/skills/ping/` (via symlink `.claude/skills -> skills/`)
- `Skill(skill="ping")` tool invocation by Claude (no `is_error: True`)
- SKILL.md body delivery as user envelope back to model
- Claude response generation per body instructions (text output path)

**Phase 3 obligation:**
Phase 3 plan MUST include a task "PostToolUse tool-invocation enforcement" that guarantees Claude either (a) invokes the Bash/tool command specified in a skill body, or (b) the turn is deterministically blocked / re-injected until compliance. Candidates:
- `UserPromptSubmit` hook re-injecting "follow skill body via tools" directive per turn
- `PostToolUse(Skill)` hook that records skill-name + expected-command, plus `PreToolUse(Bash)` that verifies compliance
- Alternative: architectural pivot — replace SKILL.md-based CLI tools with `@tool`-decorator custom SDK tools, which use first-class tool-selection path (no body-compliance dependency)

**Phase 4 precondition:**
Memory skill in phase 4 MUST execute a real FTS5 SQLite query via CLI. Phase 4 is **blocked on phase 3 enforcement** OR on a refactor of the memory skill to use in-process SDK custom tool instead of SKILL.md. This must be resolved during phase 3 design, not discovered during phase 4 implementation.

**Owner:** next-phase-planner (Plan agent) when drafting phase 3 description.md / detailed-plan.md.

**References:**
- GitHub [#39851](https://github.com/anthropics/claude-code/issues/39851) — workflow bypass
- GitHub [#41510](https://github.com/anthropics/claude-code/issues/41510) — skill steps dropped
- GitHub [#544](https://github.com/anthropics/claude-agent-sdk-python/issues/544) — SDK cannot control model tool selection
- Research artifact: `plan/phase2/spike-findings.md` §R13 confirms SDK skill plumbing OK; imperative body compliance NOT a SDK issue.
