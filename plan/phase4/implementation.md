# Phase 4 — Implementation (spike-verified, 2026-04-17)

## Revision history

- **v1** (2026-04-17): initial after spikes S-A (SDK per-skill hook
  semantics) and S-B (tool_result.content shape). Empirical answers
  backed by:
  - `spikes/sdk_per_skill_hook.py` (5 cases, all completed; report in
    `spikes/sdk_per_skill_hook_report.json`).
  - `spikes/history_toolresult_shape.py` (3 variants including Bash
    error-exit; report in `spikes/history_toolresult_shape_report.json`).
  - SDK source inspection of `ToolResultBlock`, `UserMessage`,
    `AssistantMessage` in `claude-agent-sdk==0.1.59`.

  **Key semantic shift from detailed-plan §0/§0b angle-bracket
  assumptions:** (a) SDK hooks carry **no per-skill attribution** —
  Q8 intersection must be static per turn, not per tool-call; (b)
  `ToolResultBlock` lives inside **`UserMessage`**, not
  `AssistantMessage`, and phase-2 bridge **never persists** those rows
  today — phase 4 needs a bridge fix BEFORE `history.py` synthetic
  summary can find anything to summarise; (c) on this host
  `options.allowed_tools` is advisory rather than enforcing, because
  user-level `permissions.allow` in `~/.claude/settings.json` overrides.

- **v2** (2026-04-17): applied devil's-advocate wave-2 review — 4
  blockers (B1–B4), 6 strategic gaps (G1–G6), 3 security/operational
  items (S1–S3). Key shifts from v1:
  1. **S1 honest disclaimer** — §2.3 and new §Q8-note state clearly
     that `_effective_allowed_tools` is advisory on hosts with
     permissive user-level `~/.claude/settings.json`; "memory
     isolation" is NOT claimed as a phase-4 deliverable.
  2. **B2 global `tool_use_id → tool_name` map** — `history.py`
     scans whole history, not per-turn; tool_use in turn N+result
     in turn N+1 no longer silently dropped.
  3. **B1 pre-flight test audit** — new §Step 2.0 enumerates tests
     at risk from the bridge UserMessage fix (count-assertions) with
     mitigation recipe.
  4. **B3 seed helper** — `tests/_helpers/history_seed.py::seed_toolresult_row`
     lets unit tests produce the exact row shape without running live SDK.
  5. **B4 behavioral regression** — phase-3 hash test now asserts
     model-emitted Bash command substring, not CLI-accepted flag
     (catches LLM hallucination, not just code path).
  6. **G1** union collapse → WARN log `allowed_tools_union_collapsed_to_baseline`.
  7. **G2** explicit naming split between `bridge/skills.py::parse_skill`
     (SKILL.md) and `tools/memory/_lib/frontmatter.py::parse_note` (note MD).
  8. **G3** vault dir mode 0o700 + warn if looser pre-existing perms.
  9. **G4** `PRAGMA busy_timeout=5000` on every sqlite connect.
  10. **G5** honest admission: URL detector does not scan synthetic
      snippets; model may re-fire preview on a historical URL —
      documented, not fixed in phase 4.
  11. **G6** future cap `HISTORY_MAX_SNIPPET_TOTAL_CHARS` deferred to
      phase 5 scheduler.
  12. **S2** SKILL.md carries FS-warranty block (local POSIX only; no
      iCloud / Dropbox / SMB).
  13. **S3** `_probe_lock_semantics` at first `_ensure_index` —
      `sys.exit(5)` if flock is a no-op on current FS; env
      `ASSISTANT_SKIP_LOCK_PROBE=1` skips for CI.

Companion docs (coder **must** read before starting):

- `plan/phase4/description.md` — 83-line summary.
- `plan/phase4/detailed-plan.md` — 630-line canonical spec (§0–§9, R1–R11).
- `plan/phase4/spike-findings.md` — empirical answers with raw output.
- `plan/phase3/implementation.md` / `summary.md` — phase-3 state
  (skill-installer, PostToolUse sentinel, 3-way allowed-tools semantics,
  URL detector).
- `plan/phase2/implementation.md` — bridge/hook/ConversationStore API.

**Auth:** OAuth via `claude` CLI (`~/.claude/`). Do not introduce
`ANTHROPIC_API_KEY` anywhere in phase 4 code, config, or tests.

---

## 1. Verified decisions (spike answers)

| # | Question (from detailed-plan §0/§0b) | Spike answer | Source |
|---|---|---|---|
| S-A.1 | What is the source of `active_skills` at `_build_options` time? | `init.data["skills"]` from `SystemMessage(subtype="init")` is the **authoritative post-hoc** list, but arrives AFTER `query()` is already running. Per-skill `allowed_tools` is **not** in it — names only. For pre-query contract, read from `bridge/skills.py::build_manifest` (FS scan of `<project_root>/skills/*/SKILL.md`). | `sdk_per_skill_hook_report.json` case `C0_init_observation` → `init.skills = ["update-config", "debug", "simplify", ..., "sb", "sa"]` (merged user+project scope, names only). |
| S-A.2.a | Does `options.allowed_tools=[Read]` block `Bash` invocation (case A2)? | **No** — Bash fired on this host despite options being `["Read"]`. User-level `~/.claude/settings.json` `permissions.allow` overrides the programmatic option. | `sdk_per_skill_hook_report.json` case `CA2_bash_blocked_by_options` → `pre_calls: [{tool_name: "Bash"}]`. |
| S-A.2.b | `options.allowed_tools=[]` (lockdown) — does it block `Bash`? | **No** — Bash still fired (same reason as A.2.a). | `CB_empty_options_lockdown` → `pre_calls: [{tool_name: "Bash"}]`. |
| S-A.2.c | Permissive baseline (SKILL.md has no `allowed-tools`) + options=[Bash,Read] — works as expected? | **Yes** — `Bash` fired. | `CC_skill_missing_allowed_tools` → `pre_calls: [{tool_name: "Bash"}]`. |
| S-A.3 | Can a `PreToolUse` hook know which skill invoked a tool? | **No** — `HookContext.dir(...)` returned empty, `input_data` keys = `{cwd, hook_event_name, permission_mode, session_id, tool_input, tool_name, tool_use_id, transcript_path}`, `agent_id` and `agent_type` always `None`. No `active_skill`/`source_skill` anywhere. | All 4 hook-firing cases in `sdk_per_skill_hook_report.json` → `ctx_attrs: {}`. |
| S-A.4 | Is `setting_sources=["project"]` enough for SDK to pick up `<cwd>/skills/`? | **No** — the SDK walks `<cwd>/.claude/skills/`. Phase-2 `ensure_skills_symlink` creates the `.claude/skills -> ../skills` symlink; without it the probe showed 0 project skills. | `C0_init_observation` before symlink: `init.skills` lacked `sa/sb`; after symlink: `init.skills` included them. |
| S-B.1 | Is `ToolResultBlock.content` a str or list? | **`str`** in all 3 Bash variants (single-line JSON, multi-line with Cyrillic, error-exit). SDK dataclass allows `str \| list[dict] \| None` but live traffic for Bash is `str`. | `history_toolresult_shape_report.json` → `raw_tool_result_blocks[].content_type = "str"` × 3. |
| S-B.2 | Where does `ToolResultBlock` appear — `AssistantMessage` or `UserMessage`? | **`UserMessage`**. Phase-2 `bridge/claude.py:231` skips `UserMessage` — ToolResult rows were NEVER persisted in phase 2/3. | `history_toolresult_shape_report.json` → `raw_tool_result_blocks[].enclosing_cls = "UserMessage"` × 3. |
| S-B.3 | Does `is_error` sit on the block or inside `content`? | **Block level** (`ToolResultBlock.is_error: bool`). SDK prepends `"Exit code N\n"` to the content string for non-zero exits. | `errorexit` variant → `is_error: true`, `content: "Exit code 7\nboom"`. |
| S-B.4 | Is UTF-8 truncation safe with `str[:N]`? | **Yes** — Python slices on code-point boundaries; Cyrillic `'жена'` round-tripped unchanged under `json.dumps(..., ensure_ascii=False)`. | `multiline` variant. |
| S-B.5 | Exact `content_json` shape persisted to `conversations` for `block_type='tool_result'`? | `[{"type":"tool_result","tool_use_id":"toolu_...","content":"<str>","is_error":<bool>}]` — captured via monkeypatched `ConversationStore.append`. | `history_toolresult_shape_report.json` → `appended_tool_result_rows_content_json`. |

