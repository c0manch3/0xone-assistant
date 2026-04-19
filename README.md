# 0xone-assistant

Персональный Telegram-бот (single-user) на Claude Code SDK.

## Запуск

    uv sync
    cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, OWNER_CHAT_ID
    just run

Архитектура и фазы — `plan/README.md`.

## Phases shipped

- **Phase 8** — GitHub CLI wrapper (`tools/gh/`) + daily vault auto-commit to
  a separate GitHub account via SSH deploy key. See
  [`docs/ops/github-setup.md`](docs/ops/github-setup.md) for the setup playbook.
