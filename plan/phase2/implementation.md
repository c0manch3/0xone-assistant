# Phase 2 — Implementation (spike-verified, 2026-04-15)

## Revision history

- **v1** (2026-04-15): initial after SDK spike (claude-agent-sdk 0.1.59, OAuth CLI 2.1.109). Empirical answers R1–R5.
- **v2** (2026-04-15): applied devil's advocate review — 5 blockers (thinking wiring склеен с `_build_options`; HookMatcher count 6 not 5; убран `dataclasses.replace`; явный `completed`-контракт между bridge и handler; Bash prefilter → allowlist-first с slip-guard) + 8 strategic gaps (U1/U2/U3/U5 xfail тесты, synthetic tool-note в history, project_root detection, XDG override + first-run mkdir, auth preflight для `claude` CLI, manifest mtime max по файлам, per-layer logging contract, WebFetch SSRF hook).

Этот документ — результат эмпирической верификации SDK API против `claude-agent-sdk==0.1.59` с реальным OAuth (Claude Code CLI 2.1.109). Все вопросы R1–R5 из `plan/phase2/detailed-plan.md §0 (Task 0 spike)` закрыты; неоднозначности между планом и реальным поведением SDK ниже зафиксированы explicitly как "detailed-plan верит X; реально → Y".

Полный раскладывает spike — `/Users/agent2/Documents/0xone-assistant/plan/phase2/spike-findings.md`. Артефакты-пробники — `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe{,2,3}.py` + `sdk_probe{,2,3}_report.json`. Coder **обязан** прочитать spike-findings перед началом.

Pinned-версии (known-good на 2026-04-15):

| Пакет | Пин | Примечания |
|---|---|---|
| `claude-agent-sdk` | `>=0.1.59,<0.2` | 0.1.59 — tested. API ещё минорный, пинаем верхнюю границу. |
| `pyyaml` | `>=6.0` | для парсинга frontmatter SKILL.md |
| `types-pyyaml` | `>=6.0` (dev) | mypy strict |
| Остальные пины | как в phase 1 | (aiogram 3.26, pydantic 2.9+, pydantic-settings 2.6, aiosqlite 0.20, structlog 25.1) |

**Auth:** OAuth через уже залогиненный `claude` CLI (`~/.claude/`). В `Settings` и `.env` **не появляется** `ANTHROPIC_API_KEY`. Если обнаружится в ревью — удалять.

---

## 1. Верифицированные решения (R1–R5 → итог)

| R | Вопрос | Final API | Источник |
|---|---|---|---|
| R1 | Multi-turn history | **`query(prompt=async_gen, options=...)`** где `async_gen` yield'ит `{"type":"user","message":{"role":"user","content": <str\|list[Block]>}, "parent_tool_use_id": None, "session_id": <stable>}` на каждый исторический user-turn + финальный. **НЕ** используем `resume=session_id` (наша БД — источник истины). Assistant-turn'ы НЕ переподаются отдельно; SDK восстанавливает контекст из user-turn'ов последовательного стрима. Tool-use/tool-result блоки отправляются внутри user `content=[...]` когда нужна непрерывность. | `spike-findings §R1`, `_internal/client.py:152` |
| R2 | ThinkingBlock | **`ClaudeAgentOptions(max_thinking_tokens=N, effort="high")`**. Ни `thinking={"type":"enabled",...}`, ни `extra_args={"thinking":...}` не работают: TypedDict требует `type`-literal, а CLI `--thinking` принимает только `enabled/adaptive/disabled` без бюджета. Блок: `.thinking: str`, `.signature: str`. В phase 2 thinking по дефолту **выключен** (нулевой бюджет); пишем в БД c `block_type="thinking"`, но **не переподаём обратно в SDK** (U2 в spike-findings — consider regression test). | `spike-findings §R2`, `sdk_probe3_report.json` |
| R3 | Skills discovery | **`ClaudeAgentOptions(cwd=project_root, setting_sources=["project"])`** достаточно. SDK автоподхватывает `.claude/skills/<name>/SKILL.md` (YAML frontmatter `name`, `description`, `allowed-tools: [Bash]`). `SystemMessage(subtype="init")` показывает key `skills` — логируем. `plugins=` НЕ нужен. Симлинк `.claude/skills → ../skills` — ожидается работает идентично real dir, но U3 (unverified) — coder должен smoke-tested'ть. | `spike-findings §R3`, пробник `probe_r3_setting_sources_and_skills` |
| R4 | Message stream | **`async for m in query(...)`** выдаёт: `SystemMessage(subtype="init")` → опциональный `RateLimitEvent` → один или несколько `AssistantMessage(content=list[Block], model)` → `ResultMessage(subtype, duration_ms, num_turns, total_cost_usd, usage, session_id, stop_reason, model)`. `AssistantMessage.content` — **уже собранный** список блоков (НЕ per-token). Для per-token — `include_partial_messages=True` (не используем). Bridge yield'ит block'и из `m.content` по порядку; handler на `ResultMessage` ловит meta для `turns.complete_turn(meta=...)`. | `spike-findings §R4` |
| R5 | Permission guard | **Используем `hooks={"PreToolUse": [HookMatcher(matcher=<name>, hooks=[fn])]}`, НЕ `can_use_tool`.** `can_use_tool` срабатывает только когда CLI иначе спросил бы пользователя — при `allowed_tools=[...]` callback молчит. Hook-сигнатура: `async def hook(input_data: dict, tool_use_id: str\|None, ctx: dict) -> dict`. `input_data` содержит `tool_name, tool_input, cwd, permission_mode, session_id, transcript_path, tool_use_id`. Deny: return `{"hookSpecificOutput": {"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"<msg>"}}`. Регистрируем **7 HookMatcher'ов**: **1 Bash + 5 file-tools (Read, Write, Edit, Glob, Grep) + 1 WebFetch** (SSRF guard, см. §2.1). v1 писал "5" — исправлено. Regex-matcher не верифицирован — U5, xfail test. | `spike-findings §R5`, `sdk_probe3.py::probe_pretooluse_hook` |

**Расхождения с detailed-plan, которые coder должен видеть:**

