# Phase 4 Code Review

## Executive summary

Production-quality implementation of the memory MCP server — path validation, atomic-write ordering, lock semantics, FTS5 query-builder safety, and nonce sentinel defence all correctly executed and well-tested at the unit level. **Verdict: fix before owner smoke.** Three non-blocking but real risks warrant fixes before shipping: (1) the PostToolUse audit hook dumps unbounded `tool_input` (including full 1 MiB `body` payloads) into `memory-audit.log` with world-readable default permissions, contradicting the file's own "keep audit log small" comment and leaking sensitive memory contents; (2) `memory_write` overwrite silently drops the original `created` date because the handler never reads back the existing frontmatter before re-serialising; (3) `tool_error` in `_memory_core.py` is a byte-for-byte clone of `_installer_core.tool_error` — the first time the pattern repeats, a shared module pays for itself.

## CRITICAL (block merge)

None. No security bypasses, data-loss paths, or crash surfaces found in the memory core itself.

## HIGH (fix before owner smoke)

### H1 | `src/assistant/bridge/hooks.py:691-697` | audit log dumps full `tool_input` including 1 MiB bodies

The `on_memory_tool` hook writes `tool_input` verbatim into `memory-audit.log`. For `memory_write` calls the model sends the entire `body` field (up to 1 MiB per the cap), which is then JSON-encoded into a single JSONL line. In normal operation each write doubles vault storage; over months the audit log becomes the largest file in `data_dir`. The in-file comment claims "keep audit log small — only keep is_error flag and a body-length signal rather than full snippet text" but the compaction is applied only to `tool_response`, not `tool_input`. The phase-4 plan explicitly said audit rotation is deferred; unbounded body capture is separate from rotation and should not be deferred.

**Fix:** compact `tool_input` with the same pattern — replace `body` with `body_len` (and optionally truncate path/title strings to a sane cap). Example:

```python
tool_input_compact = dict(tool_input)
if isinstance(tool_input_compact.get("body"), str):
    tool_input_compact["body_len"] = len(tool_input_compact.pop("body"))
```

### H2 | `src/assistant/bridge/hooks.py:698-701` | audit log created with default umask (likely `0o644`)

`audit_path.open("a", encoding="utf-8")` creates the file with whatever the process umask yields — typically `0o644` on macOS, world-readable. Memory audit contents include vault paths and (per H1) note bodies. For a single-user assistant running under the owner's UID this is low-severity; it becomes a real issue as soon as another user's process on the same host can read the file.

**Fix:** on first open, `os.chmod(audit_path, 0o600)` or open with a restrictive umask context. Also worth applying to `memory-index.db` / `.lock` since the same concern applies to raw note bodies in the index.

### H3 | `src/assistant/tools_sdk/memory.py:304-332` | overwrite silently loses the original `created` date

On overwrite=true, the handler always writes `created = str(args.get("created") or now_iso)`. The existing file's frontmatter is never read back, so the original `created` timestamp is replaced with `now_iso` (the same value as `updated`). This defeats the point of tracking creation time separately and makes Obsidian's sort-by-created metadata lie. Note: `created` is also NOT documented in the handler's JSON schema (`required: ["path", "title", "body"]`, properties list omits `created`), so the model cannot currently pass it via advertised args — which means every overwrite loses the date.

**Fix:** before serialising frontmatter on overwrite, attempt `parse_frontmatter(full.read_text(...))` and preserve `fm.get("created")` if present:

```python
if full.exists():
    try:
        existing_fm, _ = core.parse_frontmatter(
            full.read_text(encoding="utf-8")
        )
        preserved_created = existing_fm.get("created")
    except (OSError, ValueError):
        preserved_created = None
else:
    preserved_created = None
created = str(args.get("created") or preserved_created or now_iso)
```

### H4 | `src/assistant/tools_sdk/_memory_core.py:158-171` vs `_installer_core.py:113-129` | identical `tool_error` duplicated across modules

