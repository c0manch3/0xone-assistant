# Phase 7 — Summary

Документ подводит итоги завершённой фазы 7 проекта `0xone-assistant`
(media pipeline: voice/photo/document in; PNG/PDF/DOCX/MP3 out). Источники:
`plan/phase7/{description,detailed-plan,implementation,spike-findings,
wave-plan}.md`, исходники `src/assistant/{media,adapters,bridge,handlers,
scheduler,subagent}/` + `tools/{transcribe,genimage,extract_doc,render_doc}/`,
**~50 коммитов** (`005680a → 22bb3a6`) поверх phase-6 HEAD, **1200 passed
/ 4 skipped / 7 xfailed / 1 xpassed** на HEAD `22bb3a6`. Lint + mypy
strict зелёные на новых модулях.

**Post-phase fix-pack (6 commits поверх `465af0e`):** адресованы
CR/devil-advocate issues C1 (main-turn dispatch_reply wiring), D2
(DOCTYPE/ENTITY pre-parse reject), I4 (per-chat throttle lock TOCTOU),
I3+I7 (shared CLI path-guards), D3 (cloud-sync folder guard), I2+I5+
I6+D5 (micro-fixes cluster). Xfail count после fix-pack: **7 → 5**
(X-3 и X-4 ниже — closed). Тесты 1200+ passed.

## 1. TL;DR

Phase 7 расширяет приёмный/выходной контур бота с чисто-текстового до
мультимедийного. Тяжёлая вычислительная нагрузка (mlx-whisper, mflux)
живёт на хостовом Mac и доступна VPS-демону через SSH reverse tunnel —
VPS поднимает только тонкие HTTP-клиенты (`tools/transcribe`,
`tools/genimage`). Локальная обработка документов — `tools/extract_doc`
(pypdf/python-docx/openpyxl/striprtf + defusedxml zip-bomb guard) и
`tools/render_doc` (fpdf2 + vendored DejaVu / python-docx). Outbound
артефакты детектятся `dispatch_reply` по regex v3 + path-guard,
дедуплицируются `_DedupLedger` (300 s TTL, ключ
`(chat_id, resolved_outbox_path)`) и доставляются через
`adapter.send_photo/document/audio`. Долгие задачи (>30 s)
делегируются в worker subagent через phase-6 picker; SubagentStop hook
использует тот же `dispatch_reply`. Retention sweeper чистит
`media/inbox/` (14 d) и `media/outbox/` (7 d) + LRU cap 2 GB.

## 2. Что поставлено (waves 1–12 обзор)

**Wave 1 (commit 1)** — Spike 0 multimodal envelope + spike findings.
Inline base64 `image/jpeg/png/webp` для SDK 0.1.59 PASS на 3/5/10 MB
padded JPEG. Fallback `MEDIA_PHOTO_MODE=path_tool` documented.

**Wave 2 (commit 2)** — `_memlib` → `_lib` полный рефакторинг (Q9a
tech-debt close). Все phase-4/5/6 tools (memory/schedule/skill_installer/
task) переехали на relative imports через `tools/__init__.py`. Старая
`sys.path.insert(...)` паутина удалена.

**Wave 2b (commit 2b)** — root `pyproject.toml` +9 deps:
`Pillow>=10.4,<13` (CVE floor H-8), `pypdf>=4.0,<7`, `python-docx>=1.0,<2`,
`openpyxl>=3.1,<4`, `striprtf>=0.0.28`, `defusedxml>=0.7`,
`fpdf2>=2.7,<3`, `lxml>=5.0,<7`. S-8 manylinux_2_28 wheels verified;
`uv sync` не триггерит source build. +48.75 MB venv delta (S-4).

**Wave 3 (commits 3, 4)** — `MediaSettings(BaseSettings)` с
`env_prefix="MEDIA_"` (~50 полей: photo/voice/audio/document caps,
transcribe/genimage endpoints, retention, sweep interval). Добавлен
`MediaAttachment` dataclass, `IncomingMessage.attachments:
tuple[MediaAttachment, ...] | None`, абстрактные
`MessengerAdapter.send_photo / send_document / send_audio`.