- Detailed-plan §8 `can_use_tool=_make_path_guard(...)` → **реально**: `hooks={"PreToolUse":[HookMatcher(...),...]}`. Секция `_make_path_guard` переписывается как hook.
- Detailed-plan §8 `_make_bash_pre_hook()` с неопределённой сигнатурой → **реально**: та же hook-сигнатура, deny-shape выше.
- Detailed-plan §2 `ClaudeSettings` без thinking — оставляем как есть. Если решим включить — поле `claude_thinking_tokens: int = 0`, прокидываем в `max_thinking_tokens=` только если >0.
- Detailed-plan §7 `history_to_sdk_messages(...) -> list[dict]` → **реально**: должен быть `async def` генератор (или `list[dict]` + обёртка async-gen в bridge). SDK читает из `AsyncIterable[dict]`.

---

## 2. Corrected code snippets

Фрагменты из `detailed-plan.md` phase 2, которые **меняются** после spike. Остальное (§1 pyproject diff, §2 Settings shape за исключением thinking, §4 bootstrap, §5 skills.py manifest+mtime cache, §6 system_prompt.md, §7a migration 0002, §9 ClaudeHandler skeleton, §10 Telegram emit+split, §11 SKILL.md, §12 tools/ping, §13 тесты — кроме тех что упомянут ниже) — берётся как есть из detailed-plan.

### 2.1 `src/assistant/bridge/claude.py` — ClaudeBridge финальный код (замещает detailed-plan §8)

**v2 изменения:** `_build_options` теперь единственное место конструирования `ClaudeAgentOptions`; `system_prompt` передаётся прямо в `__init__` (никакого `dataclasses.replace`); `thinking_budget>0` → `max_thinking_tokens`+`effort` wired условно; 7 HookMatcher'ов (Bash + 5 files + WebFetch SSRF); Bash guard — allowlist-first (§2.3); per-layer logging (§3.13).

```python
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, AsyncIterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    query,
)

from assistant.config import Settings
from assistant.logger import get_logger
from assistant.bridge.history import _history_to_user_envelopes
from assistant.bridge.skills import build_manifest

log = get_logger("bridge.claude")

# ---------------------------------------------------------------------------
# Bash prefilter — SEE §2.3. Allowlist-first is Recommended; regex is secondary.
# ---------------------------------------------------------------------------
_BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "uv run tools/",
    "git status",
    "git log",
    "git diff",
    "ls ",
    "ls\n",
    "pwd",
    "echo ",
    # "cat <path>" — special-cased below (path must be inside project_root).
)
_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"                       # octal escape minting bytes
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}"          # long base64-like blob
    r")",
    re.IGNORECASE,
)
_FILE_TOOLS = ("Read", "Write", "Edit", "Glob", "Grep")

# WebFetch SSRF deny list — private/metadata/link-local ranges. Defence-in-depth
# only; real network ACL belongs at OS/firewall layer.
_WEBFETCH_BLOCKED_HOSTS: tuple[str, ...] = (
    "localhost", "127.", "0.0.0.0", "169.254.", "10.",
    "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.",
    "[::1]", "[fc", "[fd",
)


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    """Return deny-reason iff cmd is NOT allowed. None → allow."""
    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if any(stripped.startswith(p) for p in _BASH_ALLOWLIST_PREFIXES):
        return None
    # Special-case `cat <path>` — allow iff path resolves inside project_root.
    if stripped.startswith("cat "):
        target = stripped[4:].strip().split()[0] if len(stripped) > 4 else ""
        if target and not target.startswith("-"):
            try:
                p = Path(target)
                resolved = (project_root / p).resolve() if not p.is_absolute() else p.resolve()
                if str(resolved).startswith(str(project_root.resolve())):
                    return None
            except OSError:
                pass
    return (
        "not in allowlist; if you genuinely need this, ask the owner to add it "
        "to a skill's allowed-tools with explicit approval."
    )


def _make_bash_hook(project_root: Path) -> Any:
    """Allowlist-first Bash guard with slip-guard regex as defence-in-depth."""
    async def bash_hook(input_data: dict, tool_use_id: str | None, ctx: dict) -> dict:
        cmd = (input_data.get("tool_input", {}) or {}).get("command", "") or ""
        reason = _bash_allowlist_check(cmd, project_root)
        if reason is not None:
            log.warning("pretool_decision", tool_name="Bash", decision="deny",
                        reason="allowlist", cmd=cmd[:200])
            return _deny(reason)
        if _BASH_SLIP_GUARD_RE.search(cmd):
            log.warning("pretool_decision", tool_name="Bash", decision="deny",
                        reason="slip_guard", cmd=cmd[:200])
            return _deny(
                "Command matched a secrets/encoded-payload pattern. "
                "Reading .env/.ssh/.aws/tokens/encoded blobs via Bash is blocked."
            )
        log.debug("pretool_decision", tool_name="Bash", decision="allow", cmd=cmd[:120])
        return {}
    return bash_hook


def _make_file_hook(project_root: Path) -> Any:
    root = project_root.resolve()
    async def file_hook(input_data: dict, tool_use_id: str | None, ctx: dict) -> dict:
        ti = input_data.get("tool_input", {}) or {}
        candidate = ti.get("file_path") or ti.get("path") or ti.get("pattern") or ""
        if not candidate:
            return {}
        try:
            p = Path(candidate)
            if p.is_absolute():
                resolved = p.resolve()
                if not str(resolved).startswith(str(root)):
                    log.warning("pretool_decision",
                                tool_name=input_data.get("tool_name"),
                                decision="deny", reason="outside_project_root",
                                path=str(resolved))
                    return _deny(f"Path outside project_root ({root}) is not allowed: {resolved}")
        except OSError as e:
            return _deny(f"invalid path {candidate!r}: {e}")
        return {}
    return file_hook


def _make_webfetch_hook() -> Any:
    async def webfetch_hook(input_data: dict, tool_use_id: str | None, ctx: dict) -> dict:
        ti = input_data.get("tool_input", {}) or {}
        url = (ti.get("url") or "").strip()
        if not url:
            return {}
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return _deny(f"malformed URL: {url!r}")
        raw = url.lower()
        for needle in _WEBFETCH_BLOCKED_HOSTS:
            if host.startswith(needle.rstrip(".").rstrip("]")) or needle in raw:
                log.warning("pretool_decision", tool_name="WebFetch",
                            decision="deny", reason="ssrf_private_ip", url=url[:200])
                return _deny(
                    f"WebFetch to private/link-local/metadata host is blocked: {host!r}."
                )
        return {}
    return webfetch_hook


class ClaudeBridgeError(RuntimeError):
    pass


class ClaudeBridge:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.claude.max_concurrent)

    def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
        """Single place where ClaudeAgentOptions is constructed.

        system_prompt passed straight into __init__ — no dataclasses.replace.
        thinking knobs (max_thinking_tokens + effort) are wired CONDITIONALLY
        based on settings.claude.thinking_budget:
          - thinking_budget == 0 → thinking disabled, kwargs not passed at all
          - thinking_budget >  0 → max_thinking_tokens=N, effort=<setting>
        """
        pr = self._settings.project_root
        hooks = {
            "PreToolUse": [
                HookMatcher(matcher="Bash", hooks=[_make_bash_hook(pr)]),
                *[HookMatcher(matcher=t, hooks=[_make_file_hook(pr)]) for t in _FILE_TOOLS],
                HookMatcher(matcher="WebFetch", hooks=[_make_webfetch_hook()]),
            ]
        }
        thinking_kwargs: dict[str, Any] = {}
        if self._settings.claude.thinking_budget > 0:
            thinking_kwargs["max_thinking_tokens"] = self._settings.claude.thinking_budget
            thinking_kwargs["effort"] = self._settings.claude.effort
        return ClaudeAgentOptions(
            cwd=str(pr),
            setting_sources=["project"],
            max_turns=self._settings.claude.max_turns,
            allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"],
            hooks=hooks,
            system_prompt=system_prompt,
            **thinking_kwargs,
        )

    def _render_system_prompt(self) -> str:
        template = (
            self._settings.project_root / "src" / "assistant" / "bridge" / "system_prompt.md"
        ).read_text(encoding="utf-8")
        manifest = build_manifest(self._settings.project_root / "skills")
        log.info("manifest_rebuilt")
        return template.format(
            project_root=str(self._settings.project_root),
            skills_manifest=manifest,
        )

    async def ask(
        self,
        chat_id: int,
        user_text: str,
        history: list[dict[str, Any]],
    ) -> AsyncIterator[Any]:
        """Yields Block instances, then a final ResultMessage, then returns.

        Contract with handler (see §2.4):
        - Handler MUST distinguish ResultMessage from blocks to decide
          complete_turn vs interrupt_turn.
        - Bridge yields ResultMessage as the last item, then returns cleanly
          (no raise on success).
        - If stream aborts (TimeoutError, any other Exception) — ResultMessage
          is NEVER yielded; handler's finally sees completed=False and calls
          interrupt_turn.
        """
        opts = self._build_options(system_prompt=self._render_system_prompt())
        log.info("query_start", chat_id=chat_id, prompt_len=len(user_text),
                 history_rows=len(history))

        async def prompt_stream() -> AsyncIterable[dict[str, Any]]:
            for row in _history_to_user_envelopes(history, chat_id):
                yield row
            yield {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "parent_tool_use_id": None,
                "session_id": f"chat-{chat_id}",
            }

        async with self._sem:
            try:
                async with asyncio.timeout(self._settings.claude.timeout):
                    async for message in query(prompt=prompt_stream(), options=opts):
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            log.info(
                                "sdk_init",
                                model=message.data.get("model"),
                                skills=list(message.data.get("skills") or []),
                                cwd=message.data.get("cwd"),
                            )
                            continue
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                log.debug("block_received", type=type(block).__name__)
                                yield block
                            continue
                        if isinstance(message, ResultMessage):
                            log.info(
                                "result_received",
                                model=message.model,
                                stop_reason=message.stop_reason,
                                cost_usd=message.total_cost_usd,
                                duration_ms=message.duration_ms,
                                num_turns=message.num_turns,
                                input_tokens=(message.usage or {}).get("input_tokens"),
                                output_tokens=(message.usage or {}).get("output_tokens"),
                            )
                            yield message
                            return
                        # SystemMessage(other), RateLimitEvent, UserMessage — skip.
            except TimeoutError as e:
                log.warning("timeout", chat_id=chat_id,
                            timeout_s=self._settings.claude.timeout)
                raise ClaudeBridgeError("timeout") from e
            except Exception as e:
                log.error("sdk_error", error=repr(e))
                raise ClaudeBridgeError(f"sdk error: {e}") from e
```