**Pinned versions (known-good 2026-04-17):**

| Package | Pin | Note |
|---|---|---|
| `claude-agent-sdk` | `>=0.1.59,<0.2` | Spikes run against 0.1.59. |
| `pyyaml` | `>=6.0` | Phase-2 dep; reused by `tools/memory/_lib/frontmatter.py`. |
| `aiosqlite` | `>=0.19` | Phase-2 dep; not touched by phase 4. |
| stdlib `sqlite3` | (bundled) | `tools/memory/_lib/fts.py` uses sync `sqlite3` — CLI is not async. |

**Phase-4 is _mostly_ stdlib.** `tools/memory/` carries no `pyproject.toml`
(Q2 decision). The CLI shells out to `python tools/memory/main.py`
against the main interpreter so it sees the phase-2 `yaml` install.

---

## 2. Corrected code snippets

Only the snippets from `detailed-plan.md` that need revision after the
spikes. Everything else — take as-is from the detailed plan.

### 2.1 `bridge/claude.py` — bridge must persist `ToolResultBlock` rows (NEW constraint from S-B.2)

The detailed plan §5 assumes `tool_result` rows exist in the DB. The
spike proved they don't. Phase-4 must add a `UserMessage` branch to the
bridge that yields each `ToolResultBlock` so the handler's existing
`_classify(ToolResultBlock)` branch actually fires.

```python
# src/assistant/bridge/claude.py
# Add at top:
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    ToolResultBlock,   # NEW
    UserMessage,       # NEW
    query,
)

# Inside ClaudeBridge.ask, extend the message loop:
async for message in sdk_iter:
    if isinstance(message, SystemMessage) and message.subtype == "init":
        # (unchanged)
        ...
        yield init
        continue
    if isinstance(message, AssistantMessage):
        for block in message.content:
            log.debug("block_received", type=type(block).__name__,
                      enclosing="AssistantMessage")
            yield block
        continue
    if isinstance(message, UserMessage):
        # Phase 4 — surface ToolResultBlocks so ConversationStore can
        # persist them. Plain-str content (typical Bash reply) is
        # skipped — it already lives in the human user-envelope row.
        content = message.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    log.debug(
                        "block_received",
                        type=type(block).__name__,
                        enclosing="UserMessage",
                    )
                    yield block
        continue
    if isinstance(message, ResultMessage):
        # (unchanged)
        ...
        yield message
        return
    # SystemMessage(other), RateLimitEvent — skip.
```

**Compatibility with phase 3:** existing tests assert AssistantMessage
dispatch — unchanged. New `UserMessage` branch only activates when a
tool_result is in the content list; plain-str user content (SDK
pass-through of our own envelope) is correctly ignored.

### 2.2 `bridge/history.py` — `_render_tool_summary` with spike-verified shape (detailed-plan §5)

```python
# src/assistant/bridge/history.py

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# Truncate char cap. Phase-4 default; overridable via
# Settings.memory.history_tool_result_truncate_chars.
# Safe on Python str (code-point boundary); Cyrillic verified via spike.
TOOL_RESULT_TRUNCATE = 2000


def _stringify_tool_result_content(content: Any) -> str:
    """Normalise ToolResultBlock.content to a display string.

    Spike S-B.1 observed `content: str` in all Bash variants; we keep a
    defensive list-branch for future-proofing image/multi-block results
    the SDK may start returning (0.1.59 Bash → str is the only observed
    path; phase-5 may revisit for WebFetch/Read).
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
            elif btype in ("image", "image_url"):
                parts.append(f"[{btype} block]")
            else:
                parts.append(f"[{btype or 'unknown'} block]")
        return "".join(parts)
    # Bytes/other — never observed; render placeholder.
    return f"[non-text content: {type(content).__name__}]"


def _render_tool_summary(
    tool_names: list[str],
    tool_results_by_name: dict[str, list[dict[str, Any]]],
    truncate: int = TOOL_RESULT_TRUNCATE,
) -> str:
    """Build the synthetic `[system-note: ...]` body.

    Output format (Russian; the model is prompted in Russian):

        [system-note: в прошлом ходе вызваны инструменты: foo, bar.
         результат foo: {"ok":true, ...}
         ошибка bar: Exit code 7\\nboom
         Для полного вывода вызови инструмент снова.]
    """
    lines: list[str] = [
        "в прошлом ходе вызваны инструменты: " + ", ".join(tool_names) + "."
    ]
    for name in tool_names:
        for r in tool_results_by_name.get(name, []):
            text = _stringify_tool_result_content(r.get("content"))
            if len(text) > truncate:
                text = text[:truncate].rstrip() + "...(truncated)"
            prefix = "ошибка" if r.get("is_error") else "результат"
            lines.append(f"{prefix} {name}: {text}")
    lines.append("Для полного вывода вызови инструмент снова.")
    return "[system-note: " + "\n".join(lines) + "]"


def _build_tool_name_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Scan **all** rows; return `tool_use_id -> tool_name`.

    B2 fix: SDK pattern is `tool_use` in assistant turn N, `tool_result`
    in USER turn N+1. Those live under **different** `turn_id`s in
    ConversationStore because phase-2 handler allocates one turn per
    user message. A per-turn lookup silently dropped every tool_result
    in v1 of this doc. Global lookup fixes it.
    """
    m: dict[str, str] = {}
    for row in rows:
        if row.get("block_type") != "tool_use":
            continue
        content = row.get("content")
        # ConversationStore stores rows as list[dict]; be defensive.
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            tu_id = block.get("id")
            name = block.get("name")
            if isinstance(tu_id, str) and isinstance(name, str) and name:
                m[tu_id] = name
    return m


def history_to_user_envelopes(
    rows: list[dict[str, Any]],
    chat_id: int,
    *,
    tool_result_truncate: int = TOOL_RESULT_TRUNCATE,
) -> Iterator[dict[str, Any]]:
    """Convert ConversationStore rows -> SDK user-envelope stream.

    Phase 4 (Q1): tool_use/tool_result blocks are STILL not replayed
    to the SDK directly (U1 remains unverified). Instead the synthetic
    note grows a per-tool snippet with the first `tool_result_truncate`
    chars of each result content. The trade-off: bigger context
    per turn vs. model knowing what happened. Single-user, cost is
    not critical (D1 in detailed-plan).

    Pre-conditions enforced by phase-4 bridge.py: `block_type='tool_result'`
    rows DO exist in the DB (phase 2/3 never wrote any — see spike S-B.2).

    B2 fix: the tool_use_id → tool_name resolution is global across
    history, so that tool_result rows whose matching tool_use lives in
    a different turn_id still get a proper tool_name prefix
    (v1 per-turn map dropped them).
    """
    session_id = f"chat-{chat_id}"
    tool_name_by_id = _build_tool_name_map(rows)  # B2: whole history

    # Group rows by turn preserving first-seen order.
    by_turn: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for row in rows:
        if row.get("block_type") == "thinking":
            continue  # R2: SDK rejects cross-session thinking replay.
        turn_id = row["turn_id"]
        if turn_id not in by_turn:
            by_turn[turn_id] = []
            order.append(turn_id)
        by_turn[turn_id].append(row)

    for turn_id in order:
        user_texts: list[str] = []
        tool_names: list[str] = []
        results_by_name: dict[str, list[dict[str, Any]]] = {}

        for row in by_turn[turn_id]:
            if row["role"] == "user":
                for block in row["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            user_texts.append(text)
            elif row.get("block_type") == "tool_use":
                for block in row["content"]:
                    if not isinstance(block, dict):
                        continue
                    name = block.get("name")
                    if isinstance(name, str) and name and name not in tool_names:
                        tool_names.append(name)
            elif row.get("block_type") == "tool_result":
                for block in row["content"]:
                    if not isinstance(block, dict):
                        continue
                    tu_id = block.get("tool_use_id")
                    # B2: resolve via GLOBAL map, not per-turn
                    name = (
                        tool_name_by_id.get(tu_id)
                        if isinstance(tu_id, str)
                        else None
                    ) or "unknown"
                    if name not in tool_names:
                        tool_names.append(name)
                    results_by_name.setdefault(name, []).append(block)

        if not user_texts:
            continue

        if tool_names:
            note = _render_tool_summary(
                tool_names, results_by_name, truncate=tool_result_truncate
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

**Gotcha B3 closure** — `str[:2000]` is safe on Python strings (code-
point boundary). The multi-byte test case in `test_bridge_history_replay_snippet.py`
asserts Cyrillic-preservation.

**Gotcha B2 closure** — the `_stringify_tool_result_content` dispatch
matches the spike-S-B findings. The list-branch is defensive future-
proofing and has a unit test with a synthetic `[{"type":"text","text":"X"}]`
payload.

### 2.3 `bridge/claude.py` — per-skill allowed-tools intersection (detailed-plan §6, revised by S-A.3)

The detailed-plan §6 snippet was right in spirit; after S-A.3 we clarify
"union of installed skills" is static per turn (not per tool-call).
SDK cannot partition — document it loudly.

> **🟥 S1 — Known limitation (cannot be fixed in phase 4).**
>
> Spike S-A.2 showed that on this host `options.allowed_tools` is
> **advisory** — the user's `~/.claude/settings.json::permissions.allow`
> overrides it and grants full tool access regardless of what
> `_effective_allowed_tools` computes.
>
> This means: phase 4's original aspiration "memory-skill cannot
> WebFetch / Edit vault directly via SDK" is **NOT enforceable** via the
> `allowed_tools` intersection. The real defence layer is the PreToolUse
> hooks from phase 2 (Bash argv allowlist, file-path guard, WebFetch
> SSRF). But S-A.3 also found that PreToolUse hooks do not receive a
> `source_skill` hint — they cannot discriminate "this Write came from
> memory" vs "this Write came from skill-installer".
>
> Net effect: on hosts with a permissive user-level settings.json the
> memory skill effectively gets the global baseline of tools. The
> `_effective_allowed_tools` code stays in phase 4 because (a) on strict
> hosts (CI, service account, empty permissions.allow) it DOES gate,
> and (b) it serves as machine-readable declaration of author intent
> for future SDK versions that may honour the intersection.
>
> **Do NOT claim "memory isolation" in SKILL.md or user-facing docs.**
> Phase-4 Q8 is a declarative / defence-in-depth layer, not a sandbox.

```python
# src/assistant/bridge/claude.py
# Add a module-level constant; keep the existing hardcoded list inline
# with this source of truth for phase 3 compat.

