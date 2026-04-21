# Phase 3 — Unverified Assumptions / Nice-to-Have Follow-ups

Issues discovered during planning + devil's-advocate waves + RQ1 spike that
are **not blockers** for phase-3 ship but should be re-examined in
phase 4+. Ordered roughly by risk.

## NH-1 — `.0xone-installed` marker gating (BL-2 invariant)

**Status:** designed, unit-tested via `test_installer_tool_*`.
**Risk:** A crash mid-rename leaves `skills/<name>/` populated without
the marker; next boot sees marker absent and re-attempts bootstrap —
this is the intended behavior but there is no _integration_ test that
actually induces a mid-rename crash (requires injecting an OSError
between the two `os.rename` calls).
**Mitigation:** unit test monkeypatches the second `rename` to raise;
covered in `test_installer_tool_skill_install.py::test_partial_install_rollback`.
**Follow-up:** phase 5 scheduler could sweep `skills/.tmp-*` directories
left by interrupted installs.

## NH-2 — GitHub anonymous rate limit (60 req/hour)

**Status:** acknowledged; error surfaced to owner.
**Risk:** `marketplace_list` + `marketplace_info(name)` for every skill
in one owner session could hit 403 / "rate limit exceeded". Bot
surfaces this as `{"code": 10, "error": "GitHub rate-limited: ..."}`
with remedy (`gh auth login` → 5000/hour).
**Mitigation:** `_gh_api_async` detects `"rate limit"` in 403/429 body
and throws a targeted `MarketplaceError`. Test:
`test_installer_marketplace_rate_limit.py`.
**Follow-up:** phase 8 gh-auth integration could auto-authenticate
(owner's gh session, not OAuth PAT) and lift this cap.

## NH-3 — `ResultMessage.model` absence (carried over from phase 2)

**Status:** phase-2 decision (S2 fix) already migrates to capturing
`model` from `AssistantMessage.model` inside the stream loop.
**Risk:** none for phase 3 — installer @tool responses don't depend on
model metadata.
**Follow-up:** none.

## NH-4 — `ToolResultBlock` arrives in `UserMessage`

**Status:** phase-2 already handles this correctly (`B5` fix:
`ToolResultBlock → role='user', block_type='tool_result'`).
**Risk:** phase-3 test helpers that check tool output markers MUST
iterate both `AssistantMessage` and `UserMessage` content. Documented
in implementation.md §4 NH-4 + the `_drive()` idiom in the RQ1 probe.
**Follow-up:** none.

## NH-5 — `asyncio.create_task` orphan GC

**Status:** designed. `Daemon._spawn_bg` anchors tasks in
`self._bg_tasks: set[Task]`; `_installer_core._BG_TASKS` is a
module-global set.
**Risk:** if a coder forgets the anchor pattern in a future bg
helper, Python 3.12+ emits `RuntimeWarning` (not an error) and the
task may vanish mid-execution.
**Mitigation:** code-review checklist; lint rule idea (ruff's
`RUF006`).
**Follow-up:** consider enabling `RUF006` in `pyproject.toml` ruff
config once phase 3 merges.

## NH-6 — `yaml.safe_load` malformed frontmatter

**Status:** `validate_bundle` wraps in `try/except yaml.YAMLError →
ValidationError`. Covered by validator tests.
**Risk:** none.
**Follow-up:** none.

## NH-7 — `ToolSearch` pre-invoke overhead (new, from RQ1 obs 1)

**Status:** unexpected finding. Live probe showed the model auto-
invoked `ToolSearch` as a first `ToolUseBlock` in every query before
reaching `mcp__installer__skill_preview`.
**Risk:** (a) extra turn latency (~0.5-1s per query); (b) first-turn
cost includes `ToolSearch` result block; (c) tests that assert "first
`ToolUseBlock` is X" will fail.
**Mitigation:** implementation.md §3.4
`test_installer_mcp_registration.py` uses subset-assert; §4 RS-1
documents the behavior.
**Follow-up:** confirm whether `ToolSearch` is CLI-ambient (present
only when user's `~/.claude/settings.json` registers it) or always
injected by SDK preset. If ambient, it would vanish on a clean deploy
host. Re-test during phase-4 deploy smoke.

## NH-8 — `allowed-tools:` frontmatter is NO-OP in SDK

**Status:** phase-2 knowledge (gotchas memory file). Phase-3 emits
warnings `skill_permissive_default` (missing field) and
`skill_lockdown_not_enforced` (empty list) but does NOT actually gate
tools per-skill. `ClaudeAgentOptions.allowed_tools` hardcodes the
baseline + the 7 `mcp__installer__*` names.
**Risk:** skill authors writing `allowed-tools: [Bash]` expect
lockdown and don't get it.
**Mitigation:** warning + documentation.
**Follow-up:** phase-4+ could merge per-skill lists inside
`_build_options` IF the active skill set were inferred from the turn's
context (non-trivial — SDK does not tell us which skill is "active").

## NH-9 — `gh api` rc=0 on HTTP 404

**Status:** handled by `_parse_gh_json` + `MarketplaceError` on
`{"message": ..., "status": "404"}` body shape. Source: spike S2.d.
**Risk:** if gh ever changes its error body shape, the check could
silently miss 404s and return malformed data.
**Mitigation:** test `test_installer_marketplace_info.py` includes a
synthetic 404 case.
**Follow-up:** pin `gh >= 2.40` documented.

## NH-10 — Bash allowlist `shlex.split` on unbalanced quotes

**Status:** `_bash_allowlist_check` wraps `shlex.split(stripped)` in
`try/except ValueError → "unparseable Bash command"`.
**Risk:** a hostile string with unbalanced quotes used to crash; now
it is denied with a clear message.
**Follow-up:** none.

## NH-11 — First-turn cost dominated by init tool list

**Status:** observed. RQ1 Q1 cost $0.25 out of $0.39 total because the
CLI preset injects ~60 ambient tools (Task, TodoWrite, Figma/Gmail/
Drive/Calendar/Playwright MCPs) on top of our 7 installer tools.
**Risk:** phase-4 memory (+2 tools) + phase-8 gh (~10 tools) will grow
the init envelope further. At some threshold, cold-cache queries may
exceed comfortable latency / cost budget.
**Mitigation:** `exclude_dynamic_sections=True` already in
`system_prompt_preset`. Ambient CLI tools are outside our control.
**Follow-up:** phase 9 (ops polish) could explore
`disallowed_tools=[...]` to strip CLI-ambient tools we don't need.

## NH-12 — Sweeper runs ONCE at boot

**Status:** by design — `Daemon.start()` fire-and-forgets
`_core.sweep_run_dirs(data_dir)` and that's it.
**Risk:** long-running daemon (weeks) with lots of installs
accumulates `installer-cache/` up to the 7-day cap (~N × bundle
sizes).
**Mitigation:** per-install sweeper in `skill_install`'s `finally`
rmtree's the just-used cache entry (10 MB bound per bundle × how many
live installs at once → negligible).
**Follow-up:** phase-5 scheduler could run `sweep_run_dirs` hourly.

## NH-13 — Installer context is process-global

**Status:** `configure_installer(project_root, data_dir)` sets
module-level `_CTX` dict. Good enough because the daemon runs a
single `ClaudeBridge` per process.
**Risk:** if phase-5 scheduler ever spawns a separate event loop or
worker process with different paths, the `@tool` handlers would pick
up the wrong `project_root`.
**Mitigation:** `_need_ctx()` raises `RuntimeError` on missing
config; test `test_installer_mcp_registration.py::test_missing_configure_raises`.
**Follow-up:** phase 5 (scheduler) — decide whether scheduler runs in
the same process or a sidecar; if sidecar, pass config via env vars or
refactor `@tool`s to use `ContextVar`.

## NH-14 — RQ1 `C6` tolerated junk `mcpServers` in `settings.local.json`

**Status:** observed. `.claude/settings.local.json` declaring
`mcpServers: {"external_stub": {"command": "/bin/echo"}}` was
silently ignored by SDK; programmatic `mcp_servers=` took precedence.
**Risk:** future SDK version might START honouring disk-declared
mcpServers and spawn them — potentially shadowing our `installer`
server.
**Mitigation:** `assert_no_custom_claude_settings` in
`bridge/bootstrap.py` already blocks load-bearing keys (hooks,
permissions.deny). `mcpServers` is NOT in that list today.
**Follow-up:** add `mcpServers` to the load-bearing block-list IF a
future SDK point-release changes behavior. Detected during
`requires_claude_cli` upgrade smoke.

## NH-15 — `installer.py` + `_installer_core.py` import-time side
effects

**Status:** `installer.py` calls `create_sdk_mcp_server(...)` at
module load. Importing `assistant.tools_sdk.installer` thus constructs
a `McpSdkServerConfig`. No network IO, no file IO — just dataclass
wiring. Import is idempotent.
**Risk:** none observed.
**Mitigation:** `configure_installer` is mandatory; `_need_ctx`
raises on missing config → wouldn't silently install anywhere weird
even if imported early.
**Follow-up:** none.

## NH-16 — `skill_preview` re-fetch on same URL within 7-day window

**Status:** `skill_preview` unconditionally re-fetches and rewrites
`manifest.json` even if a fresh `manifest.json` exists. This is fine
because TOCTOU detection happens in `skill_install` regardless.
**Risk:** wasted bandwidth if user previews the same URL multiple
times in quick succession.
**Mitigation:** acceptable for single-user bot.
**Follow-up:** could skip re-fetch if
`manifest_path.stat().st_mtime < 60s ago`, but adds complexity for
marginal benefit.

## NH-17 — PostToolUse sentinel fires on ANY Write under tools/

**Decision:** per Q11 owner chose skills/+tools/ scope. `make_posttool_hooks`
matcher = "Write"|"Edit", inner filter `/skills/` or `/tools/<X>/` in
`file_path`.

**Cost:** manifest rebuild on every `tools/<X>/*.json` cache Write. ~5ms
overhead per turn.

**Mitigation (future):** restrict matcher to `SKILL.md` + `main.py` +
`pyproject.toml` patterns via `file_path` regex inside the hook. Not
urgent — single-user, low throughput.

## NH-18 — DNS rebinding residual risk in `_raw_single_file_async`

`urllib.request.urlopen` follows redirects automatically. If
`raw.githubusercontent.com` 303-redirects to an attacker-controlled host
(unlikely for GitHub), `check_host_safety` on the initial host does not
apply to the redirect target.

**Mitigation:** `_fetch_file_bytes` re-checks `download_url` host;
`_raw_single_file_async` does NOT. Parallel to phase-2 U9. Acceptable for
single-user; audit quarterly.

## NH-19 — Bundle validator does not gate non-Python file types

`validate_bundle` AST-parses only `*.py` files. Bundles may include
`.sh`, `.js`, `.toml`, arbitrary data files. These are NOT sandbox-checked.

**Mitigation:** the Bash allowlist only runs `python3 skills/...`,
`uv run skills/...`, `python tools/...` — so `.sh` / `.js` inside a
bundle won't be executed via the model. Python `main.py` loaded at
runtime may read arbitrary bundle files; that is by design
(trust-the-bundle after confirm).

## NH-20 — `.handler` bypass in unit tests misses MCP schema validation

Tests invoke `skill_preview.handler({"url": url})` directly. The SDK's
MCP boundary (`call_tool`) validates `arguments` against `inputSchema`
before dispatching to the handler. Unit tests bypass this.

**Mitigation:** add one integration-style test that goes through
`ClaudeBridge.ask` with a synthetic prompt "call skill_preview with
url=X" — validates end-to-end schema enforcement. Deferred to phase 3.1
if coverage gap surfaces.

## NH-21 — `_bootstrap_skill_creator_bg` 120s budget optimistic for slow network

17-file skill-creator bundle: happy path ~5-10s. Worst case (slow
network, 30s per request, 17 files) exceeds 120s → bootstrap aborted,
marker not written.

**Mitigation:** next boot retries (idempotent). Owner sees stale
`log.warning`. **Future enhancement:** bot-initiated "bootstrap failed,
retry?" notification after phase 5 scheduler/UDS lands.

## NH-22 — `sha256_of_tree` ignores empty dirs, only hashes file contents+names

If an attacker bundle adds an empty `.git/` directory, the hash is
unchanged. Low-severity (empty dirs carry no code).

**Mitigation:** current `atomic_install` strips `.git` anyway; empty-dir
bundle contents don't affect execution. Accepted.

---

Nice-to-haves for **phase 4**:

- Add `test_installer_mcp_registration.py` marker-based live probe
  against a clean host (no ambient Figma/Gmail/etc. MCPs) to confirm
  NH-7 ToolSearch is ambient-CLI, not SDK-intrinsic.
- Add `RUF006` to ruff config.
- Bump `assert_no_custom_claude_settings` block-list iff NH-14
  materializes.
