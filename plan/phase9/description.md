# Phase 9 — Ops polish (health metrics / admin panel / Yandex opt-in)

## Цель

Финальный phase "operations polish" перед release decision. Три независимых направления — health-metrics / observability, minimal localhost-only admin panel, и опциональный Yandex Messenger адаптер — плюс carry-over `should-fix` пунктов из phase-8 code-review. Phase 9 не добавляет новых user-facing фич; цель — чтобы owner мог наблюдать за работающим ботом (через Prometheus scraper + `/health` + локальную admin-страницу) и опционально включить вторую messenger-платформу. Последний phase в README §Порядок фаз (line 45); после его закрытия бот считается production-ready для single-owner deploy.

## Вход

- **Phase 8 shipped** (HEAD `9c2f317` — phase 8 fix-pack: close devil T6.1 + 10 should-fix items). В частности:
  - `GitHubSettings(env_prefix="GH_")` + `tools/gh/main.py` (auth-status, issue/pr/repo read, `vault-commit-push` write-pinned-на-vault).
  - Bash allowlist extensions — `_validate_gh_argv` поверх phase-7 `_validate_python_invocation` dispatcher.
  - Default-seed scheduled job `vault_auto_commit` (03:00 Europe/Moscow default) через `ensure_vault_auto_commit_seed`, tombstone-aware migrations 0005/0006.
  - `Daemon.start()` preflights: `_verify_gh_config_accessible_for_daemon`, `_probe_gh_version_for_daemon`, `_check_path_not_in_cloud_sync` для ssh-key parent, `allowed_repos`-empty WARN.
  - flock на `<data_dir>/run/gh-vault-commit.lock` + dedicated SSH deploy key через `GIT_SSH_COMMAND="ssh -F /dev/null -i <key> -o IdentitiesOnly=yes …"`.
  - `docs/ops/github-setup.md` (одним файлом; S-12 split deferred к phase 9 — см. "Задачи" ниже).
- **Phase 8 carry-over (should-fix, deferred к phase 9):** S-5 seed-helper TOCTOU (defer до первого реального production бага), S-7 `_validate_gh_argv` docstring overblock, S-8 public `rev_parse_head` helper (small refactor), S-10 `_flatten_gh_json` extension policy (doc note + version pin), S-11 Protocol для `_do_push_cycle` (mypy hygiene), S-12 разделение `docs/ops/github-setup.md` (344 LOC) на `setup.md` + `operations.md`.
- **Phase 7 carry-over:** `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap (phase-4 tech-debt); subagent pool telemetry (utilization, picker lag) — явно помечено на phase-9 ops-polish в phase-7 summary §5.
- **Existing metrics infrastructure:** НЕТ. `src/assistant/metrics/` не существует; `bridge/claude.py` не содержит метрик; `Daemon` логирует через structlog. Phase 9 строит metrics sub-package с нуля.
- **External:** Prometheus (owner scrape'ит) — optional, не shipping в repo.

## Выход — пользовательские сценарии (E2E)

### A. Observability
1. Owner делает `curl http://127.0.0.1:9090/metrics` → получает text-экспорт Prometheus format: histogram `claude_turn_duration_seconds`, counter `telegram_messages_total{dir=in|out}`, histogram `scheduler_tick_lag_ms`, counter `gh_call_total{sub=issue|pr|repo|vault_commit_push, outcome=ok|fail}`, gauge `vault_size_bytes`, counter `vault_commits_total`, counter `dedup_ledger_hits_total`, gauge `subagent_pool_inflight`.
2. `curl http://127.0.0.1:9090/health` → JSON `{"status": "ok", "uptime_s": 3612, "last_scheduler_tick_ts": "...", "last_telegram_heartbeat_ts": "...", "migrations_version": 6, "subagent_inflight": 0}`. Статус `degraded` если scheduler heartbeat старше `tick_interval_s * heartbeat_stale_multiplier`.
3. Все structlog events в JSON-формате (переключаемо через `LOG_FORMAT=json|console`) — ingestable в Loki/Vector без доп. парсинга.