_GLOBAL_BASELINE: frozenset[str] = frozenset(
    {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"}
)


def _effective_allowed_tools(manifest_entries: list[dict[str, Any]]) -> list[str]:
    """Compute effective `allowed_tools` set for the turn.

    Semantics (Q8, spike-verified):
      * union over installed skills' allowed_tools, intersected with baseline;
      * `None` in a skill's `allowed_tools` (missing/malformed frontmatter)
        contributes the whole baseline (permissive default);
      * `[]` (honest lockdown on one skill) contributes nothing — but other
        permissive skills re-expand the union (hence Q8 says "safe-only
        restrict"); a realistic single-lockdown skill does NOT narrow the
        turn below what other permissive skills already need;
      * no skills installed → baseline (fallback for empty manifest).

    Spike S-A.3 found that SDK 0.1.59 has no per-skill hook dispatch, so
    this union is a turn-wide static set, not per-tool-call.
    Defence-in-depth is provided by `make_pretool_hooks` (unchanged from
    phase 2/3) which always fires regardless of `allowed_tools`.

    Spike S-A.2 also found that on hosts where user-level
    `~/.claude/settings.json` has a broad `permissions.allow`, this
    option is advisory only. We still pass it because (a) on strict
    hosts it DOES gate and (b) it's the contract the SDK documents.
    """
    if not manifest_entries:
        return sorted(_GLOBAL_BASELINE)
    union: set[str] = set()
    collapsed_by: list[str] = []  # G1: names of skills that forced baseline
    for entry in manifest_entries:
        tools = entry.get("allowed_tools")
        if tools is None:
            union |= set(_GLOBAL_BASELINE)  # permissive default
            name = entry.get("name") or "<unnamed>"
            collapsed_by.append(name)
        elif not tools:
            continue  # []: honest lockdown on this skill; no contribution
        else:
            union |= {t for t in tools if t in _GLOBAL_BASELINE}
    if collapsed_by:
        # G1: one permissive skill re-expands the whole baseline — log so
        # operators can see which skill lost them any narrowing.
        log.warning(
            "allowed_tools_union_collapsed_to_baseline",
            skills=collapsed_by,
        )
    # If every skill declared `[]` (full-lockdown manifest), union is
    # empty. SDK treats empty allowed_tools as "no tools" — that's the
    # author intent. Callers should double-check before shipping an
    # all-[] manifest; this rarely matters in practice (always at least
    # ping/skill-installer need Bash).
    return sorted(union) if union else []


# `_build_options` now consults the manifest:

def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
    pr = self._settings.project_root
    dd = self._settings.data_dir
    # Same manifest used by system prompt — building it twice per turn
    # is cheap (mtime-cached) and keeps the signal-path clear.
    from assistant.bridge.skills import parse_skill
    entries: list[dict[str, Any]] = []
    skills_dir = pr / "skills"
    if skills_dir.exists():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            entries.append(parse_skill(skill_md))
    allowed_tools = _effective_allowed_tools(entries)
    log.info(
        "allowed_tools_computed",
        allowed=allowed_tools,
        skill_count=len(entries),
    )

    hooks: dict[Any, Any] = {
        "PreToolUse": make_pretool_hooks(pr),
        "PostToolUse": make_posttool_hooks(pr, dd),
    }
    thinking_kwargs: dict[str, Any] = {}
    if self._settings.claude.thinking_budget > 0:
        thinking_kwargs["max_thinking_tokens"] = self._settings.claude.thinking_budget
        thinking_kwargs["effort"] = self._settings.claude.effort
    return ClaudeAgentOptions(
        cwd=str(pr),
        setting_sources=["project"],
        max_turns=self._settings.claude.max_turns,
        allowed_tools=allowed_tools,
        hooks=hooks,
        system_prompt=system_prompt,
        **thinking_kwargs,
    )
```

**Compatibility with phase-3 tests (detailed-plan §Compatibility with
phase-3 skill-installer flow):**

- `skill-installer` declares `allowed-tools: [Bash]` → contributes `{Bash}`.
- `ping` declares `allowed-tools: [Bash]` → `{Bash}`.
- `memory` will declare `allowed-tools: [Bash, Read]` → `{Bash, Read}`.
- Effective union = `{Bash, Read}` — Write/Edit/Glob/Grep/WebFetch are
  **no longer** exposed to memory-turns. Skill-installer turns (same
  turn if both skills are loaded, since there's no per-skill dispatch)
  also narrowed — they don't need Write/Edit anyway (installer writes
  via its own subprocess, not via SDK `Write` tool).
- Regression test `test_bridge_per_skill_allowed_tools.py` asserts the
  exact union for the above 3-skill set.

### 2.4 `config.py` — `MemorySettings` (detailed-plan §1)

```python
# src/assistant/config.py — add near `ClaudeSettings`:

class MemorySettings(BaseSettings):
    """Memory / vault knobs. OAuth-agnostic; no secrets."""

    model_config = SettingsConfigDict(
        env_prefix="MEMORY_",
        env_file=(str(_user_env_file()), ".env"),
        extra="ignore",
    )

    vault_dir: Path | None = None
    index_db_path: Path | None = None
    fts_tokenizer: str = "porter unicode61 remove_diacritics 2"
    history_tool_result_truncate_chars: int = 2000
    max_body_bytes: int = 1_048_576  # 1 MB (S4 guard)


class Settings(BaseSettings):
    # ... existing fields
    memory: MemorySettings = Field(default_factory=MemorySettings)

    @property
    def vault_dir(self) -> Path:
        return self.memory.vault_dir or (self.data_dir / "vault")

    @property
    def memory_index_path(self) -> Path:
        return self.memory.index_db_path or (self.data_dir / "memory-index.db")
```

### 2.5 `tools/memory/_lib/fts.py` — FTS5 layer (detailed-plan §2)

Take detailed-plan §2 as-is. One S-B-related clarification — `notes.body`
column holds the **post-frontmatter** markdown body only (no `---`
fences), because:

1. Frontmatter is parsed out before FTS insert (we want `search("жена")`
   to match body text, not YAML keys).
2. S3 sanitisation (`_sanitize_body`) runs before the body is either
   persisted to disk or indexed.

> **G2 — Naming split: `parse_skill` vs `parse_note`.**
>
> These are two different frontmatter parsers with **different shape,
> validation, and no code sharing. Duplication is intentional.**
>
> | Function | Input | Frontmatter keys | Consumers |
> |---|---|---|---|
> | `src/assistant/bridge/skills.py::parse_skill` | `skills/*/SKILL.md` | `name`, `description`, `allowed-tools` | bridge manifest, effective-tools union |
> | `tools/memory/_lib/frontmatter.py::parse_note` | `<vault>/**/*.md` | `title`, `tags`, `area`, `created`, `related` | memory CLI (search/read/list) |
>
> B-4 principle from phase 3: the memory CLI must stay importless from
> `src/assistant/`, so it cannot reuse `parse_skill`. Coder: if you feel
> tempted to factor these into a shared helper, resist — the two files
> validate different field contracts and have different failure modes
> (skill parse errors → skill-permissive-default warn + skip; note parse
> errors → exit 3 "invalid frontmatter").

**G4 — `PRAGMA busy_timeout=5000` on every sqlite connect.** In
`_ensure_index`, `search`, `reindex`, and `upsert_index` helpers:

```python
def _connect(index_db: Path) -> sqlite3.Connection:
    """Opinionated sqlite connection for the memory index."""
    # timeout= is the python-level busy timeout; PRAGMA is redundant
    # belt+suspenders in case a child connection from a subprocess
    # inherits defaults.
    conn = sqlite3.connect(str(index_db), timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn
```

All CLI entrypoints go through `_connect` — never raw `sqlite3.connect`.
`PRAGMA busy_timeout=5000` is load-bearing under the `fcntl.flock`
sequence because WAL-mode readers may still momentarily block behind
a committing writer even with our external lock.

**S3 — Runtime flock semantics probe (called from `_ensure_index`).**

```python
import os
import fcntl

_LOCK_PROBE_DONE = False
_LOCK_PROBE_SKIP_ENV = "ASSISTANT_SKIP_LOCK_PROBE"


def _probe_lock_semantics(lock_path: Path) -> bool:
    """Return True if `fcntl.flock(LOCK_EX|LOCK_NB)` actually blocks
    a second exclusive acquire on the same path — i.e. we are on a
    filesystem where advisory locks work. On SMB / iCloud / Dropbox /
    some FUSE mounts the second acquire silently succeeds → data
    corruption risk on concurrent writes.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd1 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fd2 = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd1, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Both acquired exclusive → flock is a no-op here.
            return False
        except BlockingIOError:
            return True
    finally:
        try:
            fcntl.flock(fd1, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fcntl.flock(fd2, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd1)
        os.close(fd2)


def _ensure_lock_semantics_once(index_db: Path) -> None:
    """First-invoke safety check; cached for process lifetime."""
    global _LOCK_PROBE_DONE
    if _LOCK_PROBE_DONE:
        return
    if os.environ.get(_LOCK_PROBE_SKIP_ENV):
        _LOCK_PROBE_DONE = True
        return
    lock_path = Path(str(index_db) + ".lock")
    if not _probe_lock_semantics(lock_path):
        sys.stderr.write(
            json.dumps({
                "ok": False,
                "error": (
                    "fcntl.flock is advisory-only on this filesystem. "
                    "Vault corruption likely on concurrent writes. "
                    "Move MEMORY_VAULT_DIR to a local POSIX FS (APFS, "
                    "ext4, ZFS, XFS). Override with "
                    f"{_LOCK_PROBE_SKIP_ENV}=1 only in CI where you "
                    "serialize memory writes externally."
                ),
                "lock_path": str(lock_path),
            }, ensure_ascii=False) + "\n"
        )
        sys.exit(5)
    _LOCK_PROBE_DONE = True
```

`_ensure_index` calls `_ensure_lock_semantics_once(index_db)` as its
first line. Test `test_memory_lock_probe_exit5_on_noop_fs.py`
monkeypatches `_probe_lock_semantics` to return `False`, asserts
`exit 5`. A second test `test_memory_lock_probe_skip_env_bypass.py`
sets `ASSISTANT_SKIP_LOCK_PROBE=1` and asserts normal path.

### 2.6 `tools/memory/_lib/vault.py` — atomic write (detailed-plan §3 + S3)

Take detailed-plan §3 as-is. Add the S3 body sanitiser (detailed-plan
already specs it):

```python
def _sanitize_body(body: str) -> str:
    """Reject `---` at column 0 that would spoof a frontmatter boundary.

    Indent a literal `---` line by one space. Users writing plain markdown
    horizontal rules should use `***` or a leading space.
    """
    out: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\n\r")
        if stripped == "---":
            line = " " + line
        out.append(line)
    return "".join(out)
```

**G3 — Vault dir permissions.** Vault contains personal long-term
memory; it must not be world-readable on a multi-user box.

```python
def _ensure_vault(vault_dir: Path) -> None:
    """Idempotent init of vault structure with 0o700 dir mode.

    Note: `Path.mkdir(mode=0o700)` only applies mode on CREATION, not
    existing dirs. We do NOT chmod existing dirs (operator may have
    deliberately loosened perms); instead we warn if mode is looser
    than 0o700 so the signal surfaces in logs.
    """
    vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = vault_dir.stat().st_mode & 0o777
    except OSError:
        mode = 0
    if mode & 0o077:  # any group/other bit set
        log.warning(
            "vault_dir_permissions_too_open",
            path=str(vault_dir),
            mode=oct(mode),
        )
    (vault_dir / ".tmp").mkdir(exist_ok=True, mode=0o700)
```

Called from `Daemon.start()` (once per startup, cheap) AND from
`cmd_write` first-line (in case the vault was rm'd between writes).
Test `test_memory_vault_dir_mode_0o700.py`: first invocation creates
with mode 0o700; pre-existing vault with mode 0o755 logs
`vault_dir_permissions_too_open` but does NOT fail.

### 2.7 `tools/memory/main.py` — CLI (detailed-plan §4)

Take detailed-plan §4 as-is. Add these explicit checks in `cmd_write`
after `sys.stdin.read()`:

```python
body = sys.stdin.read()
settings = _settings_for_cli()
if len(body.encode("utf-8")) > settings.memory.max_body_bytes:
    sys.stderr.write(
        json.dumps({
            "ok": False,
            "error": f"body exceeds MEMORY_MAX_BODY_BYTES ({settings.memory.max_body_bytes})",
        }, ensure_ascii=False) + "\n"
    )
    return EXIT_VAL
body = _sanitize_body(body)
```

### 2.8 `skills/memory/SKILL.md` (detailed-plan §7)

Take detailed-plan §7 as-is. Critical: `allowed-tools: [Bash, Read]`
triggers the effective union = `{Bash, Read}` when memory is the only
write-capable skill loaded.

**S2 — FS warranty block to append to SKILL.md body (after examples,
before the closing paragraph):**

```markdown
## ⚠️ Vault storage requirements

Твой vault живёт в `{vault_dir}`. Он ДОЛЖЕН быть на локальной POSIX
файловой системе (APFS, ext4, ZFS, XFS). НЕ на iCloud Drive, Dropbox,
Google Drive, SMB / NFS mounts — там `fcntl.flock` деградирует в
silent no-op и concurrent write'ы приводят к повреждению индекса.

Если при первом `memory write` получишь `exit 5` с ошибкой
`fcntl.flock is advisory-only` — сообщи владельцу: "vault лежит на
синк-mount'е, перенеси его на локальный диск". Не пытайся обходить
через `ASSISTANT_SKIP_LOCK_PROBE=1` — это CI-escape hatch для
serialized-write сценариев, не для продакшена.
```

**Also**: do NOT claim "memory isolation" anywhere in SKILL.md
(consequence of S1). The skill is about capability ("долговременная
память"), not about sandboxing.

### 2.9 `bridge/system_prompt.md` (detailed-plan §8)

Take detailed-plan §8 as-is. Add G6 brace-escape before `.format`:

```python
# bridge/claude.py, inside _render_system_prompt:
def _escape_format_literal(s: str) -> str:
    """Escape `{`/`}` so str.format won't interpret them as placeholders."""
    return s.replace("{", "{{").replace("}", "}}")

manifest_str = _escape_format_literal(manifest)
vault_dir_str = _escape_format_literal(str(self._settings.vault_dir))
# project_root: also escape even though `/` never contains braces on POSIX.
project_root_str = _escape_format_literal(str(self._settings.project_root))
return template.format(
    project_root=project_root_str,
    vault_dir=vault_dir_str,
    skills_manifest=manifest_str,
)
```

Test `test_system_prompt_render_escapes_braces.py` writes a SKILL.md
with `description: "uses {foo}"` and asserts no `KeyError`.

---

## 3. Step-by-step execution recipe

Phases 0 and 0b are **already complete** — artifacts:
`spikes/sdk_per_skill_hook.py{,_report.json}` and
`spikes/history_toolresult_shape.py{,_report.json}`. Coder does not
re-run them (they make billed API calls). Coder only READS the reports
if debugging against the verified invariants.

**Order for coder (strictly sequential; each step lands green `uv run
pytest` before the next):**

### Step 1 — `MemorySettings` and env plumbing

- Add `MemorySettings` class to `src/assistant/config.py` (see §2.4).
- Add `memory: MemorySettings = Field(default_factory=MemorySettings)`
  to `Settings`.
- Add `vault_dir` and `memory_index_path` properties on `Settings`.
- Tests: extend `tests/test_config.py` (or add one) — assert defaults
  land under `data_dir/vault` and `data_dir/memory-index.db`; assert
  `MEMORY_VAULT_DIR=/tmp/x` env override works; assert
  `MEMORY_MAX_BODY_BYTES=1000` override works.

```bash
uv run pytest tests/test_config.py -x
```

### Step 2.0 — **Pre-flight test audit (B1 closure)**

Before touching `bridge/claude.py`, audit existing phase-2/3 tests that
mock SDK streams and assert row counts / shapes. After the fix,
`UserMessage([ToolResultBlock])` inputs will persist new rows
(`block_type='tool_result'`) that never existed before. Count-based
asserts may flip.

Concrete audit recipe (coder runs this as first action):

```bash
# 1. Enumerate tests that reference DB state or row shape.
grep -rn "conversations\|append\|block_type\|content_json" tests/ \
  | cut -d: -f1 | sort -u

# 2. For each file, check whether its mock SDK stream injects a
# UserMessage with ToolResultBlock content. Those tests need review.
grep -rln "UserMessage\|ToolResultBlock" tests/

# 3. For each "row count" assert, confirm it filters by block_type
# or by role; if it counts unfiltered rows, it's at risk.
grep -rn "fetchall\|len(rows)\|len(cursor)\|COUNT(\*)" tests/
```

**Explicit tests expected to need attention (v1 → v2 review):**

| Test file | Risk | Mitigation |
|---|---|---|
| `tests/test_bridge_mock.py` | Mock SDK stream may now include `UserMessage([ToolResultBlock])`; row count assertions become stale | Add filter `WHERE block_type IN ('text','tool_use')` or assert explicit `block_type='tool_result'` presence in the new post-fix baseline. |
| `tests/test_handler_meta_propagation.py` | Asserts meta flows; doesn't count rows directly — low risk. | Verify, don't edit unless a new row causes a `_classify` path assertion to flip. |
| `tests/test_load_recent_turn_boundary.py` | Turn-boundary slicing; if its seed data has no tool_result rows, it's unaffected. | Re-run; update seed helper call if needed. |
| `tests/test_interrupted_turn_skipped.py` | Similar — unaffected. | Re-run. |
| (new) `tests/test_bridge_per_skill_allowed_tools.py` | Does not use DB — unaffected. | — |
| (new) `tests/test_bridge_history_replay_snippet.py` | Uses seed helper from Step 2.5; depends on bridge fix landing first. | Sequential ordering enforced by Step 2 → Step 3. |

**Expected shift after the bridge fix lands:** phase-3 baseline of
282 passing tests may grow to ~285-310 as the new `tool_result` row
path is exercised. If any previously-green test flips red, the fix
is NOT to add `if block_type != 'tool_result': ...` everywhere — the
fix is to update that test's assertion to match the new, correct,
richer history shape.

### Step 2 — `bridge/claude.py` UserMessage branch (unblocks B2 persistence)

- Add `UserMessage` + `ToolResultBlock` imports.
- After the `AssistantMessage` branch, add the `UserMessage` branch
  that yields `ToolResultBlock` instances (see §2.1).
- Phase 2 handler's `_classify(ToolResultBlock)` already handles them;
  no handler changes needed for this step.

Tests (two new files; the first is the positive-flow assertion, the
second is the B1 regression gate):

1. `tests/test_bridge_yields_user_tool_result.py` — feeds a mock
   SDK stream `[SystemMessage(init), AssistantMessage([ToolUseBlock]),
   UserMessage([ToolResultBlock(content="{\"ok\":true}", is_error=False)]),
   ResultMessage]` through a monkeypatched `query`; asserts the bridge
   yielded exactly one `ToolResultBlock` (+ the expected 1
   `ToolUseBlock` from the earlier AssistantMessage).

2. `tests/test_bridge_persists_toolresult_from_usermessage.py` — E2E
   with mock SDK stream and real `ConversationStore`; after turn
   completes, `SELECT * FROM conversations WHERE block_type='tool_result'`
   returns exactly 1 row with `role='tool'` and `content_json` matching
   the shape `[{"type":"tool_result","tool_use_id":"...","content":"{\"ok\":true}","is_error":false}]`.

```bash
uv run pytest \
  tests/test_bridge_yields_user_tool_result.py \
  tests/test_bridge_persists_toolresult_from_usermessage.py \
  tests/test_bridge_mock.py -x
```

### Step 2.5 — **History seed helper (B3 closure)**

Before writing any history-replay test, create the helper that fabricates
the exact row shape without needing a live SDK turn. This lets all the
history tests (Step 3, Step 9) run fast and deterministic.

```python
# tests/_helpers/history_seed.py
from __future__ import annotations

import json
from typing import Any

import aiosqlite


async def seed_tool_use_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
) -> None:
    """Insert an `assistant`/`tool_use` row (and a `turns` row if missing)."""
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [{
        "type": "tool_use",
        "id": tool_use_id,
        "name": tool_name,
        "input": tool_input or {},
    }]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'assistant', ?, 'tool_use')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )


