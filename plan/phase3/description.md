# Phase 3 — Skill-creator (marketplace) & skill-installer

**Цель:** бот умеет нативно расширять себя двумя путями: (1) модель сама пишет новый скилл через встроенный Write tool, опираясь на Anthropic's `skill-creator` skill (берётся из публичного marketplace); (2) пользователь/модель ставит готовый скилл по URL или из marketplace.

**Вход:** phase 2 (ClaudeBridge + hooks + manifest cache + `invalidate_manifest_cache()` + `touch_skills_dir()` API + sandboxed Write path-guard).

**Выход:** два рабочих сценария через бот:
1. "сделай скилл для погоды" → модель подтягивает `skills/skill-creator/SKILL.md` (установленный при auto-bootstrap из Anthropic marketplace) → через Write пишет `skills/weather/SKILL.md` + `tools/weather/main.py` (Write sandboxed phase 2) → PostToolUse hook touch'ит `data/run/skills.dirty` → следующий же запрос видит новый скилл в manifest.
2. "какие есть готовые скилы?" → модель зовёт `skill-installer marketplace list` → JSON-список → пользователь выбирает → `skill-installer marketplace install NAME` → preview → "да" → atomic copy → готово. Либо тот же flow с произвольным URL: `skill-installer install <URL>`.

**Источник marketplace:** Anthropic's skills лежат в подкаталоге `skills/` своего репо. API-путь — `/repos/anthropics/skills/contents/skills` (не корень), а tree-URL-шаблон — `https://github.com/anthropics/skills/tree/main/skills/<name>/`. Hardcoded в `tools/skill-installer/_lib/marketplace.py` (`MARKETPLACE_URL`, `MARKETPLACE_BASE_PATH="skills"`).

## Задачи

- **PostToolUse tool-invocation enforcement** — блокер для phase 4. Обеспечивает что Claude реально выполняет Bash/tool command из SKILL.md body, НЕ просто читает body и отвечает текстом. Кандидаты: `UserPromptSubmit` hook re-injection, `PostToolUse(Skill)` → `PreToolUse(Bash)` verification, ИЛИ архитектурный pivot на `@tool`-decorator custom tools для CLI tools. См. `plan/phase2/known-debt.md#D1`.

1. **`tools/skill-installer/main.py` + `_lib/`** — CLI установки по URL **и** из marketplace. Подкоманды:
   - `preview <URL>` / `install --confirm --url <URL>` — базовый flow fetch → static-validate → preview → re-fetch → SHA-compare → atomic copy. Cache-by-URL (`sha256(canonical_url)[:16]`), TOCTOU-защита через повторный fetch + сравнение `sha256_of_tree(bundle)`; расхождение → `exit 7` "source changed since preview".
   - `marketplace list` — скачивает index публичного Anthropic-маркета (`MARKETPLACE_URL = "https://github.com/anthropics/skills"`, hardcoded; `gh api /repos/anthropics/skills/contents/skills`), возвращает JSON `[{name, description}, ...]`.
   - `marketplace info NAME` — fetch SKILL.md конкретного скилла (`/repos/anthropics/skills/contents/skills/<NAME>/SKILL.md`) + краткое превью.
   - `marketplace install NAME` — shortcut: строит tree-URL `https://github.com/anthropics/skills/tree/main/skills/<NAME>/` и делегирует `install`.
   - `status NAME` — прогресс асинхронного `uv sync` (Q8).
2. **Auto-bootstrap Anthropic's `skill-creator` (fire-and-forget).** В `Daemon.start()` после `ensure_skills_symlink` стартуем фоновую задачу через `asyncio.create_task(self._bootstrap_skill_creator_bg())` — **без** `await`. Задача сама проверяет наличие `skills/skill-creator/` и, если нет, зовёт `python tools/skill-installer/main.py marketplace install skill-creator --confirm` с внутренним таймаутом 120 сек. Любой исход (`ok` / `failed` / `timeout` / `exception`) уходит в `log.info` / `log.warning`; главный flow бота и `Daemon.start()` никогда не ждут bootstrap. Owner в Telegram про это не видит.
3. **`skills/skill-installer/SKILL.md`** — описание для модели как вызывать CLI (preview+confirm flow, marketplace subcommands, примеры диалогов). `skills/skill-creator/` ставится автоматически при первом старте — не кладём в репо.
4. **URL detector в `handlers/message.py`** — регексп на http(s)/git@; если URL найден — одноразовая system-note ("пользователь прислал URL X; возможно хочет поставить скилл — проверь через `skill-installer preview`, иначе игнорируй").
5. **Расширение Bash-allowlist в `bridge/hooks.py`** (phase 2 `_BASH_PROGRAMS`):
   - `git clone --depth=1 <https-url> <dest>` — schema-check + path-guard на dest.
   - `uv sync --directory tools/<name>` — path-guard.
   - `gh api <endpoint>` — **read-only**: разрешён только GET (дефолт), endpoint должен matched'иться на `/repos/.../contents/...` или `/repos/.../tarball/...`; любые `-X POST/PATCH/DELETE/PUT` / другие endpoints → deny.
   - `gh auth status` — read-only, allow.
   - Остальные `gh` и `git` подкоманды остаются закрытыми.
   - Требуется `gh` на хосте — задокументировано в README; при отсутствии `shutil.which("gh")` в `Daemon.start()` логируем `log.error` и отключаем marketplace-функционал, CLI в целом остаётся живым.
