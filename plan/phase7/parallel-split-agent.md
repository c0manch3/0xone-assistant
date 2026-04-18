# Parallel-split agent — spec (phase 7)

Orchestrator sub-agent, который превращает линейный `implementation.md` v2 + graph `detailed-plan.md §19` в manifest-файл `wave-plan.md` с конкретными параллельными наборами worktree-ов.

## 1. Purpose

Coder-фаза phase-7 состоит из 19 commit'ов, из которых часть может быть запущена параллельно на отдельных git worktree-ах. Вручную составлять wave-plan хрупко: надо перечитать depency matrix, выровнять файловые scope'ы, убедиться что нет overlap'ов на modified файлах, а потом написать prompt для каждого coder agent'а. Parallel-split agent делает это один раз: читает два документа и выдаёт машиночитаемый план.

## 2. Inputs

1. **`plan/phase7/implementation.md` v2** (produces by researcher fix-pack agent ПОСЛЕ того как coder fix'и закрыли все blocker-refiner Q&A). Содержит:
   - полный ordered список commit'ов (mirrored §19.2 в detailed-plan.md).
   - per-commit: purpose, touched files (create/modify), expected LOC, tests touched, acceptance bullets.
2. **`plan/phase7/detailed-plan.md §19`**:
   - §19.1 dependency graph (ASCII).
   - §19.2 commit table (depends column).
   - §19.3 wave suggestions (Wave A/B/C/D).
   - §19.4 critical path.

Ничего больше agent не читает (нет exploration phase — specification complete).

## 3. Output — `plan/phase7/wave-plan.md`

Markdown манифест с одним wave в секции. Пример формата:

```markdown
# Wave plan — phase 7

## Metadata
- Plan version: detailed-plan.md r2 + implementation.md v2
- Generator: parallel-split agent <date>
- Max concurrent coders per wave (Q locked): 4
- Orchestrator model: isolation=worktree, merge=sequential rebase

## Wave 1 — Spike 0 (sequential, 1 agent)
### Commit 1: Spike 0
- Branch: `wt/phase7-s0`
- Worktree path: `/Users/agent2/Documents/0xone-assistant-worktrees/wt-phase7-s0`
- Files created: `spikes/phase7_s0_multimodal_envelope.py`, `spikes/phase7_s0_findings.md`
- Files modified: (none)
- Agent prompt: |
    Run Spike 0 per implementation.md §2.1. Create spikes/phase7_s0_multimodal_envelope.py (180 LOC) exercising Q0-1 through Q0-6 per detailed-plan.md §2.1. Write findings to spikes/phase7_s0_findings.md documenting PASS/FAIL per question + chosen MEDIA_PHOTO_MODE default. Do NOT modify any production source code.
- Test command: (spike artifact only — sanity-check `uv run python spikes/phase7_s0_multimodal_envelope.py`)
- Merge gate: file presence + Markdown structure valid

## Wave 2 — _memlib refactor (sequential, 1 agent)
### Commit 2: _memlib → _lib full package refactor
- Branch: `wt/phase7-memlib`
- Files: see detailed-plan.md §11.3 (~27 files)
- Agent prompt: |
    Execute the _memlib refactor per detailed-plan.md §11 + implementation.md <commit 2>. Atomic commit: all 27 files in one commit or abort. Add tests/test_memlib_refactor_regression.py per §11.5.
- Test command: `uv run pytest tests/test_memlib_refactor_regression.py tests/test_memory_*.py tests/test_skill_installer_*.py -x`
- Merge gate: green pytest + green ruff + green mypy src --strict

## Wave 3 — 4 tool CLIs (parallel, up to 4 agents)
### Commits 7-10: transcribe, genimage, extract_doc, render_doc
... analogous ...

## Wave 4 — media sub-package + dispatch_reply (parallel, 2 agents)
### Commit 5: src/assistant/media/**
### Commit 6: adapters/dispatch_reply.py + dedup ledger

## Wave 5 — Bash allowlist extension (sequential, 1 agent)
### Commit 11

## Wave 6 — TelegramAdapter + handler/bridge envelope (sequential, 1 agent)
### Commits 12, 13 (combined — shared types in base.py)

## Wave 7 — SchedulerDispatcher + SubagentStop switches (parallel, 2 agents)
### Commits 14, 15

## Wave 8 — Daemon integration (sequential, 1 agent)
### Commit 16

## Wave 9 — Integration + cross-system tests (sequential, 1 agent)
### Commit 17

## Wave 10 — Unit tests (parallel, up to 12 agents)
### Commits 18a..18l (partitions of 4)

## Wave 11 — Documentation (sequential, 1 agent)
### Commit 19
```

Format stable YAML-ish Markdown (human-readable + regex-parseable).

## 4. Execution model

Orchestrator (existing phase-6 coordinator) обрабатывает `wave-plan.md` сверху вниз:

1. **Per wave:**
   - Прочитать секцию.
   - Для каждого sub-commit'а спавнить coder agent:
     - Создать worktree: `git worktree add <path> -b <branch>`.
     - Передать agent'у prompt из манифеста + scope restriction.
     - Ждать agent.done.
   - После завершения ВСЕХ coder'ов в wave:
     - Для каждой worktree: run test command.
     - Если все green: sequential merge в main.
     - После каждого merge: полный `uv run pytest && ruff check && mypy src --strict`. Если red — coder-follow-up в wt.
   - После successful merge всех sub-commit'ов: `git worktree remove <path>`.

2. **Concurrency cap:** ≤4 активных coder-agent'ов (Q locked). Wave 10 обрабатывается в 3 partitions по 4.

3. **Agent prompt isolation:** каждый coder получает prompt + explicit file scope + pointer на implementation.md sections + copy of detailed-plan §<relevant>.

4. **State between waves:** main branch — single source. Wave N+1 reads main post-wave-N merges.

## 5. Error handling

| Scenario | Response |
|---|---|
| Worktree agent tests red в wt | Coder-follow-up same wt до 3 retries; затем operator alert |
| Merge conflict | `git rebase --abort` → coder resolves conflicts; if >2 cycles failing → sequential redo in new wt |
| Post-merge integration test fail | Sequential recovery commit by single coder on top of main |
| Coder timeout | Kill agent; inspect wt state; reset to pre-agent SHA + retry |
| Flaky test post-merge | Rerun 3x; if 2+ stable red → follow-up; if 1-2 red → mark-and-continue |

## 6. Rollback

- **Per-wave:** >3 coder-follow-ups red OR merge conflict storm → `git reset --hard <pre-wave-tag>`, alert operator.
- **Worktree hygiene:** successful merge → `git worktree remove`. Failed → preserve для inspection.
- **Pre-wave tag:** `git tag phase7-pre-wave-N` перед каждым wave.

## 7. Fallback — degraded-mode

Если parallel execution стабильно ломается (>50% wave'ов требуют >1 follow-up), orchestrator switches в sequential-coder режим. Env flag `PHASE7_PARALLEL_DISABLED=1`. Existing phase-6 pattern, proven.

## 8. Кто строит `wave-plan.md`

**Рекомендовано: general-purpose sub-agent с explicit prompt'ом. Нового типа agent'а НЕ нужно.**

Orchestrator вызывает general-purpose sub-agent в начале coder-phase:

```
You are the parallel-split agent for phase 7.

Read:
- plan/phase7/implementation.md (v2, post fix-pack)
- plan/phase7/detailed-plan.md §19.1 (graph), §19.2 (commit table), §19.3 (wave suggestions), §19.4 (critical path)

Produce plan/phase7/wave-plan.md per §3 format. Rules:
1. Max 4 parallel coder per wave (Q locked).
2. Commits grouped in wave only if ALL dependencies merged BEFORE this wave (not pending in same wave).
3. Two commits in same wave MUST NOT modify overlapping files (grep each commit's modified files — overlap → serialize).
4. Spike 0 and _memlib refactor — ALWAYS single-agent sequential (wave 1, wave 2).
5. Wave 10 (tests) — up to 12 parallel; split into partitions of 4.
6. Per-wave: branch name (wt/phase7-<slug>), worktree path, files scope, agent prompt (self-contained), test command, merge gate.
7. If two commits in potential parallel wave share modified file — serialize.
8. Dedup ledger (§7) is part of commit 6, not separate.

Output: single Markdown file per §3 format. Do NOT modify implementation.md or detailed-plan.md. Do NOT create any other file.
```

## 9. Regression / trust concerns

| Concern | Mitigation |
|---|---|
| Split agent misses dependency → wave fails at merge | Detection via merge conflict OR post-merge fail → follow-up coder (§5). Worst — fallback sequential (§7). |
| Split agent generates invalid format | Schema validation: required keys (branch, files, prompt, test command). Parse error → abort + re-invoke. |
| Waves consistently fail | §7 fallback — phase 7 completes sequentially. |
| Split agent hallucinates files | Human review `wave-plan.md` MANDATORY before first merge. Orchestrator waits for operator ACK. |

## 10. Boundaries

- Parallel-split agent — **read-only**. Reads planning docs, writes one file (wave-plan.md).
- No exploration (no grep source), no current git state check (orchestrator does post-factum).
- Does not choose coder-agent models (orchestrator binds).
- One invocation per phase. Mid-phase pivot → new invocation with updated implementation.md.

---

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/dispatch_reply.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
- /Users/agent2/Documents/0xone-assistant/tools/memory/main.py
- /Users/agent2/Documents/0xone-assistant/tests/conftest.py
- /Users/agent2/Documents/0xone-assistant/pyproject.toml