### B. Admin panel (минимальная)
4. Owner открывает `http://127.0.0.1:9091/admin/` в браузере → basic-auth prompt → после auth видит read-only табы: Conversations (последние N сообщений из `ConversationStore`), Schedules (`schedules` rows + recent `triggers`), Subagent jobs (`subagent_jobs` ledger), Memory (index of vault notes — метаданные only, без содержимого), Logs (хвост structlog JSON файла).
5. Любая кнопка/форма `POST` отсутствует — panel is strictly read-only. Enable/disable schedules остаётся через `tools/schedule/main.py` CLI.

### C. Yandex opt-in
6. Owner выставляет `YANDEX_ENABLED=true` + `YANDEX_BOT_TOKEN=…` → `Daemon.start()` поднимает второй `MessengerAdapter` (yandex) параллельно Telegram. Тот же `OWNER_CHAT_ID` маппинг через `YANDEX_OWNER_CHAT_ID`. `dispatch_reply` работает одинаково. По умолчанию `YANDEX_ENABLED=false` — Yandex не подгружается, импорт lazy.

## Задачи (ordered)

### Wave A — Observability (~900 LOC, 15 тестов)

1. **`src/assistant/metrics/` sub-package** на `prometheus_client` (`Counter`, `Histogram`, `Gauge`). Метрики: `claude_turn_duration_seconds` (histogram, buckets 0.5/1/2/5/10/30/60), `telegram_messages_total{dir}`, `scheduler_tick_lag_ms` (histogram), `gh_call_total{sub, outcome}`, `vault_size_bytes` (gauge), `vault_commits_total`, `flock_contention_total{resource}`, `dedup_ledger_hits_total`, `subagent_pool_inflight` (gauge), `subagent_picker_queue_depth`.
2. **Instrumentation call-sites:**
   - `bridge/claude.py` — wrap `query()` в histogram-timer.
   - `adapters/telegram.py` — counter inc на send_text/photo/document/audio.
   - `scheduler/loop.py` — lag computed как `now_monotonic - scheduled_at`.
   - `tools/gh/_lib/git_ops.py` + CLI wrapper — counter inc с outcome=.
   - `adapters/dispatch_reply.py::_DedupLedger` — counter inc на hit.
   - `subagent/picker.py` — gauge set на inflight count.
   - `media/sweeper.py`, vault-commit — flock contention counter.
3. **`/metrics` endpoint.** In-process side-mount через **FastAPI** (`prometheus_client.make_asgi_app()`), bind `127.0.0.1:9090`. Daemon поднимает через `uvicorn.Server(config)` как bg-task.
4. **`/health` endpoint.** Structured JSON. Sources: `SchedulerLoop.last_tick_at()`, `SchedulerDispatcher.last_tick_at()`, `TelegramAdapter.last_heartbeat_ts` (новое поле), `apply_schema` migration version, `SubagentStore.count_inflight()`.
5. **Structured JSON logging.** `logger.py::setup_logging(format="json")` через `structlog.processors.JSONRenderer`. Default `console` для dev, `json` для prod через `LOG_FORMAT` env.

### Wave B — Admin panel (~500 LOC, 8 тестов)

6. **`src/assistant/web/` sub-package** — `FastAPI` app bound на `127.0.0.1:9091`, HTTPBasicAuth с `ADMIN_USER`/`ADMIN_PASS_HASH` (bcrypt) из `AdminSettings(env_prefix="ADMIN_")`.
7. **Read-only routes:** `/admin/conversations` (paginated last-100 turns), `/admin/schedules` (schedules + last 20 triggers), `/admin/subagents` (last 50 jobs), `/admin/memory` (vault index metadata only), `/admin/logs` (tail 200 lines).
8. **Opt-in flag** — `ADMIN_ENABLED=false` default. `Daemon.start()` lazy-imports `assistant.web` только если true.
9. **Docs warning** — `docs/ops/admin-panel.md`: "never expose 9091 to public internet; use SSH port-forward". Проверка `addr == 127.0.0.1` hard-coded в `uvicorn.Config`.

### Wave C — Yandex opt-in (~600 LOC, 10 тестов)