6. **PostToolUse hook для auto-sentinel.** В `bridge/hooks.py::make_posttool_hooks(data_dir)` возвращаем `[HookMatcher(matcher="Write"), HookMatcher(matcher="Edit")]`: если `file_path` лежит внутри `skills/` или `tools/` — `touch data/run/skills.dirty`. Это полностью заменяет необходимость CLI `skill-creator` самому дёргать sentinel: любой Write/Edit модели в skills/tools авто-инвалидирует кэш. `ClaudeBridge._build_options` мержит PreToolUse + PostToolUse hook'и.
7. **Hot-reload скилов через sentinel-файл.** `ClaudeBridge._render_system_prompt` в начале каждого query чекает `data/run/skills.dirty` → `invalidate_manifest_cache()` + `touch_skills_dir()` + `unlink(sentinel)`.
8. **Тесты:** marketplace list/install happy-path (mock `gh api` subprocess), installer git-mock, installer ssrf-deny, path-traversal, size limits, URL detector, PostToolUse hook → sentinel, auto-bootstrap (mock subprocess → skill-creator появляется), bash allowlist для `gh api` и `git clone`.

## Критерии готовности

- Диалог "сделай скилл echo" → модель через Anthropic's `skill-creator` (установленный при бутстрапе) пишет `skills/echo/SKILL.md` + `tools/echo/main.py` через Write → PostToolUse hook touch'ит sentinel → следующий turn видит `echo` в `{skills_manifest}` system prompt'а.
- Диалог "какие есть скилы в marketplace" → `marketplace list` → JSON со списком → пользователь выбирает → `marketplace install NAME` → preview → "да" → установлено → виден в manifest.
- Auto-bootstrap **не блокирует `Daemon.start()`** — старт завершается за <2 сек независимо от доступности GitHub; `skills/skill-creator/` появляется асинхронно. Fail → `log.warning skill_creator_bootstrap_failed` (или `_timeout` / `_exception`), бот продолжает работать.
- PostToolUse hook: Write в `skills/test/SKILL.md` → `data/run/skills.dirty` существует сразу после возврата Write'а.
- Bash allowlist:
  - `gh api /repos/anthropics/skills/contents/skills` → allow.
  - `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md` → allow.
  - `gh auth status` → allow.
  - `gh api -X DELETE /repos/x/y` → deny.
  - `gh pr create ...` → deny.
  - `git clone --depth=1 https://github.com/x/y skills/y` → allow; `git clone --depth=1 https://... /tmp/x` → deny (path escape).
- Вредоносный SKILL.md с `../../../etc/passwd` — отклонён validator'ом.
- Bundle содержит symlink (любой, даже указывающий внутрь bundle'а) — отклонён validator'ом до `copytree`.
- TOCTOU detection: `preview <URL>` → bundle v1 → изменить источник → `install --confirm --url <URL>` → re-fetch видит bundle v2 → SHA-compare fail → `exit 7` с сообщением "bundle on source changed since preview".
- URL на `http://169.254.169.254/latest/meta-data` — SSRF guard режет fetch up-front.
- `<data_dir>/run/` sweeper при старте: `tmp/` >1ч и `installer-cache/` >7д удаляются.

## Зависимости

Phase 2 (manifest cache + `invalidate_manifest_cache()` + Bash/file/WebFetch hooks + Write path-guard).

## Явно НЕ в phase 3

- Собственный `tools/skill-creator/` CLI — вместо него Anthropic's skill + Write.
- Inline-keyboard callback_query для preview confirm — phase 8 (ops polish).
- Оффлайн-кэш marketplace index.
- Множественные marketplaces — hardcoded один (`anthropics/skills`).
- `gh auth login` / auth flows — используем только read-only unauthenticated `gh api` (60 req/hour лимит).
- Sandbox для runtime выполнения установленных tools — future work.

## Риск

**Высокий (security).** Установка чужого кода = исполнение в Bash под полными правами модели. Phase 2 уже задизайнил strict argv-allowlist; сам акт создания файла (Write через Anthropic's skill-creator) sandboxed в `project_root`. Marketplace (`anthropics/skills`) — trust Anthropic (acceptable baseline); произвольные user-URL'ы — требуют preview+confirm. Митигации: preview+confirm обязателен; повторный fetch + SHA-compare при `install` (TOCTOU); static validate (AST + frontmatter schema); **symlinks в bundle отклоняются безусловно** до `copytree`; SSRF guard; size/count/timeout limits; schemes whitelist; `gh` read-only; `git clone --depth=1` без LFS; `gh` не установлен → marketplace-функционал отключается, бот живой.

**Известный baseline:** у Anthropic'ового `skill-creator` SKILL.md в frontmatter **нет** `allowed-tools`. Наш loader трактует отсутствие поля как "permissive" (передаёт полный набор `["Bash","Read","Write","Edit","Glob","Grep","WebFetch"]` в SDK) + `log.warning skill_permissive_default`. Per-skill allowed-tools НЕ единственная защита: phase 2 PreToolUse hooks (path-guard, Bash allowlist, WebFetch SSRF) срабатывают независимо от manifest'а и остаются универсальным defense-in-depth.