Byte-identical 17-line function. The pattern will repeat for every subsequent MCP server (phase 5+); the fix is cheap today and expensive once three copies exist. Error codes per-module are fine (they are MCP-server-local); only the envelope helper should move.

**Fix:** extract to `src/assistant/tools_sdk/_common.py` (or `core.py`):

```python
def tool_error(message: str, code: int) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"error: {message} (code={code})"}],
        "is_error": True,
        "error": message,
        "code": code,
    }
```

Both modules re-export locally for call-site readability.

## MEDIUM (nice-to-have, open issue)

### M1 | `src/assistant/tools_sdk/_memory_core.py:860-917` | `write_note_tx` rel_path param is dead

The function signature takes `rel_path: Path` but only the `del rel_path` at the very bottom touches it. The comment acknowledges this ("signature for call-site clarity") but the call site passes `rel` which is already derivable as `full_path.relative_to(vault_dir)`. Dead parameters invite future confusion.

**Fix:** drop `rel_path` from the signature and update the single call site in `memory.py:349-356`.

### M2 | `src/assistant/tools_sdk/_memory_core.py:425-427` | double-encode round-trip in `sanitize_body`

`cleaned = body.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="ignore")` then later `encoded = cleaned.encode("utf-8")` for the size check. For a 1 MiB body this is three full passes (encode, decode, encode). PyStemmer / FTS / YAML dwarf this, but the function is called on every write; use `len(cleaned.encode("utf-8"))` once or keep `encoded` alive and return it alongside. Minor.

### M3 | `src/assistant/tools_sdk/_memory_core.py:283-295` | `IsoDateLoader` docstring describes behaviour implemented outside the class

The class body is empty (only a docstring). All the ISO conversion lives in module-level `_timestamp_as_iso` + `IsoDateLoader.add_constructor(...)`. This works but reads as "a class that does nothing". Either move `_timestamp_as_iso` into the class (`@classmethod` constructor) or drop the docstring from the class and keep the commentary on the constructor. Readability only.

### M4 | `src/assistant/tools_sdk/_memory_core.py:370-400` | `_build_fts_query` does not sanitise against FTS5 reserved tokens inside quoted Latin phrases

The phrase-quoting strategy (`"text"`) is mostly safe, but an adversarial input like `AND` / `NEAR(5)` / `^column` inside the quoted string is fine (FTS5 treats phrase-quoted text as literal). However, a single `"` character in the raw Latin token would break the phrase quoting. Example: `raw_q = 'say "hi"'` → token `hi` is clean but a future tokenizer change or non-standard regex could let a literal `"` through. Defensive guard: drop `"` from tokens before the `f'"{low}"'` wrap, or use `\\"` escaping. Low probability given `_TOKEN_RE = r"[\w]+"` but the cost is two chars.

### M5 | `src/assistant/tools_sdk/_memory_core.py:789-794` | `reindex_under_lock` has no progress/cancel signal

A blocking `memory_reindex` on a 2000-note vault under lock can hold the lock for tens of seconds. Concurrent `memory_write` callers hit the 5s timeout and return `CODE_LOCK`. Acceptable for manual disaster recovery; flagging for a future phase to surface progress via logs or to expose `memory_reindex_cancel`.

### M6 | `tests/` | no integration-through-ClaudeBridge.ask test — acknowledged as known debt

Per review prompt and coder carry-over list: `test_memory_integration_ask.py` was deferred. Every current memory test invokes the handler directly via `mm.memory_write.handler(args)`, which bypasses MCP framing, the `@tool` JSON-schema validator, the SDK stream, the PostToolUse audit hook, and the Russian-through-Telegram path. Listing under known debt per instructions. Recommend one end-to-end smoke test in phase 5.

### M7 | `src/assistant/tools_sdk/_memory_core.py:1066-1083` | `extract_wikilinks` does not deduplicate or cap

A body with 10 000 `[[x]]` produces 10 000-entry list returned via MCP structured output. No upper bound.

