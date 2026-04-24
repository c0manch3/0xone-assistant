# CLAUDE.md

Project-local guidance for Claude Code agents working in this repo. Read
this before editing. Keep it short — per-task context lives in
`plan/phaseN/*.md`, not here.

## Project overview

`0xone-assistant` is a **single-user** Telegram bot backed by the
Claude Agent SDK. Architecture, phase plans and invariants live under
`plan/`. The bot runs as a long-running daemon on a **VPS**; only heavy
compute services (transcription, image generation) are planned to stay
on the owner's Mac.

## Deployment target

- **VPS:** `193.233.87.118` (Ubuntu 24.04, user `0xone`).
- **Code:** `/opt/0xone-assistant/`.
- **Data:** `~/.local/share/0xone-assistant/` (vault, SQLite, logs).
- **Env:** `~/.config/0xone-assistant/.env`.
- **Process manager:** systemd user unit `0xone-assistant.service`
  (see `deploy/systemd/`).
- **Bot transport:** aiogram long polling — no external IP needed; only
  outbound 443.
- **SSH:** `ssh -i ~/.ssh/bot 0xone@193.233.87.118`.

## Auth model

- **Claude auth = OAuth**, via the locally-installed `claude` CLI
  session. Do **not** add `ANTHROPIC_API_KEY` to `.env` or `settings`;
  the bridge rejects API-key auth. Session lives in
  `~/.claude/.credentials.json` on Linux (VPS) and macOS Keychain
  (`Claude Code-credentials` entry, Mac dev host). Transfer recipe for
  migrating session between hosts: see
  `plan/phase5a/summary.md` §"Claude OAuth session transfer".
- **GitHub auth** — deferred until the phase that reintroduces it
  (phase 8 for vault push).

## Phases shipped

- **Phase 1** — Telegram echo skeleton (commit `6f2d8d4`).
- **Phase 2** — ClaudeBridge + skill plumbing (commit `fbd9c18`).
- **Phase 3** — skill-creator + skill-installer @tool dogfood
  (commit `575aa6a`).
- **Phase 4** — long-term memory @tool MCP server + FTS5 + PyStemmer
  (commit `2a57b5d`).
- **Phase 5a** — VPS migration (this commit).
- **Phase 5b** — scheduler @tool MCP server (next commit).

## Non-negotiables

- Never introduce `ANTHROPIC_API_KEY` handling; OAuth only.
- Deploy + owner smoke test after **every** phase before starting the
  next one — the batch-deploy anti-pattern is what caused the
  pre-phase-1 rebuild.
- Single active daemon at a time across hosts (singleton lock file
  `.daemon.pid` enforces within-host; owner discipline across hosts).
