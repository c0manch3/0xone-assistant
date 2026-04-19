---
name: gh
description: GitHub issues/PRs/repos read-only + daily vault backup commits
allowed-tools: [Bash]
---

# gh — GitHub CLI wrapper (phase 8)

Используй `tools/gh/main.py` для read-only GitHub операций (issues, pull
requests, repo metadata) и ежедневного backup'а `<data_dir>/vault/` на
отдельный GitHub account через SSH deploy-key. Все операции идут через
`gh` OAuth session — никаких `ANTHROPIC_API_KEY` / `GH_TOKEN` в env.

## Когда использовать

- Владелец спрашивает про issues / PR / репо → read-only `list`/`view`.
- Владелец явно просит "открой issue про X" → `issue create` (единственная
  write-операция в phase 8; всё остальное — phase 9 keyboard-confirm).
- Cron `0 3 * * *` (Europe/Moscow) → `vault-commit-push` для бэкапа заметок.

## Artefact paths rule (H-13, phase-7)

Always put one space after `:` before the artefact path / sha. Inherited
from phase-7 `skills/memory/SKILL.md` and `skills/genimage/SKILL.md` —
the dispatch reply regex expects `<label>: <path>` (exactly one space).
Всегда пиши путь артефакта через пробел после двоеточия.

- Good: `готово: abc1234` / `vault сохранён: sha=abc1234`
- Bad:  `готово:abc1234` (без пробела → artefact dispatch не матчится)
- Bad:  `готово :abc1234` (пробел перед `:` тоже ломает матч)

После `vault-commit-push` exit 0 — всегда упоминай `commit_sha` в ответе.
Если exit 5 (`no_changes`) — silent, ничего не говори владельцу.

## Команды

### `auth-status` — проверка OAuth session

```bash
python tools/gh/main.py auth-status
```

Возвращает `{"ok": true}` при валидной session; exit 4 с
`{"ok": false, "error": "not_authenticated"}` если `gh auth login`
требует вмешательства. Daemon prefight в `Daemon.start` делает тот же
probe один раз при старте.

### `issue` / `pr` / `repo` — read-only (C3)

```bash
python tools/gh/main.py issue list --repo OWNER/REPO --state open
python tools/gh/main.py issue view 42 --repo OWNER/REPO
python tools/gh/main.py issue create --repo OWNER/REPO --title "bug: X" --body "..."
python tools/gh/main.py pr list --repo OWNER/REPO
python tools/gh/main.py pr view 15 --repo OWNER/REPO
python tools/gh/main.py repo view OWNER/REPO
```

Все `--repo` проверяются через `GH_ALLOWED_REPOS` allowlist; неподходящий
repo → exit 6 (`repo_not_allowed`).

### `vault-commit-push` — ежедневный backup (C4)

```bash
python tools/gh/main.py vault-commit-push [--message MSG] [--dry-run]
```

Коммитит и пушит `<data_dir>/vault/` на `GH_VAULT_REMOTE_URL`. Если
изменений нет — silent exit 5. При diverged remote — exit 7, владелец
разрешает руками. `--dry-run` обходит flock и не делает push.

## Exit code matrix

| code | смысл | что делать модели |
|---|---|---|
| 0 | ok | read-команда вернула данные, либо коммит/push прошёл |
| 1 | gh_timeout | `gh` не ответил за 10 с; повтори позже, не спамь |
| 2 | argv error | неверные аргументы или subcommand не готов ещё (C3/C4) |
| 3 | validation | vault_dir не настроен, путь невалиден, --body пустой |
| 4 | gh_not_authed | `gh auth login` требует вмешательства owner'а; скажи ему |
| 5 | no_changes | silent для scheduler; моделям → ничего не отвечать owner'у |
| 6 | repo_not_allowed | целевой `--repo` не в `GH_ALLOWED_REPOS` |
| 7 | diverged | remote расходится; попроси owner'а разрешить вручную |
| 8 | push_failed | другая ошибка git push (сеть, SSH); покажи stderr |
| 9 | lock_busy | параллельный `vault-commit-push` идёт; подожди минуту |
| 10 | ssh_key_error | deploy key отсутствует или неправильные permissions |

## Правила безопасности (выдерживать все)

1. НЕ использовать `gh pr create`, `gh issue close/comment/edit`,
   `gh pr merge`, `gh api -X POST/...` — это phase 9 keyboard-confirm.
2. НЕ использовать `--force`, `--force-with-lease`, `--no-verify`, `--amend`.
3. НЕ использовать `gh repo clone`, `gh repo create`, `gh repo delete`.
4. Target `--repo` должен быть в `GH_ALLOWED_REPOS`. CLI сам проверит,
   но если ты видишь, что владелец просит чужой repo — откажи сразу,
   не запускай команду.
5. H-13: пробел после `:` в любом упоминании артефакта / sha.

## Примеры диалогов

- "проверь gh login" → `python tools/gh/main.py auth-status` →
  exit 0 → `gh сессия ok`.
- "открой issue про баг X" → `python tools/gh/main.py issue create
  --repo OWNER/REPO --title "bug: X" --body "..."` → `issue создан: #42`.
- "какие открытые issues?" → `python tools/gh/main.py issue list
  --repo OWNER/REPO --state open` → суммаризуй список.
- "посмотри PR #15" → `python tools/gh/main.py pr view 15 --repo OWNER/REPO`
  → краткий пересказ автора/title/state.
- "запушь vault" → `python tools/gh/main.py vault-commit-push` →
  exit 0 → `vault сохранён: sha=abc1234, файлов: 3`.
