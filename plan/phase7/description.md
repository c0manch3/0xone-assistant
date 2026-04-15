# Phase 7 — GitHub tool + auto-commit

**Цель:** модель умеет работать с GitHub; vault ежедневно бэкапится.

**Вход:** phase 5 (scheduler), phase 4 (vault).

**Выход:** `tools/gh` скилл доступен; дефолтный scheduled-job коммитит+пушит vault в 03:00.

## Задачи

1. `tools/gh/main.py` — тонкая обёртка над `gh`:
   - `status`, `commit -m --paths`, `push`, `pr create`, `issue create/list/comment`.
   - Валидирует что repo path внутри `data/` или в whitelist репо из конфига.
   - Использует существующий `gh auth` на хосте.
2. `skills/github/SKILL.md` с явными примерами и предупреждениями (никогда не force-push).
3. Bootstrap при первом запуске: `git init data/vault/`, remote из env `GITHUB_VAULT_REPO`, начальный коммит.
4. Дефолтный schedule, засеваемый при первом старте:
   - `cron="0 3 * * *"`
   - `prompt="сделай git add -A и закоммить изменения vault с осмысленным сообщением, затем запушь"`
   - Модель сама вызовет `tools/gh` — граница CLI сохранена.
5. Guard: scheduler-originated промпты проходят обычный permission-стек (никаких спецправ).

## Критерии готовности

- Свежий коммит появляется на remote на следующее утро.
- Ручное "запушь память сейчас" работает.
- Force-push отклоняется CLI'ем.

## Зависимости

Phase 4, phase 5.

## Риск

**Низкий-средний.** Управление credentials — полагаемся на `gh auth login` на хосте.