10. **`src/adapters/yandex.py`** — port из `/Users/agent2/Documents/midomis-bot` (reference). Реализует `MessengerAdapter` protocol. `send_text`, `send_photo`, `send_document`, `send_audio`.
11. **`YandexSettings(env_prefix="YANDEX_")`** — bot_token, owner_chat_id, api_base_url, enabled (default `False`).
12. **`Daemon.start()` conditional wiring** — если `settings.yandex.enabled`, создаётся второй adapter, регистрируется second handler (`ClaudeHandler` shared), `_dedup_ledger` shared, same ConversationStore.
13. **Migration 0007** — добавить `platform` column в `conversations` / `turns` (`'telegram'` | `'yandex'`). История разделяется per-platform. Tombstone-compatible.

### Wave D — Phase-8 carry-over micro-fixes (~300 LOC, 5 тестов)

14. **S-7 docstring** на `_validate_gh_argv` — 1-line clarify.
15. **S-8** `rev_parse_head` public helper в `tools/gh/_lib/git_ops.py`.
16. **S-10** doc note + version pin на `_flatten_gh_json`.
17. **S-11** Protocol для `_do_push_cycle` callable.
18. **S-12** split `docs/ops/github-setup.md` → `github-setup.md` (инициальная настройка) + `github-operations.md` (troubleshooting, rotation).
19. **HISTORY_MAX_SNIPPET_TOTAL_BYTES cap** — phase-4 carry-over; реализация в `bridge/history.py::history_to_user_envelopes`.

## Критерии готовности

- `curl http://127.0.0.1:9090/metrics` возвращает ≥ 10 названных метрик в Prometheus text format.
- `curl http://127.0.0.1:9090/health` → 200 + JSON с `status ∈ {ok, degraded}` и всеми ключами.
- `LOG_FORMAT=json` → каждый `log.info` — valid JSON одной строкой.
- `ADMIN_ENABLED=true` + basic-auth → admin-panel показывает last 10 conversations / all schedules / last 50 subagent jobs.
- `YANDEX_ENABLED=false` (default) → adapter не импортируется; `YANDEX_ENABLED=true` + валидный token → owner шлёт "ping" в Yandex чат → модель отвечает.
- **Phase-8 invariants preserved** (no regression): `GitHubSettings.allowed_repos` whitelist, `vault-commit-push` exit-code matrix (0/5/6/7/8/9/10), flock `<data_dir>/run/gh-vault-commit.lock`, dedicated SSH key via `GIT_SSH_COMMAND`, default-seed `vault_auto_commit` idempotent.
- **Phase-7 invariants preserved:** `_DedupLedger` TTL=300/cap=256, retention sweeper (14/7/2GB), media/vault separation, `ARTEFACT_RE` v3.
- CI: `uv sync` OK, `just lint` зелёный, `uv run pytest -q` — без регрессий.

## Явно НЕ в phase 9

- Multi-user admin panel, RBAC, audit-log.
- Admin panel write-операции (edit schedule, kill subagent, retry trigger).
- OAuth для admin panel (GitHub / Google).
- Full production ops (K8s, multi-region, blue/green).
- Encryption-at-rest для `data/assistant.db` + vault (phase 10+).
- Telegram UI рефакторинг, inline keyboards, callback_query handlers.
- TTS / audio reply back (phase-7 carry-over).
- Docker + Caddy deployment. README phase-9 упоминает "Docker/Caddy опционально" — можно ad-hoc Dockerfile, но не shipped.
- Grafana/Loki configs shipping в repo.
- Yandex Disk sync integration для vault.

## Зависимости

- **Phase 8 (КРИТИЧНО):** `GitHubSettings`, `tools/gh`, flock primitive — observability hooks в vault-commit-push рассчитывают на существующую структуру; admin-panel Schedules-tab показывает `vault_auto_commit` seed row.
- **Phase 7:** `_DedupLedger` metrics hook, `dispatch_reply` reuse для Yandex adapter.
- **Phase 6:** `SubagentStore.count_inflight()`, `SubagentRequestPicker` queue depth — для `subagent_pool_inflight` gauge.
- **Phase 5:** `SchedulerLoop.last_tick_at()`, `SchedulerDispatcher.last_tick_at()` — уже существуют (heartbeat watchdog phase-5 использует). Reuse.
- **Phase 4:** `ConversationStore` queries для admin panel Conversations tab. Migration 0007 (platform column) если Yandex enabled.
- **External:** `prometheus_client` (new dep), `fastapi` + `uvicorn` + `jinja2` + `python-multipart` + `bcrypt` (new deps), optional `httpx` для Yandex adapter.