**Важные отличия от detailed-plan §8 и от v1:**

1. **`can_use_tool` удалён**, заменён на `hooks={"PreToolUse": [...]}` (R5 spike).
2. **7 HookMatcher'ов:** 1 Bash + 5 file-tools (Read/Write/Edit/Glob/Grep) + 1 WebFetch SSRF. v1 писал "5" — исправлено.
3. **`allowed_tools` задан явно** — без этого CLI каждый раз спрашивает разрешение (нет TTY → UB).
4. **Bash guard — allowlist-first (Recommended, см. §2.3), НЕ regex-only.** `_bash_allowlist_check` — primary deny; `_BASH_SLIP_GUARD_RE` — defence-in-depth.
5. **WebFetch SSRF hook:** deny на private/link-local/metadata hosts (AWS IMDS `169.254.169.254`, localhost, RFC1918, IPv6 ULA/loopback).
6. **File-hook читает три поля** (`file_path`, `path`, `pattern`). Blanket absolute-path check; relative paths допускаются.
7. **`prompt_stream()` — async generator.**
8. **`_build_options(system_prompt=...)` — единственное место конструирования ClaudeAgentOptions**, `system_prompt` передаётся прямо в `__init__`. **Никакого `dataclasses.replace`** — v1 использовал его, spike подтвердил что ClaudeAgentOptions это dataclass (`dataclasses.fields(ClaudeAgentOptions)` работает в `probe_options_fields`), но прямая передача ближе к sample-коду `sdk_probe*.py` и надёжнее.
9. **Thinking wiring склеен:** `thinking_budget > 0` → `max_thinking_tokens`+`effort` передаются через `**thinking_kwargs`. Иначе — не передаются вовсе. v1 имел оторванные снippets в §2.5.
10. **ResultMessage yield'ится как last item, затем bridge `return`'ится без raise.** Контракт с handler: `isinstance(item, ResultMessage) → complete_turn`. Stream abort (Timeout/Exception) → ResultMessage не yield'ится → handler видит `completed=False` → `interrupt_turn`. См. §2.4 handler skeleton.
11. **Per-layer logging** (§3.13 contract): `query_start`, `sdk_init`, `block_received` (debug), `result_received`, `timeout` (warning), `sdk_error` (error), `pretool_decision`, `manifest_rebuilt`.

