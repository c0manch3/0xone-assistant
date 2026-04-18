---
name: task
description: "Delegate long-running work to a background subagent. For async UX (owner keeps chatting) use CLI `python tools/task/main.py spawn --kind K --task T` — the main turn returns immediately and the subagent's final result is delivered to the owner via Telegram automatically. The native Task tool BLOCKS the main turn until the subagent finishes — use only for short (<30s) delegations where blocking is acceptable. Three kinds available: general (default, full tool access), worker (CLI-focused), researcher (read-only). CLI `python tools/task/main.py` manages spawn/list/status/cancel/wait from shell init."
allowed-tools: [Task, Bash]
---

# task

Этот skill описывает, когда и как делегировать задачу фоновому subagent'у
вместо того, чтобы решать её inline в основном turn'е. Phase 6 даёт
два пути: **CLI `python tools/task/main.py spawn ...`** (async, main turn
возвращается сразу — preferred default) и **native `Task` tool** (SDK RPC;
блокирует main turn до завершения subagent'а — только для коротких
делегаций).

## Два пути делегации

### CLI — async, NON-blocking (preferred default)

```bash
python tools/task/main.py spawn --kind researcher \
  --task "Найди 3 недавних OAuth 2.0 security CVE и выпиши summary"
```

Возвращает `{"job_id": 42, "status": "requested"}` мгновенно. Picker
подхватывает row'у в фоне, SDK запускает subagent'а, Stop hook отправит
финальный текст в Telegram. **Основной turn может завершиться сразу** —
owner продолжает чатиться, пока subagent работает.

Используй этот путь ВСЕГДА, когда:
- задача занимает >30 секунд,
- owner явно попросил "в фоне" / "run in background",
- любой длинный write-up / deep research / bulk CLI.

### Native `Task` tool — sync RPC, BLOCKS main turn

SDK-native Task tool — синхронный RPC: main turn ждёт, пока subagent
полностью завершится. Используй только когда блокировка главного turn'а
**явно приемлема**, а задача короткая (<30 секунд).

Когда использовать:
- короткий лукап / классификация (<30 s), где результат нужен сразу в
  том же turn'е,
- нет необходимости освобождать main turn — owner всё равно ждёт ответ.

Когда НЕ использовать:
- любой длинный write-up → **используй CLI**, иначе owner увидит "bot
  typing..." на несколько минут,
- любая задача, которая может потребовать >30 s → CLI,
- задачи, где владелец явно хочет продолжать диалог параллельно → CLI.

## Общие правила

- Быстрый factual question → отвечай сразу в main turn, без делегации.
- Ambiguous ask → сначала уточни у owner'а, потом делегируй.
- Действие, результат которого нужен в следующем tool call того же
  turn'а — main turn или native Task (CLI async не подойдёт).

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

### CLI spawn (preferred default, NON-blocking)

```bash
python tools/task/main.py spawn --kind researcher \
  --task "Найди 3 недавних OAuth 2.0 security CVE и выпиши summary"
```

Ответ: `{"job_id": 42, "status": "requested"}`. Picker (bg task в
daemon'е) подхватит row'у, вызовет SDK, Start hook патчит
`sdk_agent_id`, Stop hook отправит финальный текст в Telegram. Main
turn возвращается сразу — owner продолжает диалог, пока subagent
работает. Твой main turn короткое подтверждение вроде "окей, запустил
job 42 в фоне — пришлю результат" достаточно.

### Native Task tool (sync RPC — main turn BLOCKS)

Вызови `Task` tool. SDK спавнит subagent'а; main turn ждёт, пока
subagent не завершится, потом возвращает управление. Hooks в daemon'е
дополнительно доставят результат в Telegram — но owner уже видит его в
ответе main turn'а. Используй только для коротких (<30 s) delegations,
где блокировка приемлема. Для длинных задач используй CLI выше.

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

- **Native Task tool is synchronous RPC — main turn blocks.** S-6-0 Q1
  / wave-2 Q1-BG re-run: `background=True` флаг зафиксирован в
  AgentDefinition ради forward-compat, но на SDK 0.1.59 + CLI 2.1.114
  main turn ждёт subagent'а так же, как без флага. **Для async UX
  используй CLI `python tools/task/main.py spawn`** (preferred default) —
  main turn возвращается сразу, picker дисптчит subagent'а в фоне.
  Native Task — только когда блокировка main turn'а приемлема (короткая
  <30 s задача).
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
