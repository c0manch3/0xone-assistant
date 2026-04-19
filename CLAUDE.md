# CLAUDE.md

Project-local guidance for Claude Code agents working in this repo. Read
this before editing. Keep it short — per-task context lives in
`plan/phaseN/*.md`, not here.

## Project overview

`0xone-assistant` is a **single-user** Telegram bot backed by the
Claude Code SDK. Architecture, phase plans and invariants live under
`plan/`. The bot runs as an in-process daemon (`python -m assistant`)
on the owner's workstation; there is no cloud deployment.

## Auth model

- **Claude auth = OAuth**, via the locally-installed `claude` CLI
  session. Do **not** add `ANTHROPIC_API_KEY` to `.env` or `settings`;
  the bridge rejects API-key auth. The session lives in
  `~/.claude/...` and is read by the child `claude` subprocesses the
  bridge spawns.
- **GitHub auth = `gh` OAuth** (main account) for read-only
  `issue`/`pr`/`repo` calls, plus a **separate** SSH deploy key for
  the daily vault backup push. See `docs/ops/github-setup.md` for
  setup.

## Skills

Skills live under `skills/<name>/SKILL.md` and are auto-discovered by
the Claude Code runtime. Currently shipped:

- `memory` — long-term notes in an Obsidian-style vault.
- `scheduler` — cron registration for periodic reminders.
- `task` — async delegation to background subagents.
- `gh` — GitHub issues/PRs/repos (read-only) + daily vault
  auto-commit (phase 8).
- `ping` — healthcheck sentinel.
- `skill-installer` — install external skills from URLs / marketplace.

Each skill has its own CLI under `tools/<skill>/main.py`. Invocation
is always by explicit path (never via `pyproject` scripts) so the
bash hook `_validate_<skill>_argv` can veto argv before subprocess
spawn.

## Phases shipped

- **Phase 8** — GitHub CLI wrapper (`tools/gh/`) + daily vault
  auto-commit to a separate GitHub account via SSH deploy key. See
  `docs/ops/github-setup.md`.

## Commands the bot owner runs directly

- `just run` — start the daemon in foreground.
- `just lint` — ruff + format + mypy strict on new files.
- `uv run pytest -q` — full test suite.
- `python tools/gh/main.py auth-status` — verify `gh` OAuth session.
- `python tools/schedule/main.py ls` / `rm` / `revive-seed` — manage
  scheduled jobs (including the phase-8 vault auto-commit seed).

## Non-negotiables

- Never introduce `ANTHROPIC_API_KEY` handling; OAuth only.
- Never push vault plaintext to a **shared** GitHub account; use the
  dedicated `vaultbot-owner` account per `docs/ops/github-setup.md`.
- All child subprocess argv goes through the bash pretool hook; every
  new CLI must provide a `_validate_<tool>_argv` guard in
  `src/assistant/bridge/hooks.py`.
- Flock, path-pinning, and isolated `UserKnownHostsFile` in
  `tools/gh/` are invariants I-8.1..I-8.4 — do not relax them.