### 2.2 `src/assistant/bridge/history.py` (заменяет detailed-plan §7 `history_to_sdk_messages`)

```python
from __future__ import annotations

from typing import Any, Iterator


def _history_to_user_envelopes(
    rows: list[dict[str, Any]], chat_id: int
) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows → SDK user-envelope stream.

    Per spike R1: history is fed as a sequence of `"type":"user"` envelopes.
    Assistant turns do NOT need to be emitted back — SDK reconstructs context
    from user envelopes in stream order. Tool-use/tool-result blocks that
    accompanied an assistant turn are folded into the NEXT user envelope's
    content list so the SDK sees the full round-trip.

    Skips rows with block_type='thinking' (R2: SDK refuses cross-session thinking).

    Phase 2 simplification (U1 unverified): we SKIP tool_use/tool_result replay
    и ThinkingBlock (R2). Но чтобы модель не галлюцинировала повтор уже
    выполненной tool-работы, в первый user-envelope turn'а с tool-активностью
    ВСТРАИВАЕМ synthetic system-note:

        [system-note: в прошлом ходе были вызваны инструменты: <names>. Результаты получены.]

    Язык — русский (бот по умолчанию).
    """
    session_id = f"chat-{chat_id}"
    by_turn: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        if row.get("block_type") == "thinking":
            continue
        turn_id = row["turn_id"]
        if turn_id not in by_turn:
            by_turn[turn_id] = []
            order.append(turn_id)
        by_turn[turn_id].append(row)

    for turn_id in order:
        user_texts: list[str] = []
        tool_names: list[str] = []
        for row in by_turn[turn_id]:
            if row["role"] == "user":
                for block in row["content"]:
                    if block.get("type") == "text":
                        user_texts.append(block["text"])
            elif row.get("block_type") == "tool_use":
                for block in row["content"]:
                    name = block.get("name")
                    if name and name not in tool_names:
                        tool_names.append(name)
        if not user_texts:
            continue
        if tool_names:
            note = (
                f"[system-note: в прошлом ходе были вызваны инструменты: "
                f"{', '.join(tool_names)}. Результаты получены.]"
            )
            user_texts = [note, *user_texts]
        content: str | list[dict[str, Any]]
        if len(user_texts) == 1:
            content = user_texts[0]
        else:
            content = [{"type": "text", "text": t} for t in user_texts]
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": session_id,
        }
```

**Важное (v2):**