**Wave 4 (commits 7–10, parallel ×4)** — четыре CLI-инструмента:
- `tools/transcribe/main.py` (~230 LOC stdlib-only + `_net_mirror.py`
  loopback guard) + `SKILL.md`. Multipart POST на
  `http://127.0.0.1:9100/transcribe`.
- `tools/genimage/main.py` (~430 LOC) + flock-locked JSON daily quota +
  `SKILL.md`. POST на `http://127.0.0.1:9101/generate`. Cap = 1/сутки
  UTC по умолчанию (MEDIA_GENIMAGE_DAILY_CAP override).
- `tools/extract_doc/main.py` (~350 LOC, `pypdf`/`python-docx`/`openpyxl`/
  `striprtf` + `defusedxml.defuse_stdlib()` + zip-bomb guard 64 MB cap)
  + `SKILL.md`.
- `tools/render_doc/main.py` (~260 LOC, `fpdf2` + vendored DejaVu TTF
  в `tools/render_doc/_lib/` / `python-docx` / stdlib txt fallback)
  + `SKILL.md`. Write-first stage-dir (`<data_dir>/run/render-stage/`)
  + outbox (`<data_dir>/media/outbox/`) path-guard.

**Wave 5 (commits 5, 6, parallel ×2)** — `src/assistant/media/`
sub-package: `paths.py` (inbox/outbox/stage helpers), `download.py`
(`_SizeCappedWriter` с aiogram 3.26 streaming cap — C-3 audit confirmed
write+flush path), `sweeper.py` (two-phase age+LRU), `artefacts.py`
(v3 regex — 43/46 из S-2 corpus, 3 documented xfails). Commit 6
shipped `adapters/dispatch_reply.py` + `_DedupLedger` (TTL=300 s,
LRU cap 256, in-memory per-Daemon).

**Wave 6 (commit 11)** — Bash allowlist расширен: `_validate_transcribe_argv`,
`_validate_genimage_argv`, `_validate_extract_doc_argv`,
`_validate_render_doc_argv`. `make_pretool_hooks(project_root,
data_dir: Path | None = None)` — `data_dir` keyword-optional для
forward-compat с phase-3/5/6 тестами.

**Wave 7 (commits 12, 13, parallel ×2)** — TelegramAdapter media
handlers (`_on_voice`, `_on_photo`, `_on_document`, `_on_audio`,
`_on_video_note`) + `send_photo/document/audio` с `TelegramRetryAfter`
retry wrapping (L-21). Attachment-ingress dedup на
`IncomingMessage.attachments` (I-7.6). Handler + bridge multimodal
envelope (safe pseudocode S-7 — per-attachment try/except); path_tool
branch (C-4); turn-id placeholder в history (H-10).

**Wave 8 (commits 14, 15, parallel ×2)** — `SchedulerDispatcher._deliver`
switched to `dispatch_reply`. `subagent/hooks.py::on_subagent_stop`
switched to `dispatch_reply`. Факторная сигнатура
`make_subagent_hooks` **дропнула параметр `outbox_root`** (H-11) —
теперь derived внутри Stop-hook closure через
`outbox_dir(settings.data_dir)` на каждый fire.