**Fix:** `return list(dict.fromkeys(out))[:256]` (preserves order, dedupes, caps).

### M8 | `src/assistant/tools_sdk/_memory_core.py:920-957` | `delete_note_tx` rolls back DELETE but leaves recomputed `max_mtime_ns` coupled to the DELETE commit

If unlink succeeds and COMMIT fails, the doc-comment says the next auto-reindex will fix up. Fine. But if unlink SUCCEEDS and `_scan_vault_stats` raises (e.g. vault dir vanishes mid-delete), the function re-raises without rolling back — leaving the DELETE as a pending transaction on connection close. `conn.close()` with an open transaction implicitly rolls it back, so the index is fine, but the file is gone. End state: disk reality ahead of index (same as H2's "vault is authoritative"), so the next auto-reindex repairs. Worth a comment confirming this ordering is intentional.

### M9 | `src/assistant/tools_sdk/_memory_core.py:44` | PyStemmer `_STEMMER` initialised at import time — no guard against ImportError

The module-level `_STEMMER = Stemmer.Stemmer("russian")` runs at import. A PyStemmer wheel mismatch (e.g. ARM64 vs x86_64 CPython) or missing `.so` raises at `import assistant.tools_sdk.memory`, which propagates through `ClaudeBridge` → `Daemon.start()` and the daemon refuses to boot with a stack trace instead of a structured error. The phase-4 requirement was to fail fast; fine as-is. But the review prompt specifically asked about this — flag: no graceful fallback. If PyStemmer ever becomes optional, wrap the construction in a try and fall back to identity-stemming with a WARNING log.

### M10 | `src/assistant/tools_sdk/memory.py:239-250` | `memory_read` coerces `tags` to list but other keys unvalidated

`allowed_fm_keys = {"title", "tags", "area", "created", "updated"}`. For a corrupt on-disk note with e.g. `area: {nested: dict}`, the read tool passes the dict straight through the structured output. `json.dumps` tolerates dicts, but the downstream model would see a weird shape. Low-impact for single-user vault. Consider stringifying non-list values for consistency with tags handling.

## LOW (style / polish)

### L1 | `src/assistant/tools_sdk/memory.py:37` | `_LOG` defined but never used

`memory.py:36` assigns `_LOG = logging.getLogger(__name__)` but only uses `_LOG.warning` inside `configure_memory` (line 71). That one call site is correct; just flagging that no other module-level logging happens in `memory.py`. Fine.

### L2 | `src/assistant/tools_sdk/_memory_core.py:39` | `_LOG` is both module-local and passed into `reindex_vault(log=...)` as a parameter

`reindex_vault` takes `log: logging.Logger` as an explicit parameter but `reindex_under_lock` and `_maybe_auto_reindex` both pass `_LOG`. The parameter is vestigial — drop it and use the module `_LOG`, or make it default to `_LOG` for test-injection clarity. Minor.

### L3 | `src/assistant/tools_sdk/_memory_core.py:678-712` | `_parse_note_for_index` returns `(row_tuple, str | None)` tuple — discriminated-union style

Style choice; the `assert row is not None` after `if reason is not None:` is mildly awkward. A `Result[Row, str]` dataclass or just raising a custom `SkipNote(reason: str)` would read cleaner. Not worth changing.

### L4 | `src/assistant/tools_sdk/memory.py:419-450` | `memory_delete` args schema uses shorthand `{"path": str, "confirmed": bool}` while other handlers use full JSON Schema

`memory_read` uses `{"path": str}`, `memory_delete` uses `{"path": str, "confirmed": bool}`, but `memory_search`, `memory_write`, `memory_list`, `memory_reindex` use the full object-type form. The SDK accepts both; inconsistency is only cosmetic but visibly jarring across adjacent tools in the same file.

### L5 | `src/assistant/tools_sdk/memory.py:50-79` | `configure_memory` re-config path re-runs `fs_type_check` and `auto_reindex`? No — it returns early before them. Comment-only nit: the docstring says "H2.6: re-config with a NEW max_body_bytes is permitted" but doesn't state that `fs_type_check` / `auto_reindex` DON'T re-run on re-config. Worth one line.

### L6 | `src/assistant/tools_sdk/_memory_core.py:112-152` | schema SQL uses a raw triple-quoted string with ad-hoc CREATE ... IF NOT EXISTS

Fine for a single-table MCP server. If the schema ever grows past one version, consider the same migration pattern phase 2 established (`migrations/` dir). Forward-looking; no action needed for phase 4.

### L7 | `src/assistant/tools_sdk/memory.py:325` | `str(args.get("created") or now_iso)` will coerce truthy non-string inputs to their repr

If the model sends `created: 0` or `created: false`, the `or` short-circuit picks `now_iso`. If the model sends `created: {"iso": "..."}`, `str()` renders the dict-repr into the frontmatter. Given H3 above suggests reading `created` from disk instead of args, this becomes moot.

### L8 | `src/assistant/tools_sdk/_memory_core.py:298-312` | `_timestamp_as_iso` swallows ValueError/TypeError silently

The fallback to `str(node.value)` on `construct_yaml_timestamp` failure is intentional (H2.4), but there's no log line. If a seed note ships with a malformed date, the owner never learns about it until a downstream consumer notices the raw-string creeping through. A `_LOG.debug("frontmatter_timestamp_fallback", value=node.value)` would aid triage without noise.

## Commendations

- **Path-validation ordering is correct.** Symlink check before `.resolve()` (lines 230-236) avoids the common pitfall where `resolve()` follows the symlink and turns the escape-detection into a "path escapes vault" error that masks the real reason. The inline comment explains the reasoning — future maintainers won't "simplify" it.
- **Nonce-sentinel defence-in-depth is thorough.** Three layers (reject on write → scrub on read → unpredictable nonce) with a test for each; the ZWSP injection strategy is a clever minimal-perturbation solution that preserves legibility while breaking the tag. `wrap_untrusted`'s collision-retry plus 16-byte fallback is a clean pattern.
- **FTS5 query builder is well-designed.** `_build_fts_query` keeps the user-controlled text OUT of MATCH syntax entirely — Latin tokens phrase-quoted, Cyrillic stems prefix-wildcarded, punctuation dropped by the tokenizer. The H2.1 "≥3 char stem" gate prevents lone-character recall poisoning. All three are tested.
- **Transaction ordering in `write_note_tx` is correct.** `BEGIN IMMEDIATE` → INSERT → atomic_write → stat → commit keeps the filesystem authoritative and the index a best-effort mirror. Rollback on `atomic_write` failure is correct; commit-after-rename is the right tradeoff for a single-user vault.
- **`_fs_type_check` path-prefix warning is a real find.** Cloud-sync directories (iCloud, Dropbox) report `apfs` while silently ignoring `fcntl.flock`. Catching this as a WARNING on boot is exactly the kind of operational foresight that saves the owner from mystery data loss.
- **Test structure mirrors the module.** 16 test files (not 14 per prompt), each named `test_memory_{core,tool,mcp}_<topic>.py`. 94 asserts, unit-level. Seed-vault round-trip test exercises all 12 real notes, which is the right way to guard against Cyrillic / bare-date / list-valued frontmatter regressions.
- **`conftest.py` safety rail.** The `assert not tmp_path.startswith(...)` guard in `memory_ctx` is cheap insurance against a mis-typed fixture trashing the owner's real vault. Pattern worth copying for every fixture that writes to disk.
- **`extend-exclude` / `per-file-ignores` in `pyproject.toml`.** Targeted Cyrillic-ambiguous-char overrides for `tests/test_memory_*.py` + `_memory_core.py` keep ruff strict elsewhere while admitting the domain-required mixed scripts.
- **`_need_ctx` uses `tuple[Path, Path, int]` return** — gives every call site a typed unpack with no KeyError possibility at runtime.

## Metrics

### LOC by file

| File | LOC |
| --- | --- |
| `src/assistant/tools_sdk/_memory_core.py` | 1083 |
| `src/assistant/tools_sdk/memory.py` | 503 |
| `src/assistant/bridge/claude.py` | 304 (memory diff: +4) |
| `src/assistant/bridge/hooks.py` | 710 (memory diff: +44) |
| `src/assistant/main.py` | 271 (memory diff: +10) |
| `src/assistant/config.py` | 124 (MemorySettings: +17) |
| **Memory production total** | **~1660** |
| `tests/test_memory_*.py` (16 files) | 1451 |
| `tests/conftest.py` (memory fixtures) | ~50 of 82 |

### Test count + coverage estimate

- **Test functions across memory files:** 94 (count of `async def test_` + `def test_`; matches prompt's claimed 94).
- **Estimated line coverage:**
  - `memory.py` ≈ 90% — all 6 `@tool` handlers exercised on happy + at least one error branch; `_not_configured()` path not explicitly tested.
  - `_memory_core.py` ≈ 75% — every public helper has direct tests; under-tested paths: `delete_note_tx` OSError branch, `write_note_tx` stat-failure branch, `_detect_fs_type` Linux branch (Darwin-only tests), `_maybe_auto_reindex` MEMORY_ALLOW_LARGE_REINDEX opt-in.
  - Bridge integration: 0% of the actual `ClaudeBridge.ask` path is exercised with memory tools — known debt M6.

### Complexity hotspots

| Function | File:line | Why |
| --- | --- | --- |
| `write_note_tx` | `_memory_core.py:860` | Lock + BEGIN + INSERT + atomic_write + stat + mtime-update + COMMIT in one body (~55 lines). Candidate for extracting `_update_max_mtime(conn, mtime_ns)` helper. |
| `_build_fts_query` | `_memory_core.py:370` | Tight, ~25 lines. OK. |
| `_detect_fs_type` | `_memory_core.py:485` | Darwin mount-parse branch with nested best-prefix loop (~35 lines). OK but hardest to unit-test. |
| `_maybe_auto_reindex` | `_memory_core.py:800` | Two-signal trigger + cap + non-blocking lock + error recovery (~55 lines). Could split into `_needs_reindex(vault, idx) -> bool` + `_do_auto_reindex(...)` to flatten. |
| `memory_write` handler | `memory.py:291-369` | 79 lines — on the boundary of "too long", approaching the project's 30-40 LOC suspicious-threshold. All the argument validation (title, tags, area conflict, body sanitize, frontmatter build) lives in one function. Candidate to extract `_validate_write_args(args) -> (WriteArgs | tool_error)` dataclass. |

### Module-structure note on `_memory_core.py` size

1083 LOC is large but **coherent**, not wandering. The layout is dependency-ordered:

```
constants/regexes → SQL schema → tool_error → _ensure_index → validate_path →
atomic_write → IsoDateLoader/parse_frontmatter → serialize_frontmatter →
_build_fts_query → sanitize_body → wrap_untrusted → _detect_fs_type /
_fs_type_check → vault_lock → vault scan / reindex → transaction helpers →
search_notes / list_notes → extract_wikilinks
```

Each section is small (20-60 LOC), self-contained, and referenced only by later sections. Splitting into `_memory_fs.py` (validate_path, atomic_write, fs_type_check, vault_lock), `_memory_index.py` (schema, reindex, search_notes, list_notes, transactions), and `_memory_content.py` (parse_frontmatter, serialize_frontmatter, sanitize_body, wrap_untrusted, wikilinks, build_fts_query) would yield three ~350-LOC modules. **Recommendation: split in phase 5 or phase 6, not now** — the file is navigable, the split has a cost (test imports ripple), and the Phase 4 ship gate is already tight. Flag the size in the follow-up list.