- **U1 (phase 2 ограничение):** replay `tool_use`/`tool_result` блоков в history-envelope'ы — **не делаем**. Скипаем, но добавляем synthetic system-note со списком названий инструментов, чтобы модель не повторяла их случайно. Расширенная history replay — phase 3+, см. `test_u1_tool_block_roundtrip_xfail` в §3.11.
- **U2:** ThinkingBlock'и не попадают в envelope (`block_type=='thinking'` filter). Xfail `test_u2_cross_session_thinking_rejected_xfail` защищает инвариант.
- **`test_bridge_mock`:** assert ровно (N user-envelope'ов + 1 current); если history содержит turn с `block_type="tool_use"` — первый текст envelope'а этого turn'а начинается с `[system-note: ...]`.

### 2.3 Bash prefilter — выбор варианта (Recommended: allowlist-first)

Devil's review указал, что regex `\b(\.env|\.ssh|...)\b` из v1 имеет bypass'ы: `env`/`printenv`/`set` (утечка env vars), octal-escape `$'\056env'`, glob `.e*`, base64-decode payloads, `awk` / `strings` на `~/.aws/credentials`, и false-positive на legitimate `token`-args. Ниже два варианта.

#### Вариант A (Recommended): allowlist-first

Primary control = whitelist точных префиксов: `python tools/`, `uv run tools/`, `git status`, `git log`, `git diff`, `ls`, `pwd`, `echo`, `cat <path inside project_root>`. Всё остальное — deny с reason "not in allowlist; попроси владельца добавить в allowed-tools скила". Slip-guard regex (расширенный: `env`, `printenv`, `.aws`, octal-escape `$'\0…`, `base64 -d`, `openssl enc`, `xxd -r`, длинные base64-блобы ≥48 chars) — вторичный, блокирует даже allowlist'нутые команды с попыткой эксфильтрации.

**Pros:** любая неожиданная команда блокируется по-дефолту; модель вынуждена явно просить расширения allowlist'а. Защита от zero-day escape-трюков. Явный аудит-trail в логах (`pretool_decision reason=allowlist`).

**Cons:** любое тривиальное `wc -l`, `head`, `grep` требует расширения через skill. Для phase 2 smoke (ping skill) хватает; в phase 4+ (memory, GitHub) список расширяется через `allowed-tools` в SKILL.md — но это правильно: каждый skill декларирует свои команды.

#### Вариант B: regex slip-guard only (v1 подход)

Primary = расширенный regex. Allowlist отсутствует.

**Pros:** максимум свободы модели.

**Cons:** невозможно доказать полноту. Devil's review показал 4 bypass'а за 2 минуты; атакующий найдёт больше. Нарушает "secure by default".

#### Решение

**Recommended: вариант A (allowlist-first)**, реализован в §2.1 (`_bash_allowlist_check` + `_BASH_SLIP_GUARD_RE`). Если в production окажется слишком строго — расширяем allowlist через skill'овские `allowed-tools` frontmatter'ы, НЕ regex relaxation.

### 2.4 `src/assistant/handlers/message.py` — ClaudeHandler skeleton (замещает detailed-plan §9)

Полный skeleton с явным `completed`-контрактом и classify-helper. **v2 изменения:** `completed=True` выставляется **только** при получении `ResultMessage` (единственный success-signal из bridge); любой другой выход из цикла → `finally` делает `interrupt_turn`.

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from assistant.adapters.base import IncomingMessage
from assistant.bridge.claude import ClaudeBridge, ClaudeBridgeError
from assistant.config import Settings
from assistant.logger import get_logger
from assistant.state.conversations import ConversationStore

Emit = Callable[[str], Awaitable[None]]
log = get_logger("handlers.message")


def _classify_block(item: Any) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    """Returns (role, payload_dict, text_to_emit_or_None, block_type).

    role ∈ {'user','assistant','tool','result', None}; 'result' is meta-only
    and is NOT written to conversations — it's the success signal for the handler.
    """
    if isinstance(item, ResultMessage):
        meta = {
            "model": item.model,
            "stop_reason": item.stop_reason,
            "usage": item.usage,
            "cost_usd": item.total_cost_usd,
            "duration_ms": item.duration_ms,
            "num_turns": item.num_turns,
        }
        return ("result", meta, None, None)
    if isinstance(item, TextBlock):
        return ("assistant", {"type": "text", "text": item.text}, item.text, "text")
    if isinstance(item, ThinkingBlock):
        return (
            "assistant",
            {"type": "thinking", "thinking": item.thinking, "signature": item.signature},
            None,
            "thinking",
        )
    if isinstance(item, ToolUseBlock):
        return (
            "assistant",
            {"type": "tool_use", "id": item.id, "name": item.name, "input": item.input},
            None,
            "tool_use",
        )
    if isinstance(item, ToolResultBlock):
        return (
            "tool",
            {
                "type": "tool_result",
                "tool_use_id": item.tool_use_id,
                "content": item.content,
                "is_error": item.is_error,
            },
            None,
            "tool_result",
        )
    return (None, {}, None, None)


class ClaudeHandler:
    def __init__(
        self, settings: Settings, conv: ConversationStore, bridge: ClaudeBridge
    ) -> None:
        self._settings = settings
        self._conv = conv
        self._bridge = bridge

    async def handle(self, msg: IncomingMessage, emit: Emit) -> None:
        turn_id = await self._conv.start_turn(msg.chat_id)
        log.info("turn_started", turn_id=turn_id, chat_id=msg.chat_id)
        await self._conv.append(
            msg.chat_id,
            turn_id,
            "user",
            [{"type": "text", "text": msg.text}],
            block_type="text",
        )
        history = await self._conv.load_recent(
            msg.chat_id, self._settings.claude.history_limit
        )
        # Current turn has status='pending' → load_recent's 'complete' filter excludes it.

        completed = False
        try:
            async for item in self._bridge.ask(msg.chat_id, msg.text, history):
                role, payload, text_out, block_type = _classify_block(item)
                if role == "result":
                    # THE success signal — bridge guarantees this is the last item.
                    await self._conv.complete_turn(turn_id, meta=payload)
                    completed = True
                    log.info("turn_complete", turn_id=turn_id,
                             cost_usd=payload.get("cost_usd"))
                    continue
                if role is None:
                    continue
                await self._conv.append(
                    msg.chat_id, turn_id, role, [payload], block_type=block_type
                )
                if text_out:
                    await emit(text_out)
        except ClaudeBridgeError as e:
            await emit(f"\n\n⚠ {e}")
        finally:
            if not completed:
                await self._conv.interrupt_turn(turn_id)
                log.warning("turn_interrupted", turn_id=turn_id)
```

**Contract notes:**

- `completed = True` выставляется **только** при успешном `ResultMessage`. Timeout/exception/break/cancel → `completed` остаётся `False` → `finally` → `interrupt_turn`.
- `complete_turn` вызывается прямо внутри цикла при получении ResultMessage — гарантирует, что `turns.status='complete'` записывается ДО возврата handler'а. Spike R4 подтвердил: ResultMessage всегда последний item; после него bridge просто `return`'ится.
- `emit` вызывается только для текста; `tool_use`/`tool_result`/`thinking` пишутся в БД, но НЕ в чат.

### 2.5 `src/assistant/config.py` — полный shape с project_root + XDG (замещает detailed-plan §2 в части defaults)

**v2 изменения:** добавлены `_default_project_root` (долг плана), `_default_config_dir` и `_default_data_dir` с XDG override, `_user_env_file` теперь использует `_default_config_dir`. Thinking wiring — через `_build_options(**thinking_kwargs)` в §2.1 (НЕ отдельным snippet'ом как v1 писал).

```python
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_project_root() -> Path:
    # src/assistant/config.py → parents[2] = project root
    return Path(__file__).resolve().parents[2]


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "0xone-assistant"


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _user_env_file() -> Path:
    return _default_config_dir() / ".env"


class ClaudeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_",
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    timeout: int = 300
    max_turns: int = 20
    max_concurrent: int = 2
    history_limit: int = 20
    thinking_budget: int = 0     # 0 = disabled; >0 → max_thinking_tokens
    effort: str = "medium"       # 'low'|'medium'|'high'|'max'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[_user_env_file(), Path(".env")],
        extra="ignore",
    )
    telegram_bot_token: str
    owner_chat_id: int
    log_level: str = "INFO"
    project_root: Path = Field(default_factory=_default_project_root)
    data_dir: Path = Field(default_factory=_default_data_dir)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)  # type: ignore[arg-type]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "assistant.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