async def seed_tool_result_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    tool_use_id: str,
    content: str | list[dict[str, Any]] | None,
    is_error: bool = False,
) -> None:
    """Insert a `tool`/`tool_result` row matching a prior tool_use_id."""
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [{
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'tool', ?, 'tool_result')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )


async def seed_user_text_row(
    conn: aiosqlite.Connection,
    *,
    chat_id: int,
    turn_id: str,
    text: str,
) -> None:
    await _ensure_turn(conn, chat_id=chat_id, turn_id=turn_id)
    payload = [{"type": "text", "text": text}]
    await conn.execute(
        "INSERT INTO conversations(chat_id, turn_id, role, content_json, block_type) "
        "VALUES (?, ?, 'user', ?, 'text')",
        (chat_id, turn_id, json.dumps(payload, ensure_ascii=False)),
    )


async def _ensure_turn(
    conn: aiosqlite.Connection, *, chat_id: int, turn_id: str,
) -> None:
    """Idempotent insert of a completed `turns` row matching FK."""
    async with conn.execute(
        "SELECT 1 FROM turns WHERE turn_id = ?", (turn_id,),
    ) as cur:
        if await cur.fetchone():
            return
    await conn.execute(
        "INSERT INTO turns(turn_id, chat_id, status, started_at, completed_at) "
        "VALUES (?, ?, 'complete', strftime('%Y-%m-%dT%H:%M:%SZ','now'), "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (turn_id, chat_id),
    )