**Wave 9 (commit 16)** — `Daemon.start` integration:
`ensure_media_dirs()` ПЕРЕД `_spawn_bg(media_sweeper_loop(...))`
(pitfall #14); `_DedupLedger` plumbing в
`adapter._dedup_ledger` + `make_subagent_hooks(...)`.

**Wave 10 (commit 17)** — Integration E2E тесты: voice→transcribe,
photo→inline_base64, document→extract, scheduler→send_photo,
double-delivery race. SDK-calling тесты gated `RUN_SDK_INT=1`.

**Wave 11 (commits 18a–18l, 3 partitions ×4 parallel)** — cross-cutting
unit-test top-up:
- 11.A: H-13 SKILL.md assertion, `is_loopback_only` 11-case integration,
  sweeper concurrency, cyrillic filename round-trip.
- 11.B: partial-send failure isolation, scheduler race dedup,
  history multi-image placeholders, genimage quota midnight rollover.
- 11.C: TelegramAdapter oversize reject, defusedxml zip-bomb,
  render_doc path guard matrix, Daemon sweeper stop ordering.

**Wave 12 (commit 19)** — эта документация: description §82 Pillow
correction + SKILL.md §4.5 dedup polish + summary.md.

## 3. Инварианты для сохранения в phase 8

| # | Инвариант | Файл / модуль | Основание |
|---|-----------|---------------|-----------|
| I-7.1 | `_DedupLedger` TTL = **300 s**, LRU cap = **256**. Ledger **in-memory, per-Daemon** — НЕ переживает рестарт демона. Изменение TTL меняет гарантии pitfall #9 / H-12 тестов. | `src/assistant/adapters/dispatch_reply.py:69` (`_DEDUP_TTL_S`) | plan §2.6, L-19 |
| I-7.2 | Media retention defaults: `retention_inbox_days=14`, `retention_outbox_days=7`, `retention_total_cap_bytes=2_147_483_648` (2 GB), `sweep_interval_s=3600`. Sweeper two-phase: age-based первый, LRU второй. `outbox/` evicted ПЕРЕД `inbox/` в LRU phase (inbox = user-uploaded, дорого восстановить). | `src/assistant/config.py:192-195`, `src/assistant/media/sweeper.py` | Q6 locked |
| I-7.3 | `MediaSettings.photo_mode` default = `"inline_base64"` (S-0 PASS). Fallback `"path_tool"` ЕСТЬ в handler (C-4 — explicit `elif` branch, не silent no-op). | `src/assistant/config.py:161`, `src/assistant/handlers/message.py` | pitfall #16 |
| I-7.4 | `make_subagent_hooks` сигнатура: `(*, store, adapter, settings, pending_updates, dedup_ledger)`. **`outbox_root` НЕ параметр** — derived внутри Stop-hook closure via `outbox_dir(settings.data_dir)` на каждый fire (H-11). Добавление `outbox_root` обратно = регрессия (snapshot-drift risk). | `src/assistant/subagent/hooks.py:71-97` | H-11 |
| I-7.5 | `make_pretool_hooks(project_root, data_dir: Path \| None = None)` — `data_dir` **keyword-default `None`** для backward-compat с 9 phase-3/5/6 test call sites. При `None` Bash hook отказывает `tools/render_doc/main.py` outright. | `src/assistant/bridge/hooks.py:1386-1400` | Wave 6 commit 11 |
| I-7.6 | `_ARTEFACT_RE` v3 — pre-lookahead `(?<![\w/.:])` + post-lookahead `(?=[\s\`"'<>()\[\].,;:!?/]|$)`. Матчит 43/46 S-2 corpus; 3 documented xfails (adjacency edge cases). Space-after-colon rule (H-13) в SKILL.md + `bridge/system_prompt.md` — регекс **намеренно не матчит `что-то:/abs/outbox/...` без пробела** (защита от URL-scheme false positives). | `src/assistant/media/artefacts.py`, `src/assistant/adapters/dispatch_reply.py` | pitfall #2, #18 |
| I-7.7 | Attachment-ingress dedup в `TelegramAdapter`: `IncomingMessage.attachments` нормализован на уникальные `local_path` до dispatch. Handler loop предполагает invariant; re-dedup на handler-level скрывал бы adapter-bugs. | `src/assistant/adapters/telegram.py` | pitfall #17, I-7.6 |
| I-7.8 | `SizeCappedWriter` реализует BOTH `write(data: bytes) -> int` AND `flush() -> None` (aiogram 3.26 source audit: `__download_file_binary_io` вызывает оба per chunk). Caller catches `SizeCapExceeded`, `unlink(missing_ok=True)` partial file, re-raises. | `src/assistant/media/download.py` | pitfall #3, C-3 |
| I-7.9 | `_is_loopback_only(url)` — narrower than phase-3 `classify_url`. CLI требует DNS-resolution every адрес = loopback (127.0.0.0/8 / ::1). 10.x / 192.168.x / 169.254.x — exit 2 / 3. Mirror in `tools/{transcribe,genimage}/_net_mirror.py`. | phase-3 `classify_url` + `_net_mirror.py` | pitfall #5, S-1 |

## 4. Known xfails (production bugs → phase-8 fix-pack candidates)

| # | Test | Location | Bug | Status |
|---|------|----------|-----|--------|
| X-1 | `test_wrong_shape_list_payload_recovers` | `tests/test_genimage_quota_midnight_rollover.py:378` | `_check_and_increment_quota` вызывает `state.get("date")` без проверки, что JSON распарсился в dict. List/scalar-shaped quota-file (rare: disk fill mid-write / operator hand-edit) крашит с `AttributeError`. Диагностический `_read_quota_best_effort` ЭТО обрабатывает — asymmetry = bug. | `tools/genimage/main.py:331` | xfail(strict=True) — **phase-8 fix-pack** |
| X-2 | `test_best_effort_reader_binary_input_xfail` | `tests/test_genimage_quota_midnight_rollover.py:413` | `_read_quota_best_effort` catches `OSError` + `JSONDecodeError` но **НЕ `UnicodeDecodeError`**. Quota file с arbitrary binary bytes (partial fsync after crash) leak'ит exception к caller'у. Locked write path корректно обрабатывает; диагностический reader должен совпасть. | `tools/genimage/main.py:355` | xfail(strict=True) — **phase-8 fix-pack** |
| ~~X-3~~ | ~~`test_xxe_docx_rejected_by_defusedxml`~~ | ~~`tests/test_extract_doc_defusedxml_zip_bomb.py:233`~~ | **CLOSED** fix-pack D2 (commit `ff924e7`). `_reject_xml_entity_declarations` сканит каждую XML-часть в DOCX/XLSX zip на `<!DOCTYPE` / `<!ENTITY` markers BEFORE парсера — rejection через `EXIT_VALIDATION` (3). | ~~`tools/extract_doc/main.py`~~ | passed |
| ~~X-4~~ | ~~`test_billion_laughs_docx_rejected_by_defusedxml`~~ | ~~`tests/test_extract_doc_defusedxml_zip_bomb.py:297`~~ | **CLOSED** fix-pack D2 (same commit). Same bytes-level `<!ENTITY>` scan catches billion-laughs declarations. | ~~`tools/extract_doc/main.py`~~ | passed |

Плюс унаследованные xfails from earlier phases (1 xpassed на HEAD —
один из ранее ожидаемо-fail'ящихся теперь проходит, следует проверить
и снять xfail если поведение стабилизировалось).

## 5. Caveats / caveat-fields для phase 8

- **Yandex Disk sync:** vault-пути (`<vault_dir>/`) могут жить на смонтированном
  Yandex Disk (или другой облачной синхронизации). Sweeper работает с
  `<data_dir>/media/` — НЕ vault — так что коллизий нет, но любые
  phase-8 фичи, которые пишут большие binary в vault (photo-memory,
  audio-memory), должны учитывать sync-delay + возможный conflict на
  rename (sweeper использует `unlink(missing_ok=True)` — POSIX-safe,
  на sync-backed mount'ах может проявиться race).
- **Obsidian vault:** `memory` skill пишет только markdown — binary
  артефакты НЕ уходят в vault по дизайну (Q8 phase 7). Phase-8 GitHub
  skill планирует daily `git commit` vault'а — если phase 8 добавит
  artefact-linking в markdown, нужно убедиться, что ссылки на outbox
  не ломают vault после sweeper-eviction (7 d outbox retention vs
  бесконечный git history).
- **SSH reverse tunnel:** `whisper-server` (9100) / `flux-server` (9101)
  НЕ shipятся в repo. Owner поднимает на Mac руками. CLI graceful
  fail exit 4 — phase-8/9 auto-reconnect tunnel не в scope.
- **Pillow CVE monitoring:** Pillow на hot path (photo decode + fpdf2
  render). Root `pyproject.toml` pin `>=10.4,<13` — review CVE feed
  перед upgrade (H-8 pitfall #1).
- **Daily cap genimage = 1 UTC:** NTP rollback через полночь даёт
  +1 jitter (S-5 R-4 known edge). Документировано в `SKILL.md`,
  НЕ фиксим — принято operational noise.
- **Photo history replay:** `[image: <abs path>]` placeholder **не** конвертируется
  обратно в `image_block` при history replay — если photo deleted
  sweeper'ом, turn-replay увидит placeholder без реального inline.
  Acceptable для phase 7; phase-8 memory-of-photo может пересмотреть.
- **1 xpassed в тестах:** указывает что какой-то из исторических
  xfail'ов теперь PASS'ит. Phase-8 владелец должен идентифицировать
  и либо снять xfail, либо pin версию зависимости, которая
  восстанавливает исходное поведение.
- **HISTORY_MAX_SNIPPET_TOTAL_BYTES cap:** phase-4 carryover — всё
  ещё отложен на phase 9. Photos добавляют placeholder
  (`[image: <path>]`) в history — byte-cost negligible, но формальное
  закрытие долга остаётся.

## 6. Метрики

**Тесты:** 765 (phase-6 HEAD) → **1200 passed, 4 skipped, 7 xfailed,
1 xpassed** (+435 tests net). 20+ новых тест-файлов на phase 7.

**Коммиты phase 7:** **~50** (`005680a..22bb3a6`): 16 plan/wave commits,
4 Wave-4 CLI, 2 Wave-5 media/dispatch, 1 Wave-6 bash allowlist,
2 Wave-7 adapter+handler, 2 Wave-8 dispatcher switches, 1 Wave-9
Daemon integration, 1 Wave-10 E2E, 12 Wave-11 unit-test top-up,
1 Wave-12 documentation.

**LOC исходников phase 7:**
- `src/assistant/media/` — **NEW 4 модуля** (`paths.py`, `download.py`,
  `sweeper.py`, `artefacts.py`).
- `src/assistant/adapters/dispatch_reply.py` — **NEW** (~330 LOC).
- `tools/{transcribe,genimage,extract_doc,render_doc}/main.py` — **NEW
  4 CLI + SKILL.md** (~1300 LOC + 600 LOC docs).
- Edits в `src/assistant/`: `adapters/telegram.py` (media handlers),
  `handlers/message.py` (multimodal envelope), `bridge/hooks.py`
  (4 new allowlist validators + `data_dir` keyword), `bridge/claude.py`
  (image_block content), `config.py` (MediaSettings), `main.py`
  (Daemon integration), `scheduler/dispatcher.py` (dispatch_reply
  switch), `subagent/hooks.py` (dispatch_reply switch + H-11).

**LOC планирования phase 7:** `description.md` ~107,
`detailed-plan.md` 1048, `implementation.md` 1209, `spike-findings.md`
~620, `wave-plan.md` ~410 — **~3400 LOC** plan total.

**Deps added:** 8 (`Pillow`, `pypdf`, `python-docx`, `openpyxl`,
`striprtf`, `defusedxml`, `fpdf2`, `lxml`). Venv delta: +48.75 MB
(x86_64 manylinux_2_28).

**Spikes:** S-0 multimodal envelope, S-1 loopback-only narrower than
classify_url, S-2 ARTEFACT_RE v3 corpus, S-3 fpdf2 Cyrillic (→ Pillow
required correction), S-4 venv footprint, S-5 flock quota race, S-6
aiogram SizeCappedWriter audit, S-7 handler-envelope missing-photo
safe variant, S-8 manylinux_2_28 wheel availability.

**CI gates:** `uv sync` OK, `just lint` зелёный (ruff + format +
mypy src strict), `uv run pytest -q` — 1200 passed на HEAD `22bb3a6`.

**Calendar:** phase 7 pipeline — 2026-04-17 → 2026-04-18
(parallel waves reduced wall-clock).

## 7. Готовность к phase 8 (GitHub skill)

Phase 8 (GitHub skill + daily vault commit) разблокирован. Наследует:

- **dispatch_reply / _DedupLedger** — GitHub PR creation может
  отвечать артефактом (patch.diff) → дедуп сработает на
  `(chat_id, outbox_path/patch.diff)`.
- **Bash allowlist pattern** — новый `_validate_gh_argv` следует
  паттерну phase-3/7 (subcmd whitelist + dup-flag reject).
- **Write-first stage-dir** — `<data_dir>/run/` reserved для stage
  файлов (render_doc + memory уже используют); GitHub skill
  пусть придерживается того же паттерна.
- **SSH tunnel model** — GitHub API отличается (no tunnel), но
  endpoint SSRF guard через `classify_url` остаётся обязательным.

Унаследованный техдолг для phase 9:
- `HISTORY_MAX_SNIPPET_TOTAL_BYTES` cap (phase-4 carryover).
- Per-skill `allowed-tools` enforcement (Q9c).
- `_memlib` — **ЗАКРЫТ** в phase 7 Wave 2.
- 99 test-only mypy errors (phase-5 carryover).

Genimage + extract_doc fix-packs (X-1..X-4) — рекомендованы на
phase-8 startup или в виде отдельного phase-7.1 hotfix, если pre-deploy
audit найдёт X-3/X-4 эксплуатабельными.

---

Phase 7 закрыт. Media pipeline работает E2E: voice → transcribe CLI →
reply; photo → inline_base64 → model describes; document → extract →
summary; scheduler/worker → send_photo/document/audio через
dispatch_reply с двухслойной дедупликацией (prompt-rule +
`_DedupLedger`). Retention sweeper держит disk под 2 GB LRU + 14/7 d
age. Phase 8 (GitHub skill) разблокирован.

## 8. Fix-pack (post-merge, 6 commits поверх `465af0e`)

| Commit | Issue | Краткое описание | Тесты добавлены |
|--------|-------|-------------------|------------------|
| `0c94781` | **C1** (I-7.5 main-turn) | `TelegramAdapter._on_text` wiring → `dispatch_reply`. Main-turn outbox artefact'ы теперь доставляются как photo/document/audio + cleaned text. Общий `_DedupLedger` между three call-sites. | `tests/test_telegram_main_turn_dispatch_reply.py` (4 теста) |
| `ff924e7` | **D2** (XXE pre-parse) | extract_doc `_reject_xml_entity_declarations` — bytes-level `<!DOCTYPE`/`<!ENTITY>` scan по каждой XML-части zip'а ДО python-docx / openpyxl. Закрывает X-3 + X-4. | 2 ранее-xfail'ящихся теста теперь passed |
| `d5d3bee` | **I4** (throttle TOCTOU) | Per-chat `asyncio.Lock` в `subagent/hooks.py::_throttle`. Два concurrent Stop hook'а для одного chat'а больше не видят stale `last_notify_at`. | `tests/test_subagent_throttle_concurrent.py` (3 теста) |
| `d497d2a` | **I3 + I7** (path-guard drift) | `src/assistant/media/path_guards.py` — canonical `validate_existing_input_path` + `validate_future_output_path`. 4 CLI (transcribe/extract_doc/genimage/render_doc) делегируют. Исправлена genimage `resolve(strict=False)` regression (symlink parent escape). | `tests/test_path_guards_shared.py` (20 тестов) |
| `71ebdde` | **D3** (cloud-sync guard) | `_check_data_dir_not_in_cloud_sync` в `Daemon.start` — reject startup если `data_dir` резолвится под iCloud/Dropbox/Yandex/OneDrive/GDrive/CloudStorage. Opt-out через `<data_dir>/.nosync` sentinel. Exit code 4 (`DATA_DIR_SYNC_GUARD_FAIL_EXIT`). | `tests/test_daemon_data_dir_sync_guard.py` (6 тестов) |
| `a1570c0` | **I2 + I5 + I6 + D5** (micro-cluster) | I2: drop redundant inner TTL re-check в `_DedupLedger.mark_and_check` (dead after `_evict_expired`). I5: move `send_text`/`dispatch_reply` INSIDE `ChatActionSender.typing(...)` context (done in C1 commit). I6: `except (OSError, Exception)` → `except Exception` в `media/download.py`. D5: add `\u200B-\u200D` (ZWSP/ZWNJ/ZWJ) в `ARTEFACT_RE` body-forbid + stop-set. | 5 новых param-cases в `test_dispatch_reply_regex.py` |

**Xfail count:** `7 → 5` после fix-pack (X-3, X-4 closed; X-1, X-2
остаются как phase-8 candidates; плюс 3 regex S-2 adjacency residuals).

**Файлы добавлены:** `src/assistant/media/path_guards.py`, 4 новых
test-модуля (`test_telegram_main_turn_dispatch_reply.py`,
`test_subagent_throttle_concurrent.py`, `test_path_guards_shared.py`,
`test_daemon_data_dir_sync_guard.py`).

**Фикс-пак итого:** 34 новых теста, ~600 LOC нового кода,
6 bisectable commits. Mypy strict зелёный на HEAD.