```

Dev-fallback: если `./.env` существует в project_root, `pydantic-settings 2.6` merge'ит его поверх отсутствующих полей (см. gotcha §4.8). Priority: XDG `~/.config/0xone-assistant/.env` first, `./.env` second.

`.env.example` дополнить:
```
CLAUDE_TIMEOUT=300
CLAUDE_MAX_TURNS=20
CLAUDE_MAX_CONCURRENT=2
CLAUDE_HISTORY_LIMIT=20
CLAUDE_THINKING_BUDGET=0
CLAUDE_EFFORT=medium
```

**First-run mkdir:** в `Daemon.start()` до `connect(db_path)` — `db_path.parent.mkdir(parents=True, exist_ok=True)` (см. §3.3 item 5).

---

## 3. Step-by-step execution recipe

Все команды из `/Users/agent2/Documents/0xone-assistant/`. Phase 1 код существует; coder **не делает** `uv init`. Task 0 spike уже выполнен researcher'ом — `spikes/sdk_probe*.py` и `plan/phase2/spike-findings.md` на месте, читать перед стартом.

### 3.1 Прочитать контекст

1. `plan/phase2/spike-findings.md` — целиком (особенно §1 итоговая таблица и §4 UNVERIFIED).
2. `plan/phase2/detailed-plan.md` — целиком; секции §1–13 базовые, §2–§8 patch'ятся по §2 этого документа.
3. Этот файл (`implementation.md`) — §1 верифицированные решения, §2 скорректированный код.

### 3.2 Dependencies

```bash
uv add "claude-agent-sdk>=0.1.59,<0.2" "pyyaml>=6.0"
uv add --dev "types-pyyaml>=6.0"
uv sync
```

Ожидаем: `pyproject.toml` обновлён, `uv.lock` изменился (коммитим).

### 3.3 Config + XDG + first-run mkdir (см. §2.5)

1. Переписать `src/assistant/config.py` полностью по §2.5 (`_default_project_root`, `_default_config_dir`, `_default_data_dir`, `_user_env_file`, `ClaudeSettings`, `Settings.claude=Field(default_factory=ClaudeSettings)`, `project_root` и `data_dir` через `default_factory`). `@lru_cache` оставить на `get_settings()`.
2. Обновить `.env.example` — блок `CLAUDE_*` (§2.5 хвост).
3. `.gitignore`: добавить `.claude/skills` (симлинк), `spikes/sdk_probe*_report.json`.
4. Обновить `README.md`: секция "Где живут `.env` и `data/`" (XDG override, `~/.config/0xone-assistant/.env`, `~/.local/share/0xone-assistant/`), migration note phase1→phase2 (вручную `mv ./data/assistant.db ~/.local/share/0xone-assistant/assistant.db` — не автомиграция).
5. В `Daemon.start()` до `connect(db_path)` — `db_path.parent.mkdir(parents=True, exist_ok=True)`. First-run работает без ручного `mkdir`.

### 3.4 Auth preflight (новое в v2)

В `Daemon.start()` до создания `ClaudeBridge` — проверить, что `claude` CLI залогинен:

```python
import asyncio
import sys

async def _preflight_claude_auth() -> None:
    """Fail-fast if `claude` CLI is missing or not authenticated.

    Exit codes:
      3 — CLI missing, hung, or not authenticated (user action required).
    """
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "claude", "--print", "ping",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=10.0,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (FileNotFoundError, TimeoutError):
        log.error("claude_cli_missing_or_hanged",
                  hint="Install Claude Code CLI and run `claude login`.")
        sys.exit(3)
    if proc.returncode != 0:
        err = (stderr_bytes or b"").decode("utf-8", "replace").lower()
        if "auth" in err or "login" in err or "not authenticated" in err:
            log.error("claude_cli_not_authenticated",
                      hint="Run `claude login` before starting the bot.")
            sys.exit(3)
        log.error("claude_cli_failed", stderr=err[:500])
        sys.exit(3)
    log.info("auth_preflight_ok")
