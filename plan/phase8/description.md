# Phase 8 — Ops polish (health / admin / Yandex)

**Цель:** ops-полировка; явно опциональная фаза.

**Вход:** phases 2–7.

**Выход:** health-эндпоинт с метриками, простая admin-страница (опционально), Yandex Messenger адаптер (опционально).

## Задачи

1. `src/health/metrics.py` — bridge-метрики (latency/turns/cost) + per-CLI subprocess метрики (время выполнения, RC) + IPC-сокет liveness + daemon liveness-файл + валидация всех `skills/*/SKILL.md` и `tools/*/pyproject.toml` (использует `tools/skill-creator validate`).
2. `/health` endpoint (FastAPI или aiohttp) — JSON со статусом компонентов.
3. Опционально: `src/web/admin.py` — простая админ-страница (JWT) с секциями:
   - Schedules (view/enable/disable)
   - Conversations (recent)
   - Memory (browse vault)
   - Tools audit (последние вызовы CLI)
4. Опционально: `src/adapters/yandex.py` — порт из midomis, включается env-флагом `YANDEX_ENABLED`.
5. Опционально: Docker/Caddy-конфиг.

## Критерии готовности

- `/health` возвращает green при нормальной работе и показывает деградацию при падении демона или UDS-сокета.
- (Если делаем admin) panel доступна с токеном.
- (Если делаем Yandex) демо работает.

## Зависимости

Phases 2–7.

## Риск

**Низкий** — в основном ported code из midomis.