```

All Step-3 history tests import from `tests/_helpers/history_seed` and
call `seed_tool_use_row(turn_id="t1")` then
`seed_tool_result_row(turn_id="t2")` to exercise B2's cross-turn lookup.

### Step 3 — `bridge/history.py` synthetic summary (Q1 closure)

- Replace `history_to_user_envelopes` body with §2.2 version.
- Add `_stringify_tool_result_content` and `_render_tool_summary`
  helpers.
- Wire `tool_result_truncate` through `ClaudeBridge` (read from
  `settings.memory.history_tool_result_truncate_chars` inside
  `ask()` and pass to `history_to_user_envelopes`).

Tests (all use `tests/_helpers/history_seed.py` from Step 2.5):
- `tests/test_bridge_history_replay_snippet.py` — 3 cases:
  (1) short str content round-trips verbatim; (2) content >2000 chars
  gets `...(truncated)` suffix; (3) `is_error=True` row renders as
  `ошибка <tool>: <text>` prefix. Cyrillic UTF-8 case included
  (content=`"найдено 3 заметки: жена жене женой"`).
- `tests/test_history_stringify_defensive_list.py` — synthetic
  `content=[{"type":"text","text":"X"},{"type":"image"}]` renders as
  `"X[image block]"`.
- `tests/test_history_toolname_map_spans_turns.py` (B2 regression):
  `seed_tool_use_row(turn_id="t1", tool_use_id="TU1", tool_name="memory")`
  then `seed_tool_result_row(turn_id="t2", tool_use_id="TU1",
  content="snip")`; call `history_to_user_envelopes(rows, chat_id)` →
  yielded envelope for `t2` contains `"результат memory: snip"` **not**
  `"результат unknown: snip"`.

```bash
uv run pytest \
  tests/test_bridge_history_replay_snippet.py \
  tests/test_history_stringify_defensive_list.py \
  tests/test_history_toolname_map_spans_turns.py -x