```

Вызывается **до** `ensure_skills_symlink` в §3.12, чтобы `sys.exit(3)` не оставлял ресурсов.

### 3.5 DI-рефактор (detailed-plan §3)

`src/assistant/main.py` — `Daemon.__init__(self, settings: Settings)`. `main()` делает `settings = get_settings(); setup_logging(settings.log_level); d = Daemon(settings)`. `TelegramAdapter.__init__(settings)` и `ClaudeHandler.__init__(settings, store, bridge)` принимают settings явно. `get_settings()` зовётся **только** из `main()`.

### 3.6 Handler contract (S1)

1. `src/assistant/adapters/base.py` — `Handler` Protocol: `async def handle(self, msg, emit: Callable[[str], Awaitable[None]]) -> None`.
2. `src/assistant/adapters/telegram.py` — `_on_text` собирает chunks через emit, финальный `send_message`; split по 4096; `DefaultBotProperties(parse_mode=None)` (был HTML).

### 3.7 Store миграция 0002 + manifest mtime fix (S6)

1. Создать `src/assistant/state/migrations/0002_turns_block_type.sql` (detailed-plan §7a). Recreate-table pattern для FK.
2. Обновить `src/assistant/state/db.py` — `SCHEMA_VERSION=2`; bootstrap применяет 0001 (inline) и 0002 (из файла). Atomic-per-version `BEGIN IMMEDIATE`. FK recreate: `PRAGMA foreign_keys=OFF` → `BEGIN` → recreate → `COMMIT` → `PRAGMA foreign_keys=ON`.
3. `src/assistant/state/conversations.py`:
   - Полностью переписать `load_recent` — turn-based SQL из detailed-plan §7. **Phase 1 row-based `load_recent` выбрасывается.**
   - Добавить `start_turn(chat_id) -> turn_id`, `complete_turn(turn_id, meta)`, `interrupt_turn(turn_id)`.
   - `append(...)` — добавить kwarg `block_type` (обязательный начиная с phase 2).
4. **`src/assistant/bridge/skills.py` manifest mtime fix (v2):** detailed-plan §5 содержит правильный код, фиксируем формулировку:
   ```python
   def _manifest_mtime(skills_dir: Path) -> float:
       mtimes = [skills_dir.stat().st_mtime]
       mtimes.extend(p.stat().st_mtime for p in skills_dir.glob("*/SKILL.md"))
       return max(mtimes)
   ```
   Dir mtime на APFS НЕ меняется при in-place edit SKILL.md — обязательный `max` по файлам.

### 3.8 Bridge (§2.1–§2.4 выше)

1. Создать `src/assistant/bridge/__init__.py` (пустой).
2. `src/assistant/bridge/bootstrap.py` — `ensure_skills_symlink` (detailed-plan §4).
3. `src/assistant/bridge/skills.py` — parser + mtime-cached `build_manifest` (detailed-plan §5 с fix §3.7 выше).
4. `src/assistant/bridge/system_prompt.md` — detailed-plan §6 **as-is**.
5. `src/assistant/bridge/history.py` — §2.2 выше (с synthetic tool-note).
6. `src/assistant/bridge/claude.py` — §2.1 выше (полный финальный код).

### 3.9 Handler (§2.4 выше)

`src/assistant/handlers/message.py` — **полный skeleton из §2.4** (не detailed-plan §9). Заменяет `EchoHandler` целиком.

### 3.10 Skill + tool (detailed-plan §11–§12)

1. `skills/ping/SKILL.md` — as-is.
2. `tools/ping/main.py` — as-is.

### 3.11 Tests

**6 core tests** (detailed-plan §13):
- `test_skills_manifest.py` — frontmatter parser, manifest string.
- `test_skills_manifest_cache.py` — `build_manifest` кеширует (mock `Path.stat`); `touch` SKILL.md → новый manifest.
- `test_bootstrap.py` — idempotent `ensure_skills_symlink`; `readlink` == `../skills`.
- `test_bridge_mock.py` — `monkeypatch.setattr("assistant.bridge.claude.query", fake_async_gen)`. fake yields `SystemMessage(init)` → `AssistantMessage(content=[TextBlock("hi")])` → `ResultMessage(usage={"input_tokens":1,"output_tokens":1}, total_cost_usd=0.01, model="m", stop_reason="end_turn", duration_ms=1, num_turns=1, session_id="s", subtype="success")`. **Asserts:** (1) `prompt_stream` даёт ровно (N user-envelope'ов + 1 текущий), без assistant-envelope'ов; (2) bridge yield'ит TextBlock; (3) bridge yield'ит ResultMessage последним; (4) history с `block_type="tool_use"` → первый envelope turn'а начинается с `[system-note: ...]`.
- `test_load_recent_turn_boundary.py` — 3 turn'а × 5 rows; `load_recent(limit=2)` → ровно 10 rows последних двух, 3-й turn полностью отсутствует.
- `test_interrupted_turn_skipped.py` — 2 complete + 1 interrupted (свежайший); `load_recent(limit=10)` возвращает только 2 complete; direct SELECT подтверждает физическое присутствие interrupted.

**4 UNVERIFIED-guard tests (v2, новые):**
- `test_u1_tool_block_roundtrip_xfail.py` — `@pytest.mark.xfail(strict=False, reason="U1: tool_use/tool_result replay in history not verified")`. Собирает history c complete turn'ом, где модель раньше вызвала Bash; hypothetical helper варианта с включением tool-блоков → `query` → assert стрим проходит до ResultMessage без exception. xpassed → можно включать replay.
- `test_u2_cross_session_thinking_rejected_xfail.py` — `xfail(strict=False)`. Подаёт в prompt_stream fake envelope с `content=[{"type":"thinking","thinking":"...","signature":"fake"}]` и assert'ит что `query()` отвергает (exception или error в Result). Защищает инвариант "не переподаём thinking".
- `test_u3_symlink_skill_discovery.py` — **regular test (НЕ xfail).** В `tmp_path` создаёт `skills/echo/SKILL.md`, `.claude/skills` как symlink → `../skills`, запускает `query(prompt="say PONG_TEST", options=ClaudeAgentOptions(cwd=tmp_path, setting_sources=["project"]))`, перехватывает `SystemMessage(subtype="init")`, assert `"echo" in message.data["skills"]`. Marker `@pytest.mark.requires_claude_cli` → skip в CI без OAuth.
- `test_u5_hookmatcher_regex_xfail.py` — `xfail(strict=False)`. `HookMatcher(matcher="Ba.*", hooks=[spy])`, Bash-вызов, assert spy сработал. xpassed → 7 matcher'ов можно сократить до 2.

**Expected:** `just test` → 3 phase-1 + 6 core + 1 regular U3 = **10 passed**, 3 xfail (U1/U2/U5) = **13 total**. `just lint` зелёный.

### 3.12 `main.py` wiring

`Daemon.start()`:
```python
await _preflight_claude_auth()                                     # §3.4
ensure_skills_symlink(self._settings.project_root)
self._settings.db_path.parent.mkdir(parents=True, exist_ok=True)   # §3.3 item 5
self._conn = await connect(self._settings.db_path)
await apply_schema(self._conn)                                     # применяет 0001 и 0002
store = ConversationStore(self._conn)
bridge = ClaudeBridge(self._settings)
self._adapter = TelegramAdapter(self._settings)
handler = ClaudeHandler(self._settings, store, bridge)
self._adapter.set_handler(handler)
await self._adapter.start()
```

### 3.13 Lint + test + logging contract

```bash
uv run ruff format .
just lint                          # ruff check + format-check + mypy strict
just test                          # 10 passed + 3 xfail
```

**Logging contract (per-layer, v2 новое):**

| Logger | Event | Level | Fields |
|---|---|---|---|
| `bridge.claude` | `query_start` | info | chat_id, prompt_len, history_rows |
| `bridge.claude` | `sdk_init` | info | model, skills (list), cwd |
| `bridge.claude` | `block_received` | debug | type |
| `bridge.claude` | `result_received` | info | model, stop_reason, cost_usd, duration_ms, num_turns, input_tokens, output_tokens |
| `bridge.claude` | `timeout` | warning | chat_id, timeout_s |
| `bridge.claude` | `sdk_error` | error | error |
| `bridge.claude` | `manifest_rebuilt` | info | (no fields required) |
| `bridge.claude` (hooks) | `pretool_decision` | info/warning/debug | tool_name, decision, reason, cmd/path/url (truncated) |
| `handlers.message` | `turn_started` | info | turn_id, chat_id |
| `handlers.message` | `turn_complete` | info | turn_id, cost_usd |
| `handlers.message` | `turn_interrupted` | warning | turn_id |

### 3.14 Manual smoke

1. Создать `~/.config/0xone-assistant/.env` с реальными токенами.
2. Auth preflight: `claude --print ping` должен работать; если нет — `claude login`.
3. `just run` → JSON-лог `auth_preflight_ok` → `daemon_started`; при первом сообщении owner'а: `sdk_init` с `model=claude-opus-4-6`, `skills=['ping']`.
4. Telegram: "use the ping skill" → модель → Bash → allowlist allows (`python tools/ping/main.py` матчит `python tools/`) → `{"pong": true}` → handler шлёт ответ.
5. Security (Bash allowlist): "run `wc -l README.md`" → deny "not in allowlist". "run `cat README.md`" → allowed.
6. Security (secrets): "run `cat .env`" или "run `cat ~/.aws/credentials`" → deny по slip-guard/allowlist.
7. Security (file): "read `/etc/passwd`" через Read → file_hook deny (absolute outside project_root).
8. Security (SSRF): WebFetch `http://169.254.169.254/latest/meta-data/` → webfetch_hook deny.
9. Перезапуск + новое сообщение → SDK видит историю; turn с прошлым tool_use → модель НЕ повторяет (видит synthetic note).
10. `~/.local/share/0xone-assistant/assistant.db`: `SELECT status, COUNT(*) FROM turns GROUP BY status` → все `complete`.
11. Длинный ответ (>4096 chars) — разбит на 2+ telegram-сообщения.
12. `.claude/skills` — `readlink` должен показать `../skills`. Если SystemMessage.data['skills'] пустой (U3 failure) — fallback на реальный copy.

