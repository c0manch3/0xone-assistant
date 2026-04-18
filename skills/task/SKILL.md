---
name: task
description: "Delegate long-running work (>10s) to a background subagent via the native Task tool. Use when the user asks for a long writeup, deep research, or a bulk tool operation that would stall the main turn. The subagent's final result is delivered to the owner via Telegram automatically; you don't need to re-paste it back. Three kinds available: general (default, full tool access), worker (CLI-focused), researcher (read-only). CLI `python tools/task/main.py` manages spawn/list/status/cancel/wait from shell init."
allowed-tools: [Task, Bash]
---

# task

Этот skill описывает, когда и как делегировать задачу фоновому subagent'у
вместо того, чтобы решать её inline в основном turn'е. Phase 6 даёт
SDK-native `Task` tool — ты просто вызываешь его, SDK спавнит
subagent'а, hooks в daemon'е пишут ledger-row и отправляют финальный
текст в Telegram.

## Когда использовать

- Задача займёт заметно больше 10 секунд (длинный write-up, deep
  research, прогон 10+ файлов и т.п.)
- Read-only research: собрать факты из Read/Grep/Glob/WebFetch и
  подготовить сводку.
- Bulk CLI operation: один CLI вызов, который долго работает.
- Operator явно попросил "run in background" / "отработай в фоне".

## Когда НЕ использовать

- Быстрый factual question → отвечай сразу в main turn.
- Ambiguous ask → сначала уточни у owner'а, потом делегируй.
- Действие, результат которого сразу нужен в том же turn'е (например,
  `memory write` и следующий шаг использует этот файл) — main turn.

## Три вида subagent'ов

| kind | tools | примеры |
|------|-------|---------|
| `general` | Bash, Read, Write, Edit, Grep, Glob, WebFetch | длинные посты, multi-step reasoning, генерация артефактов |
| `worker` | Bash, Read | один CLI + его вывод, простая инструментальная задача |
| `researcher` | Read, Grep, Glob, WebFetch | read-only research + concise summary |

Все три inherit model от главного turn'а (`model="inherit"`) и не
имеют доступа к `Task` tool'у — depth cap = 1 (subagent не может
spawn'ить sub-subagent'а в phase 6).

## Как делегировать

### Из main turn (inline)

Вызови `Task` tool. SDK спавнит subagent'а; hooks сами доставят
результат owner'у. Твой main turn не должен перепечатывать длинный
результат — короткое подтверждение вроде "готово, ответ отправлен в
Telegram" достаточно.

### Shell-init / manual

```bash
python tools/task/main.py spawn --kind researcher \
  --task "Найди 3 недавних OAuth 2.0 security CVE и выпиши summary"
```

Ответ: `{"job_id": 42, "status": "requested"}`. Picker (bg task в
daemon'е) подхватит row'у, вызовет SDK, Start hook патчит
`sdk_agent_id`, Stop hook отправит финальный текст в Telegram.

### Посмотреть список

```bash
python tools/task/main.py list                    # 20 последних
python tools/task/main.py list --status started
python tools/task/main.py list --kind researcher --limit 5
```

### Посмотреть конкретный job

```bash
python tools/task/main.py status 42
```

Exit 7 если нет такого id.

### Отменить

```bash
python tools/task/main.py cancel 42
```

Выставит `cancel_requested=1`. Если subagent делает tool call —
PreToolUse hook вернёт deny и stack unwind'нет. Если subagent НЕ
делает tool call'ов (например, только генерирует текст), флаг ничего
не сделает — subagent закончит работу до конца. Это задокументированное
ограничение (S-6-0 Q7).

### Дождаться завершения

```bash
python tools/task/main.py wait 42 --timeout-s 120
```

Exit 0 если `completed`, 5 если другой terminal status, 6 если timeout.
Полезно для CI / скриптов, где нужно синхронно дождаться результата.

## Exit codes CLI

- `0` — ok
- `2` — usage
- `3` — валидация / cap
- `4` — I/O (DB не создана)
- `5` — `wait` завершился terminal-статусом, отличным от `completed`
- `6` — `wait` timeout
- `7` — job id не найден

## Границы и поведение

- **Main turn stays open до завершения subagent'а.** S-6-0 Q1 /
  wave-2 Q1-BG re-run: `background=True` флаг зафиксирован в
  AgentDefinition ради forward-compat, но на SDK 0.1.59 + CLI 2.1.114
  main turn ждёт subagent'а так же, как без флага. Использование
  `python tools/task/main.py spawn` (а не inline Task) решает это —
  main turn возвращается сразу.
- **Delivery = at-least-once.** Если daemon рестартует между
  subagent'ом и Telegram'ом — recover_orphans пометит row
  `interrupted`, owner увидит уведомление на следующем старте.
- **Tool-free subagents uncancellable.** Флаг проверяется в
  PreToolUse — нет tool call'а, нет проверки.
- **Depth cap = 1.** Subagent'ы не имеют `Task` в своём `tools`, так
  что recursion эмпирически не наблюдается (S-6-0 Q4; регрессионный
  тест `test_subagent_no_recursion_lock`).
- **Subagent Bash проходит через parent phase-3 sandbox.** S-2 wave-2
  верифицировал 5/5 denies — argv allowlist, file-path guard, SSRF
  defence работают для subagent'а так же, как для main turn.

## Observability

```bash
python tools/task/main.py list --limit 50
```

Покажет row'ы с `status`, `agent_type`, `started_at`, `finished_at`,
`cost_usd` (всегда NULL в phase 6 — phase 9 заполнит), `result_summary`
(первые 500 символов финального текста).
