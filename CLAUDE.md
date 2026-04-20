# CLAUDE.md

> **NOTICE (rebuild):** implementation was wiped. All source code, tools,
> skills, tests, spikes, docs, Docker config and `uv.lock` were removed.
> Only `plan/` research is preserved. Rebuilding phase-by-phase with
> deploy + owner smoke test after each phase. See `plan/README.md` for
> the methodology and `plan/phaseN/` for per-phase prescriptions.

Project-local guidance for Claude Code agents working in this repo. Read
this before editing. Keep it short — per-task context lives in
`plan/phaseN/*.md`, not here.

## Project overview

`0xone-assistant` is a **single-user** Telegram bot backed by the
Claude Agent SDK. Architecture, phase plans and invariants live under
`plan/`. The bot runs as an in-process daemon on the owner's
workstation; there is no cloud deployment.

## Auth model

- **Claude auth = OAuth**, via the locally-installed `claude` CLI
  session. Do **not** add `ANTHROPIC_API_KEY` to `.env` or `settings`;
  the bridge rejects API-key auth. The session lives in
  `~/.claude/...` and is read by the child `claude` subprocesses the
  bridge spawns.
- **GitHub auth** — deferred until the phase that reintroduces it.
  Prior design (phase 8): `gh` OAuth for read-only calls plus a
  separate SSH deploy key for the daily vault backup push. Details in
  the phase-8 plan under `plan/phase8/`.

## Phases shipped

- **None.** All prior phase output was removed in the wipe commit.
  Phase 1 (skeleton + Telegram echo) is next; deploy + owner smoke
  test gate every subsequent phase.

## Non-negotiables

- Never introduce `ANTHROPIC_API_KEY` handling; OAuth only.
- Deploy + owner smoke test after **every** phase before starting the
  next one — the batch-deploy anti-pattern is what caused the current
  rebuild.