### 3.15 Git

```bash
git add .
# НЕ коммитить — коммитит отдельный шаг orchestrator'а.
```

---

## 4. Known gotchas (дополнительно к spike-findings §3)

1. **`asyncio.timeout` вокруг streaming-input `query()`** — при таймауте async-gen prompt_stream должен быть закрыт корректно (иначе aiosqlite-коннекция может остаться залочена). SDK `client.py:162` делает `finally: await query.close()`, который сигнализирует stream_input shutdown. Проверить что `ClaudeBridgeError` пробрасывается чисто (использовать `try/except TimeoutError`, **не** `asyncio.TimeoutError` — py3.12 unified их, но future-proof).
2. **`HookMatcher(matcher="Bash")` регистронезависим?** Spike'ом не проверено. SDK tool names приходят как `"Bash"`, `"Read"` — PascalCase. Использовать точно такой case.
3. **`system_prompt=str`** в `ClaudeAgentOptions` — просто строка. Но тип-аннотация поля: `str | SystemPromptPreset | SystemPromptFile | None`. Проверить что передача plain str не триггерит "preset" логику.
4. **`UserMessage.content` как list[dict]** — SDK валидирует формат. Безопаснее: если у row только один text-блок — отправлять как `str` (`content="hello"`); несколько → `content=[{"type":"text","text":"..."}, ...]`. §2.2 уже делает так.
5. **`message.data` на SystemMessage** — dict с нестабильным содержимым. Логируем keys, но не строим on-disk contract от него.
6. **`ThinkingBlock.signature`** содержит opaque data SDK. Сохраняем в БД **как есть** (строка), никогда не пытаемся парсить.
7. **Sqlite migration 0002** — detailed-plan §7a использует recreate-table pattern для FK. Порядок: `PRAGMA foreign_keys=OFF` → `BEGIN` → recreate → `COMMIT` → `PRAGMA foreign_keys=ON`. aiosqlite connect() ставит `foreign_keys=ON` в phase 1 — migration runner должен временно выключить.
8. **`pydantic-settings env_file=[list]`** — 2.6 берёт **первый существующий**, затем заглядывает в следующий для отсутствующих переменных (merge, а не short-circuit). Если в `~/.config/.../.env` есть `TELEGRAM_BOT_TOKEN` а в `./.env` тоже есть — выигрывает первый.
9. **`ChatActionSender.typing` vs долгая блокировка** — phase 1 уже оборачивает handle. После §2.1 bridge может занять 30–60с; typing должен всё это время капать. Aiogram's ChatActionSender пингует каждые 5с пока async-context открыт — OK.
10. **`spikes/` остаётся в репо** как reference. Не коммитим `sdk_probe*_report.json` (machine artefacts) — добавить в `.gitignore`.
11. **SQLite `data_dir` переезд** — миграция phase1→phase2: если у dev'а уже есть `./data/assistant.db`, нужно либо вручную `mv ./data/assistant.db ~/.local/share/0xone-assistant/assistant.db`, либо запустить скрипт. Документировать в README; не делать автомиграцию (risk).
12. **Orphan bridge task при падении handler'а** — `ClaudeBridge.ask` это async-gen; при exception в handler async-gen'овский `aclose()` вызывается автоматически, bridge `async with self._sem` освободит семафор. `finally: interrupt_turn()` выполнится ДО закрытия gen'а — стандартный `try/finally` вокруг `async for` справляется.
13. **Symlink vs real dir (U3, overblown fear):** smoke step §3.14.12 закрывает вопрос. Если `SystemMessage.data['skills']` пустой после bootstrap — проверить симлинк, fallback — заменить на реальный copy. `test_u3_symlink_skill_discovery` (§3.11) ловит регрессию в unit'ах.
14. **RateLimitEvent mid-stream** — skip-branch уже в bridge (§2.1), no action required.
15. **Python 3.12 async-gen cleanup** — работает корректно; spike проверил через timeout в `probe_r5_deny`. `aclose()` вызывается при garbage-collect генератора.
16. **Auth preflight spawns `claude --print`** — в `create_subprocess_exec` (НЕ shell). `claude` CLI смотрит на `~/.claude/` для OAuth token; если `$HOME` не выставлен при старте daemon (rare) — preflight падает с RC != 0, логируется понятный hint.

---

## 5. Citations

- spike artefacts (эмпирическая база): `/Users/agent2/Documents/0xone-assistant/plan/phase2/spike-findings.md`, `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe{,2,3}.py`, `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe{,2,3}_report.json`.
- SDK source (installed 0.1.59): `/Users/agent2/.cache/uv/archive-v0/L2hRBggEhtq5pQwTPhx4o/lib/python3.12/site-packages/claude_agent_sdk/` — notably `_internal/client.py:45-164`, `_internal/query.py:264-316`, `types.py:1209,1257`.
- Claude Agent SDK Python docs: https://docs.claude.com/en/api/agent-sdk/python
- GitHub `anthropics/claude-agent-sdk-python` (0.1.59 tag).
- Claude Code Settings & Hooks reference: https://docs.claude.com/en/docs/claude-code/settings , https://docs.claude.com/en/docs/claude-code/hooks
- aiogram 3.26 `ChatActionSender`: https://docs.aiogram.dev/en/latest/utils/chat_action.html (carried over from phase 1)
- pydantic-settings 2.6 `env_file` list behaviour: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
- SQLite FK migration (recreate-table pattern): https://sqlite.org/lang_altertable.html#otheralter
- OWASP SSRF Prevention Cheatsheet: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- AWS IMDSv2 / private-IP guidance: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instancedata-data-retrieval.html