## Риск

**Низкий → Средний.** Бóльшая часть phase 9 — добавление read-only observability и порт существующих паттернов.

| Severity | Risk | Mitigation |
|---|---|---|
| 🔴 | Admin panel случайно expose'нут в public internet | Hard-code `host="127.0.0.1"` в uvicorn.Config; startup-error если в env указан `0.0.0.0`; docs WARNING; basic-auth обязателен. |
| 🟡 | Secrets в logs (bot_token, gh token) при JSON log-format | `logger.py` redact-processor — сканит keys matching `(token|secret|password|key)` → replace value `<REDACTED>`. |
| 🟡 | Yandex API rate limits / API contract drift | Adapter graceful degradation — exit warn + owner-notify через Telegram (если оба enabled); cap на send attempts per minute. |
| 🟡 | Prometheus endpoint leak'ит internal state | `/metrics` не содержит user content (chat text, owner_id). Только counts / histograms. Test: `test_metrics_no_pii`. |
| 🟡 | Admin panel раскрывает vault content через Memory tab | Показываем metadata only (filename, size, mtime, tags); body не рендерим. |
| 🟡 | Migration 0007 platform-column breaks phase-5/6 history | Migration backfills `platform='telegram'` для всех existing rows. |
| 🟢 | prometheus_client bump breaks exporter | Pin `>=0.20,<1.0`. |
| 🟢 | FastAPI / uvicorn footprint (~30 MB venv delta) | Acceptable; admin-panel opt-in default. |

## Порядок фаз

Phase 9 — **финальный phase в README §Порядок фаз** (line 45). После его закрытия:

1. README помечается как "phase 9 closed — feature-complete for single-owner".
2. Release decision: tag `v1.0.0`, написать `RELEASE.md` с установкой / миграцией / known caveats.
3. Любой дальнейший phase (10+) — не в README scope; либо отдельный feature-branch (TTS, Docker, admin-write, encryption-at-rest), либо maintenance track.

Yandex-направление (Wave C) намеренно вынесено последним — если scope давит, можно закрыть phase 9 с ТОЛЬКО Wave A + B + D и оставить Yandex на phase 10 optional.

## Развилки для Q&A (before detailed-plan)

- **Q1 — Metrics backend:** Prometheus-pull (HTTP scrape) vs StatsD-push? Default: Prometheus-pull.
- **Q2 — Admin panel framework:** FastAPI (pydantic-native) vs aiohttp (lighter). Default: FastAPI.
- **Q3 — Admin panel auth:** basic-auth (bcrypt hash в env) vs GitHub OAuth (reuse phase-8). Default: basic-auth.
- **Q4 — Yandex приоритет:** include в phase 9 Wave C vs defer к phase 10. Default: Wave C included but "can be cut".
- **Q5 — `/health` shape:** plain text `OK` vs structured JSON. Default: JSON + `/livez` alias.
- **Q6 — Log format default:** structlog JSON vs console-friendly plain-text. Default: `console` в dev, `json` требует explicit env.
- **Q7 — Metrics exporter process:** in-process FastAPI side-mount vs separate daemon. Default: in-process.
- **Q8 — Admin panel deployment gating:** always-on-localhost vs opt-in через `ADMIN_ENABLED`. Default: opt-in.
- **Q9 — `prometheus_client` dep:** accept new dep vs hand-rolled text exporter. Default: accept.
- **Q10 — Admin panel log tail:** SSE live tail vs refresh-page. Default: refresh-page.
- **Q11 — Scheduler tick-lag histogram boundaries:** default `[10, 50, 100, 500, 1000, 5000]` ms. Q&A-able.