```

### Step 4 — `bridge/claude.py` per-skill allowed-tools intersection

- Add `_GLOBAL_BASELINE` module constant.
- Add `_effective_allowed_tools` helper (see §2.3).
- Rewrite `_build_options` to compute allowed_tools from manifest.
- Emit `allowed_tools_computed` log line (helps debug S-A.2 advisory
  semantics).

Tests:
- `tests/test_bridge_per_skill_allowed_tools.py`:
  - empty manifest → baseline.
  - ping=[Bash] + skill-installer=[Bash] + memory=[Bash, Read] →
    `['Bash', 'Read']`.
  - missing `allowed-tools` key (None) contributes baseline → test
    with 1 `None` skill returns baseline.
  - explicit `[]` contributes nothing — but with another permissive
    skill, baseline still returned.

```bash
uv run pytest tests/test_bridge_per_skill_allowed_tools.py -x
```

### Step 5 — `tools/memory/_lib/` stdlib helpers

Create in this order (bottom up, each with its own unit tests):

- `tools/memory/_lib/__init__.py` (empty).
- `tools/memory/_lib/paths.py` — `validate_path(rel) -> Path` (see
  detailed-plan §3) + `_VAULT_SCAN_EXCLUDES` + `_should_skip_vault_path`
  (see detailed-plan §G2).
- `tools/memory/_lib/frontmatter.py` — parse/serialize YAML +
  `_sanitize_body` (see §2.6) + tags normalization (detailed-plan G4).
- `tools/memory/_lib/vault.py` — `atomic_write` + `read_note` +
  `list_notes` (respect excludes).
- `tools/memory/_lib/fts.py` — `_ensure_index` + `upsert_index` +
  `search` + `reindex` + `vault_lock(db_path)` context manager using
  `fcntl.flock` on `<db_path>.lock` (detailed-plan §2).

Tests (split per lib, same naming as detailed-plan §9):
- `test_memory_cli_write.py`, `test_memory_cli_search.py`, etc.
- Plus these tests targeting implementation details:
  - `test_memory_list_skips_obsidian_metadata.py` — write `.obsidian/workspace.json`
    inside vault; `list` returns no rows for it.
  - `test_memory_frontmatter_roundtrip.py` — 3 forms of `tags:` (string,
    list, null) all round-trip to `list[str]`.
  - `test_memory_write_body_with_frontmatter_marker_sanitized.py` —
    body with literal `---` line gets indented.
  - `test_memory_write_rejects_oversize_body.py` — body >1 MB exits 3.
  - `test_lock_released_after_kill.py` — `subprocess.Popen` holding the
    lock via a sleep-loop, `os.kill` it, second Popen acquires lock
    cleanly.

```bash
uv run pytest tests/test_memory_cli_*.py tests/test_memory_fts5_*.py tests/test_memory_frontmatter_roundtrip.py tests/test_memory_concurrent_writes.py tests/test_lock_released_after_kill.py -x
```

### Step 6 — `tools/memory/main.py` (CLI wiring)

- argparse subcommands: `search`, `read`, `write`, `list`, `delete`,
  `reindex`.
- Exit codes per detailed-plan §X2: `0/2/3/4/5/6/7`.
- JSON stdout (detailed-plan §X3).
- `vault_lock` wraps every write/delete/reindex.

Tests: `test_memory_cli_write.py` + happy-path + exit-code matrix.

### Step 7 — `skills/memory/SKILL.md`

- frontmatter `allowed-tools: [Bash, Read]` + Russian-language
  description with wikilink/area guidance (detailed-plan §7).

Test: `tests/test_skill_memory_frontmatter.py` — parses the file,
asserts `meta["allowed_tools"] == ["Bash", "Read"]`, `meta["name"] == "memory"`.

### Step 8 — system prompt update

- Add `{vault_dir}` variable and memory-first block to
  `bridge/system_prompt.md`.
- `_render_system_prompt` escapes `{}` in all interpolated values
  (§2.9 G6).

Tests:
- `tests/test_system_prompt_render_escapes_braces.py` — description
  with literal `{foo}` does NOT raise KeyError.
- `tests/test_system_prompt_contains_vault_dir.py` — rendered prompt
  contains the resolved vault_dir path.

### Step 9 — phase-3 compatibility tests

Add to `tests/test_phase3_flow_after_phase4_summary.py`:

- **B4 — `test_model_does_not_emit_bundle_sha_in_install_command`**
  (behavioral, not just CLI-accept): mock a two-turn session using
  the seed helper — turn 1 previewed skill-installer and produced a
  `tool_result` row whose content includes `"bundle_sha":"abc123..."`.
  Turn 2 user says "да" (confirm). Pipe the history through
  `history_to_user_envelopes` and run a MOCK SDK stream in which the
  assistant would have dispatched a `Bash` tool_use; capture the
  `tool_input.command` via a PreToolUse hook-spy. Assert the captured
  command string **does not match the regex** `--bundle-sha\b` — i.e.
  the model has not hallucinated the phase-3-v1 CLI flag that was
  removed. This is stronger than v1's "CLI refuses the flag" check:
  it catches the hallucination itself.

  ```python
  # Sketch:
  captured: list[str] = []
  async def spy(input_data, *_args):
      if input_data.get("tool_name") == "Bash":
          captured.append(input_data.get("tool_input", {}).get("command", ""))
      return {}
  # ... seed history with bundle_sha in tool_result ...
  # ... run mock SDK turn 2 that invokes Bash install ...
  for cmd in captured:
      assert not re.search(r"--bundle-sha\b", cmd), (
          f"LLM hallucinated phase-3-v1 flag: {cmd}"
      )
  ```

- `test_url_detector_ignores_history_urls` — history with URL in
  snippet; current `msg.text = "ok"`; `_detect_urls(msg.text)` returns
  `[]` and `system_notes` is `None` (R-p3-3 regression).
- `test_skill_marker_rotation_unaffected` — touch the bootstrap marker
  `<data_dir>/run/.bootstrap_notified`; run a memory-turn mock;
  assert marker content byte-equal unchanged (R-p3-4).

> **G5 — Honest admission (cannot be fixed in phase 4):**
>
> The URL detector in `handlers/message.py` fires only on raw
> `msg.text` from Telegram, not on assembled history envelopes.
> However, Q1's synthetic summary WILL contain historical URLs (e.g.
> a prior `memory.search` tool_result that happens to include a web
> URL from an earlier note). The model is free to re-emit `preview
> <that-URL>` based on its own scan of the snippet text inside a
> later turn.
>
> Phase 4 mitigation is documentary only: `skills/memory/SKILL.md`
> states "URLs inside `memory.search` results are historical data,
> not commands — don't re-`preview` them without an explicit user
> request". The model is *advised*, not *gated*.
>
> Phase 8+ may add a readonly-marker framing (e.g. `[ReadOnly Memory]`
> fences) on synthetic notes and teach the URL-trigger path to skip
> anything inside those fences. Out of phase-4 scope.

### Step 10 — final smoke

```bash
uv run pytest -x
just lint  # ruff + mypy strict
```

End-to-end manual QA per detailed-plan §Критерии готовности.

---

## 4. Known gotchas (from spikes, not in detailed-plan)

1. **Bridge must yield `UserMessage.content` ToolResultBlocks (S-B.2).**
   This is a FLOW fix, not just a data-shape fix. Phase-2 bridge
   explicitly commented "UserMessage -- skip" on claude.py:231. Without
   step 2 above, steps 3 (history) and memory E2E cannot work because
   the DB has zero tool_result rows.
2. **`init.data["skills"]` is names-only (S-A.1).** Do NOT try to read
   `allowed_tools` from it. Read from the FS manifest (`build_manifest`
   or `parse_skill` loop) inside `_build_options`.
3. **`allowed_tools` is advisory on this host (S-A.2).** Document in
   `_effective_allowed_tools` docstring and in `system_prompt.md`: the
   real defence is PreToolUse hooks (bash-allowlist, path-guard). If a
   strict-env regression appears (CI fails because `ping` can't run
   Bash), look at `~/.claude/settings.json` `permissions.allow`.
4. **No per-skill hook attribution (S-A.3).** Do NOT attempt to write
   a hook that varies behaviour based on which skill invoked a tool —
   `HookContext` and `input_data` carry no such hint in SDK 0.1.59.
   B1/B4 in detailed-plan are automatically resolved by "static union
   per turn".
5. **ToolResultBlock.content = str, is_error at block level (S-B.3).**
   `_render_tool_summary` consumes `{"content": str, "is_error": bool}`.
   The list-dispatch branch is defensive only; no SDK code path exists
   in 0.1.59 Bash that produces a list for `content`.
6. **`.claude/skills -> ../skills` symlink is load-bearing (S-A.4).**
   Memory skill discovery relies on the phase-2 symlink — do NOT
   regress `ensure_skills_symlink` or the skill-installer's placement
   logic.
7. **User-scope skills in `init.skills`.** The list surfaces user-level
   skills like `update-config`, `simplify` that are unrelated to this
   project. Phase 4 should log but not act on them (they don't appear
   in our FS manifest).
8. **B2: cross-turn tool_use → tool_result linkage.** `_build_tool_name_map`
   in history.py scans the WHOLE history, not per-turn — a tool_use
   emitted in turn N's assistant row with its matching tool_result
   landing in turn N+1's user row still resolves to the right name.
9. **G1 log surface: `allowed_tools_union_collapsed_to_baseline`.**
   If a skill has missing `allowed-tools` frontmatter, the union
   collapses to full baseline and we WARN with the offender's name.
   Operators: `grep allowed_tools_union_collapsed_to_baseline /var/log/...`
   to audit which skill broke the narrowing.
10. **G6: `HISTORY_MAX_SNIPPET_TOTAL_CHARS` NOT in phase 4.** Worst-case
    context with 10-turn history × 3 tool_results × 2000 chars = 60 KB
    ≈ 20 K tokens. Still fits 200 K context for single-user. Phase 5
    scheduler (auto-injected turns) will revisit — truncate oldest
    snippets first if total > budget.
11. **S1 hard limit (bears repeating).** `_effective_allowed_tools` is
    advisory on hosts with permissive user-level permissions. Real
    gates = PreToolUse hooks + no per-skill dispatch. Memory is NOT
    sandboxed.
12. **S3 `_probe_lock_semantics` at startup.** On SMB / iCloud / FUSE
    the memory CLI exits 5 before writing. CI escape hatch:
    `ASSISTANT_SKIP_LOCK_PROBE=1`.

---

## 5. Tests — per-test asserts

Phase-4 hard floor: 25 new tests + 3 phase-3 compatibility tests (up
from 17+3 in v1 — v2 wave added 8 tests for B1/B2/B3/B4/G1/G3/S3).
Each row below gives a concrete assert. Coder may add more coverage.

| File | Key assert |
|---|---|
| `test_memory_cli_write.py` | `proc = run(["python","tools/memory/main.py","write","inbox/a.md","--title","T","--body","-"], input="hello")` → `rc==0`; `(vault/inbox/a.md).exists()`; frontmatter YAML parses to `{title: "T", created: <iso>}`. |
| `test_memory_cli_search.py` | Write a note with body `"day is Tuesday"`; `search "Tuesday"` → `{"ok": true, "data": {"hits": [{"path":"inbox/a.md", ...}]}}`. |
| `test_memory_cli_read.py` | `read inbox/a.md` → JSON with `frontmatter.title == "T"`, `body` contains original markdown, `wikilinks == []`. Missing path → `rc==7`. |
| `test_memory_cli_list.py` | Write 2 notes in different areas; `list --area inbox` returns exactly 1 entry. |
| `test_memory_cli_delete.py` | Write + delete; `read` on deleted path → `rc==7`. |
| `test_memory_cli_reindex.py` | Write, corrupt index by `DELETE FROM notes_fts`, `reindex` → `search` hits restored. |
| `test_memory_fts5_roundtrip.py` | Write `{title:"Wife Birthday", body:"3 апреля"}`; `search "Wife"` → 1 hit; `search "3 апреля"` → 1 hit. |
| `test_memory_fts5_cyrillic.py` | Body `"жена жене женой"`; `search "жены"` returns hit (porter stemming). |
| `test_memory_concurrent_writes.py` | 2 × `subprocess.Popen` writing distinct paths in parallel (Popen, not threads — `fcntl.flock` is process-level on macOS); both final exit 0, both files exist, `search` returns 2 hits. |
| `test_lock_released_after_kill.py` | Popen #1 holds lock via `--sleep 10` hidden flag (or a stub that acquires + time.sleep); SIGKILL; Popen #2 acquires within 500ms. |
| `test_memory_frontmatter_roundtrip.py` | Write 3 notes with `tags: foo` / `tags: [a,b]` / `tags:` (null); read each → `tags` is always `list[str]`. |
| `test_memory_wikilinks_preserved.py` | Body `"see [[target-note]] for context"`; `read` returns body verbatim and `wikilinks == ["target-note"]`. |
| `test_memory_collision_exit6.py` | Two sequential `write inbox/a.md` → second `rc==6`; stderr JSON `{"ok": false, "error": "collision..."}`. Third with `--overwrite` → `rc==0`. |
| `test_memory_atomic_write_fsync.py` | Monkey-patch `os.rename` to raise; target NOT created; `<vault>/.tmp/*` cleaned up (no orphan tmp). |
| `test_memory_vault_path_outside_project.py` | `settings.vault_dir = /tmp/x`; call the phase-2 file-guard with `/tmp/x/inbox/a.md`; deny. |
| `test_memory_list_skips_obsidian_metadata.py` | `.obsidian/workspace.json` written under vault; `list` returns `[]`. |
| `test_memory_write_rejects_oversize_body.py` | Body 2 MB; `rc==3`; stderr mentions `MEMORY_MAX_BODY_BYTES`. |
| `test_memory_write_body_with_frontmatter_marker_sanitized.py` | Body with literal `---\n` at col 0 → written as ` ---\n` (one-space indent); `read` returns `body` containing leading-space `---`. |
| `test_bridge_history_replay_snippet.py` | Row list with `tool_use.name=memory`, `tool_result.content="{\"hits\":[...]}"`, tool_result.is_error=False → yielded envelope content contains `результат memory: {"hits"...` prefix. Second case: `content` length 5000 → truncated at 2000 with `...(truncated)`. Third case: `is_error=True` → prefix `ошибка memory:`. Cyrillic case: body with `жена` preserved. |
| `test_history_stringify_defensive_list.py` | Call `_stringify_tool_result_content([{"type":"text","text":"A"},{"type":"image","url":"x"}])` → `"A[image block]"`. |
| `test_bridge_per_skill_allowed_tools.py` | `manifest=[{allowed_tools:["Bash"]},{allowed_tools:["Read"]}]` → `_effective_allowed_tools == ["Bash","Read"]`. `manifest=[{allowed_tools:None}]` → baseline sorted. `manifest=[]` → baseline. `manifest=[{allowed_tools:[]}, {allowed_tools:["Bash"]}]` → `["Bash"]`. |
| `test_bridge_yields_user_tool_result.py` | Mock SDK stream with `UserMessage([ToolResultBlock(content="X", is_error=False)])` → bridge yields one `ToolResultBlock`. |
| `test_bridge_persists_toolresult_from_usermessage.py` (B1) | E2E with mock stream; after turn completes, `SELECT content_json FROM conversations WHERE block_type='tool_result'` returns exactly 1 row matching `[{"type":"tool_result",...}]`. |
| `test_history_toolname_map_spans_turns.py` (B2) | Seed `tool_use(turn=t1, id=TU1, name=memory)` + `tool_result(turn=t2, tu_id=TU1, content="snip")`; rendered envelope for t2 contains `"результат memory: snip"`, not `"unknown"`. |
| `test_memory_lock_probe_exit5_on_noop_fs.py` (S3) | Monkeypatch `_probe_lock_semantics` → `False`; run `_ensure_index` → `sys.exit(5)` with stderr JSON `{"ok":false, "error":"fcntl.flock is advisory-only..."}`. |
| `test_memory_lock_probe_skip_env_bypass.py` (S3) | `ASSISTANT_SKIP_LOCK_PROBE=1` env set; `_probe_lock_semantics` returning `False` is ignored; normal flow proceeds. |
| `test_memory_vault_dir_mode_0o700.py` (G3) | `_ensure_vault` on fresh dir → stat mode & 0o077 == 0. Pre-existing dir with mode 0o755 → no chmod, but log `vault_dir_permissions_too_open`. |
| `test_allowed_tools_union_collapsed_log.py` (G1) | Manifest with one `allowed_tools=None` skill → `_effective_allowed_tools` returns baseline AND emits `allowed_tools_union_collapsed_to_baseline skills=[<name>]` log at WARN. |
| `test_phase3_flow_after_phase4_summary.py::test_model_does_not_emit_bundle_sha_in_install_command` (B4 behavioral) | Seed history with tool_result containing `bundle_sha=abc123`; run mock turn 2 install; captured Bash `tool_input.command` via PreToolUse spy does NOT match regex `--bundle-sha\b`. |
| `test_skill_memory_frontmatter.py` | `parse_skill(skills/memory/SKILL.md)` → `meta["allowed_tools"] == ["Bash","Read"]`, `meta["name"] == "memory"`. |
| `test_system_prompt_render_escapes_braces.py` | Write SKILL.md with `description: "uses {x}"`; `_render_system_prompt()` returns without `KeyError`; result string contains the literal `{x}`. |
| `test_system_prompt_contains_vault_dir.py` | Assert rendered prompt contains `str(settings.vault_dir)` exactly once. |
| `test_phase3_flow_after_phase4_summary.py::test_url_detector_ignores_history_urls` | History contains URL in a synthetic snippet; `msg.text = "ok"`; `_detect_urls(msg.text)` returns `[]`. |
| `test_phase3_flow_after_phase4_summary.py::test_skill_marker_rotation_unaffected` | Touch `<data_dir>/run/.bootstrap_notified`; run a memory-turn; marker content unchanged. |

---

## 6. Citations

- Python `str` slice on code-point boundary (UTF-8 safe):
  <https://docs.python.org/3.12/library/stdtypes.html#string-methods>.
- Python `sqlite3` FTS5 virtual table:
  <https://www.sqlite.org/fts5.html>; `porter unicode61 remove_diacritics 2`
  tokenizer documented at <https://www.sqlite.org/fts5.html#unicode61_tokenizer>.
- `fcntl.flock` semantics (process-level, POSIX):
  <https://docs.python.org/3.12/library/fcntl.html#fcntl.flock>.
- `shutil.copytree(symlinks=True)` preserving symlinks:
  <https://docs.python.org/3.12/library/shutil.html#shutil.copytree>
  (phase-3 spike §S4).
- Anthropic `claude-agent-sdk` `ToolResultBlock`/`UserMessage`/
  `AssistantMessage` dataclass shapes — SDK 0.1.59 source
  (`inspect.getsource`) captured in `spike-findings.md §S-B`.
- Phase-2 `bridge/claude.py:229-231` (the "UserMessage -- skip" comment)
  — canonical reference for the flow gap resolved in §2.1.
- Phase-3 `plan/phase3/spike-findings.md §S3` — PostToolUse hook input
  keys reused here to document `input_data` shape (identical modulo
  `tool_response`).
- Phase-2 `bridge/bootstrap.py::ensure_skills_symlink` — reference for
  S-A.4 skills-discovery mechanism.

---

## 7. What remains UNVERIFIED after v1

- **U4-1.** Behaviour of `options.allowed_tools=["Read"]` in a strict
  env where `~/.claude/settings.json` has NO `permissions.allow` key.
  Predicted: SDK will deny Bash calls. Not observed on this host.
  Operator who runs phase-4 in CI / service account should re-run
  `spikes/sdk_per_skill_hook.py` to confirm.
- **U4-2.** Behaviour of `ToolResultBlock.content` when the tool is
  `Read` on a binary file or `WebFetch` on a large HTML page.
  Predicted: SDK may switch to `list[dict]` with `{"type":"image"}`
  or `{"type":"text", ...}` blocks. Phase-4 `_stringify_tool_result_content`
  has a defensive list-branch; add a `phase-5` reminder to extend it
  once empirical traffic shows up.
- **U4-3.** Cross-FS atomicity of `os.rename` when `<vault_dir>` is on
  a different filesystem than `<vault_dir>/.tmp`. The detailed-plan
  requires same-FS by placing `.tmp/` inside the vault root — verified
  by the `test_memory_atomic_write_fsync` test path. Operator who sets
  `MEMORY_VAULT_DIR` to an NFS / iCloud path is outside the warranty
  (description.md §Warning).
- **U4-4.** `fcntl.flock` silent-no-op on SMB / iCloud / Dropbox
  mounts — phase-4 description §Warning documents this. No test
  exercises it; single-user bot on local FS is the target.
- **U4-5.** Obsidian's own in-place edit of SKILL.md (not via memory
  CLI) would skip sentinel-touch; manifest cache misses. Detailed-plan
  R3 defers FS watcher to phase 5; no phase-4 test.
