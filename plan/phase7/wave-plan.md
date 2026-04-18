# Wave Plan — Phase 7

**Plan version:** detailed-plan.md r2 + implementation.md v2

## Metadata

- Generator: parallel-split agent, 2026-04-18
- Max concurrent coders per wave (Q locked): 4
- Orchestrator model: isolation=worktree, merge=sequential rebase per wave
- Worktree parent dir: `/tmp/0xone-phase7/` (created on first wave; preserved until phase-7 merge complete)
- Total commits: 20 (1–19 + inserted 2b) distributed over 12 waves
- Waves: 8 sequential + 4 parallel; max parallelism = 4 (Wave 4 tools, Wave 10 test partitions)

**Pre-flight (before Wave 1):**

```bash
mkdir -p /tmp/0xone-phase7
cd /Users/agent2/Documents/0xone-assistant
git tag phase7-pre-start
```

**Per-wave sequence (orchestrator):**

1. Tag `phase7-pre-wave-N` on `main`.
2. For each commit in the wave: `git worktree add <worktree_path> -b <branch>`; spawn coder with the manifest prompt.
3. Await all coders; run the wave's per-commit test command in each worktree.
4. Sequential rebase-merge into main (each merge followed by `uv run pytest -q && just lint && uv run mypy src --strict`).
5. `git worktree remove <worktree_path>` per successful merge.
6. If any step red: follow-up coder in same worktree (max 3 retries) → else sequential fallback (§7 parallel-split-agent.md).

---

## Wave 1 — Spike 0 scripts + findings (sequential, 1 agent)

**Depends on:** nothing.
**Rationale for sequential:** Spike 0 establishes the multimodal envelope shape (Q0-1..Q0-6) that every downstream commit depends on semantically; artefacts must be produced and reviewed before any code references them.

### Commit 1 — Spike 0 findings + spike scripts

- **Branch:** `phase7-wave-1-commit-1-spike0`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-1-commit-1-spike0`
- **Files created:** `spikes/phase7_s0_multimodal_envelope.py`, `spikes/phase7_s0_findings.md` (if not already committed — verify in worktree first; the researcher fix-pack noted spike artefacts already exist. If present: this wave is a no-op and orchestrator skips to Wave 2 after a `git log --stat` verification.)
- **Files modified:** none.
- **Agent prompt:**
  > You are the Wave-1 coder for phase 7 (commit 1). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.1 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §2 (Spike 0 BLOCKER). Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 1. First run `git log --stat -- spikes/phase7_s0_multimodal_envelope.py spikes/phase7_s0_findings.md`. If both files already exist and are committed, STOP and report "spike-0 already present — wave 1 no-op". Otherwise create `spikes/phase7_s0_multimodal_envelope.py` (≈510 LOC probing Q0-1 through Q0-6 per detailed-plan.md §2.1) and `spikes/phase7_s0_findings.md` (PASS/FAIL per question + chosen `MEDIA_PHOTO_MODE` default). Do NOT modify production source. OAuth via `claude` CLI (no `ANTHROPIC_API_KEY`). Single commit with message "phase 7: Spike 0 SDK multimodal envelope probes + findings".
- **Test command:** `uv run python spikes/phase7_s0_multimodal_envelope.py` (manual sanity — spike is empirical, not pytest).
- **Merge gate:** both files present + findings Markdown structure valid (Verdict table + per-probe section) + `uv run ruff check spikes/` green.

---

## Wave 2 — `_memlib` → `_lib` full package refactor (sequential, 1 agent)

**Depends on:** Wave 1 merged.
**Rationale for sequential:** ~27-file refactor spanning `tools/memory/`, `tools/skill-installer/` rename, 8 test-file import rewrites, `tests/conftest.py` shim removal, `system_prompt.md` path updates. Non-atomic merge would break every subsequent test run. Single agent, single commit.

### Commit 2 — `_memlib` → `_lib` full package refactor (Q9a tech-debt close)

- **Branch:** `phase7-wave-2-commit-2-memlib`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-2-commit-2-memlib`
- **Files modified (per implementation.md §4.1 H-9):**
  - Rename: `tools/skill-installer/` → `tools/skill_installer/` (hyphen → underscore).
  - 8 test files — `from _memlib ...` → `from tools.memory._lib ...`:
    - `tests/test_memory_lock_probe.py`
    - `tests/test_memory_vault_dir_mode_0o700.py`
    - `tests/test_memory_atomic_write_fsync.py`
    - `tests/test_memory_frontmatter_roundtrip.py`
    - `tests/test_tmp_dir_chmods_loose_perms.py`
    - `tests/test_sanitize_body_fence_awareness.py`
    - `tests/test_memory_wikilinks_preserved.py`
    - `tests/test_memory_write_body_with_frontmatter_marker_sanitized.py`
  - `tests/conftest.py` — remove `_INSTALLER_DIR` / `_MEMORY_DIR` sys.path shims (lines 14-18, 26-28).
  - `tools/__init__.py` — create marker so `tools.memory._lib` importable.
  - `src/assistant/bridge/system_prompt.md` — any references to `tools/skill-installer/` → `tools/skill_installer/`.
  - Any other call-site referencing the hyphenated path.
- **Files created:** `tests/test_memlib_refactor_regression.py` (≈60 LOC — 8-case parametrized test: 4 tools × 2 invocation forms — cwd launch + `python -m tools.<name>.main`).
- **Agent prompt:**
  > You are the Wave-2 coder for phase 7 (commit 2, `_memlib` refactor). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §4.1 H-9 + pitfall #11 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §11. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 2. ATOMIC COMMIT — every file listed in implementation.md §4.1 H-9 must land in ONE commit, or abort and revert. Steps: (1) `git mv tools/skill-installer tools/skill_installer`; (2) rewrite 8 `from _memlib` imports in tests to `from tools.memory._lib`; (3) remove `_INSTALLER_DIR`/`_MEMORY_DIR` sys.path shims from `tests/conftest.py`; (4) create `tools/__init__.py`; (5) update any `system_prompt.md` / SKILL.md / Bash allowlist reference of `skill-installer` to `skill_installer`; (6) create `tests/test_memlib_refactor_regression.py` (8-case parametrized: 4 tools × 2 invocation forms). Run `uv run pytest tests/test_memlib_refactor_regression.py tests/test_memory_*.py tests/test_skill_installer_*.py tests/test_sanitize_body_fence_awareness.py tests/test_tmp_dir_chmods_loose_perms.py -x` until green. Then full `uv run pytest -q` must stay green. Single commit message "phase 7: _memlib → _lib package refactor + skill-installer rename (Q9a tech debt)".
- **Test command:** `uv run pytest tests/test_memlib_refactor_regression.py tests/test_memory_*.py tests/test_skill_installer_*.py tests/test_sanitize_body_fence_awareness.py tests/test_tmp_dir_chmods_loose_perms.py -x && uv run pytest -q && just lint && uv run mypy src --strict`.
- **Merge gate:** all tests green + ruff/mypy clean + `git grep -n '_memlib\|skill-installer'` returns zero hits in source (only in docs/plan is acceptable).

---

## Wave 2b — Root `pyproject.toml` dep addition (sequential, 1 agent)

**Depends on:** Wave 2 merged.
**Rationale for sequential:** every tool/media commit in later waves imports `pypdf`/`python-docx`/`openpyxl`/`striprtf`/`defusedxml`/`fpdf2`/`Pillow`/`lxml`. Running Wave 4 or Wave 5 before this lands produces `ModuleNotFoundError` on first test. Single commit, single agent.

### Commit 2b — Root `pyproject.toml` phase-7 deps (v2 fix-pack C-1)

- **Branch:** `phase7-wave-2b-commit-2b-pyproject`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-2b-commit-2b-pyproject`
- **Files modified:** `pyproject.toml` (root).
- **Agent prompt:**
  > You are the Wave-2b coder for phase 7 (commit 2b, root pyproject.toml). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §0 pitfall #1 (Pillow pin) + §1 (commit 2b) + §7 acceptance H-8 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §9. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 2b. Edit root `pyproject.toml` `[project]` dependencies array; add these deps with CVE-floor pins (exact bounds per H-8):
  > - `Pillow>=10.4,<13`
  > - `pypdf>=4.0`
  > - `python-docx>=1.0`
  > - `openpyxl>=3.1`
  > - `striprtf>=0.0.28`
  > - `defusedxml>=0.7`
  > - `fpdf2>=2.7,<3`
  > - (lxml arrives transitively via python-docx; verify no pin needed.)
  >
  > Run `uv sync` — wheel resolution must succeed (S-8 confirmed manylinux_2_28 availability for all 9 deps). Verify `uv run python -c "import PIL, pypdf, docx, openpyxl, striprtf, defusedxml, fpdf; print('ok')"` prints `ok`. Run `uv run pytest -q` — must remain green (no new tests, just dep availability). Single commit message "phase 7: root pyproject.toml — add media/tool deps (C-1, H-8)".
- **Test command:** `uv sync && uv run python -c "import PIL, pypdf, docx, openpyxl, striprtf, defusedxml, fpdf; print('ok')" && uv run pytest -q`.
- **Merge gate:** `uv sync` clean + import smoke green + full pytest green.

---

## Wave 3 — Config + adapter abstracts (parallel ×2)

**Depends on:** Wave 2b merged.
**Rationale for parallel:** commit 3 touches `src/assistant/config.py` only; commit 4 touches `src/assistant/adapters/base.py` only. Disjoint files, disjoint tests.

### Commit 3 — `MediaSettings` config (env_prefix `MEDIA_`)

- **Branch:** `phase7-wave-3-commit-3-mediasettings`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-3-commit-3-mediasettings`
- **Files modified:** `src/assistant/config.py` (+≈50 LOC, `MediaSettings` class + `Settings.media` field).
- **Files created:** `tests/test_media_settings.py` (≈40 LOC — env override round-trip, default values, `photo_mode` Literal validation).
- **Agent prompt:**
  > You are the Wave-3 coder for phase 7 (commit 3, MediaSettings). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.11 + §1 commit row 3 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §9. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 3. Add `MediaSettings` class to `src/assistant/config.py` per implementation.md §2.11 verbatim (env_prefix `MEDIA_`, photo/voice/audio/document/transcribe/genimage/extract/render/retention fields with exact defaults shown). Append `media: MediaSettings = Field(default_factory=MediaSettings)` to `Settings`. Write `tests/test_media_settings.py` verifying: (1) defaults match §2.11 table; (2) `MEDIA_PHOTO_MODE=path_tool` env parses; (3) unknown env var ignored (extra="ignore"). Single commit "phase 7: MediaSettings config (env_prefix MEDIA_)".
- **Test command:** `uv run pytest tests/test_media_settings.py -x && uv run mypy src/assistant/config.py --strict`.
- **Merge gate:** test green + mypy strict clean + no mutation of existing `Settings` fields.

### Commit 4 — `MediaAttachment` + `IncomingMessage.attachments` + adapter abstracts

- **Branch:** `phase7-wave-3-commit-4-attachment`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-3-commit-4-attachment`
- **Files modified:** `src/assistant/adapters/base.py` (add `MediaKind` Literal, `MediaAttachment` dataclass, `IncomingMessage.attachments` field, three `send_photo` / `send_document` / `send_audio` abstractmethods).
- **Files created:** `tests/test_media_attachment_dataclass.py` (≈50 LOC — immutability/slots/equality/Literal rejection).
- **Agent prompt:**
  > You are the Wave-3 coder for phase 7 (commit 4, MediaAttachment + adapter abstracts). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.1 + §1 commit row 4 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §5. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 3. Add to `src/assistant/adapters/base.py`: `MediaKind` Literal; `MediaAttachment` frozen slots dataclass (all fields per §2.1); `IncomingMessage.attachments: tuple[MediaAttachment, ...] | None = None` field (backward-compat default); three new abstractmethods `send_photo`/`send_document`/`send_audio` on `MessengerAdapter`. DO NOT implement them in TelegramAdapter here (that's Wave 7 commit 12). Write `tests/test_media_attachment_dataclass.py` asserting frozen, slots, equality, and rejection of invalid `MediaKind`. This commit will make TelegramAdapter abstract-incomplete — but tests that construct it via a mock (existing test helpers) must still pass. Verify: `uv run pytest tests/test_adapters_*.py -x` stays green (if existing mock adapter needs 3 stub methods, add them as `raise NotImplementedError`). Single commit "phase 7: MediaAttachment + IncomingMessage.attachments + adapter send_* abstracts".
- **Test command:** `uv run pytest tests/test_media_attachment_dataclass.py tests/test_adapters_*.py -x && uv run mypy src/assistant/adapters/base.py --strict`.
- **Merge gate:** both tests green + mypy clean + existing adapter-related tests unchanged (no regression).

**Wave 3 merge gate (all commits merged):** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 4 — Four CLI tools (parallel ×4)

**Depends on:** Wave 2b merged (Wave 3 not required — no shared files).
**Rationale for parallel:** each tool lives in its own directory (`tools/transcribe/`, `tools/genimage/`, `tools/extract_doc/`, `tools/render_doc/`). Completely disjoint file sets. Four agents, one per tool.
**Scheduling note:** orchestrator can run Wave 4 **in parallel with Wave 5** since Wave 4 touches `tools/*` and Wave 5 touches `src/assistant/media/` + `src/assistant/adapters/dispatch_reply.py` — also disjoint. If orchestrator chooses to fuse them into one combined 6-coder wave, the Q=4 cap forces a split anyway. Serialised here as separate waves for merge-order clarity.

### Commit 7 — `tools/transcribe/` + skill + thin-HTTP client

- **Branch:** `phase7-wave-4-commit-7-transcribe`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-4-commit-7-transcribe`
- **Files created:** `tools/transcribe/__init__.py`, `tools/transcribe/main.py` (~180 LOC stdlib-only HTTP), `tools/transcribe/_net_mirror.py` (mirror `is_loopback_only` helper), `tools/transcribe/SKILL.md`, `tests/test_tools_transcribe_cli.py` (~120 LOC).
- **Agent prompt:**
  > You are the Wave-4 coder for phase 7 (commit 7, tools/transcribe). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.7 + §0 pitfall #5 + §1 commit row 7 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §3.1 + §5.3. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 4. Build stdlib-only HTTP client CLI per §2.7. MUST use `is_loopback_only(url)` helper (mirror to `tools/transcribe/_net_mirror.py`) — NOT `classify_url` (S-1 finding). Exit codes: 0 OK, 2 argv, 3 path, 4 network, 5 unknown. SKILL.md MUST include "always put a space after `:` before an outbox path" rule per H-13. Tests: 11-case `is_loopback_only` port from S-1, argv validation, multipart encoding, timeout/network error paths. Single commit "phase 7: tools/transcribe/ HTTP client + SKILL (loopback-only endpoint)".
- **Test command:** `uv run pytest tests/test_tools_transcribe_cli.py -x`.
- **Merge gate:** test green + `python tools/transcribe/main.py --help` exits 0 + SKILL.md lints.

### Commit 8 — `tools/genimage/` + skill + flock quota

- **Branch:** `phase7-wave-4-commit-8-genimage`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-4-commit-8-genimage`
- **Files created:** `tools/genimage/__init__.py`, `tools/genimage/main.py`, `tools/genimage/_net_mirror.py`, `tools/genimage/SKILL.md`, `tests/test_tools_genimage_cli.py` (~120 LOC incl. S-5 R-3 flock contention port).
- **Agent prompt:**
  > You are the Wave-4 coder for phase 7 (commit 8, tools/genimage). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.8 + §0 pitfall #7 + §1 commit row 8 + `/Users/agent2/Documents/0xone-assistant/plan/phase7/detailed-plan.md` §3.2. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 4. Build CLI per §2.8 with `fcntl.flock(LOCK_EX)` quota enforcement; quota file schema `{"date":"YYYY-MM-DD","count":N}`. Exit codes: 0 OK, 2 argv, 3 path, 4 network, 5 unknown, **6 quota**. Tests: port 4 S-5 scenarios (including R-3 10-worker contention) to `tests/test_tools_genimage_cli.py`. SKILL.md MUST include the "space after `:` before path" rule (H-13). Single commit "phase 7: tools/genimage/ HTTP client + SKILL + flock daily quota".
- **Test command:** `uv run pytest tests/test_tools_genimage_cli.py -x`.
- **Merge gate:** test green + flock contention test passes + `--help` exits 0.

### Commit 9 — `tools/extract_doc/` + skill + local extractor

- **Branch:** `phase7-wave-4-commit-9-extract-doc`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-4-commit-9-extract-doc`
- **Files created:** `tools/extract_doc/__init__.py`, `tools/extract_doc/main.py`, `tools/extract_doc/SKILL.md`, `tests/test_tools_extract_doc_cli.py` (~80 LOC).
- **Agent prompt:**
  > You are the Wave-4 coder for phase 7 (commit 9, tools/extract_doc). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.9 + §0 pitfall #11 + §1 commit row 9. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 4. Build local extractor CLI using `pypdf>=4.0`, `python-docx>=1.0`, `openpyxl>=3.1`, `striprtf>=0.0.28`, `defusedxml>=0.7` (zip-bomb + entity-expansion guard). Dispatch by suffix. Tests for each file type (PDF/DOCX/XLSX/RTF/TXT) in `tests/test_tools_extract_doc_cli.py`. SKILL.md MUST contain H-13 "space after `:`" rule. IMPORTANT: directory name `tools/extract_doc/` (underscore), not `extract-doc` (pitfall #11). Single commit "phase 7: tools/extract_doc/ local extractor + SKILL".
- **Test command:** `uv run pytest tests/test_tools_extract_doc_cli.py -x`.
- **Merge gate:** test green + all 5 file-type codepaths exercised.

### Commit 10 — `tools/render_doc/` + skill + fpdf2/docx render

- **Branch:** `phase7-wave-4-commit-10-render-doc`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-4-commit-10-render-doc`
- **Files created:** `tools/render_doc/__init__.py`, `tools/render_doc/main.py`, `tools/render_doc/_lib/DejaVuSans.ttf` (vendored font), `tools/render_doc/SKILL.md`, `tests/test_tools_render_doc_cli.py` (~80 LOC).
- **Agent prompt:**
  > You are the Wave-4 coder for phase 7 (commit 10, tools/render_doc). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.10 + §0 pitfall #1 (Pillow required) + §1 commit row 10. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 4. Build fpdf2 + python-docx renderer. sys.path pragma to project root. Path guards: `--body-file` under `<data_dir>/run/render-stage/`, `--out` under `<data_dir>/media/outbox/`. Vendor DejaVuSans.ttf under `tools/render_doc/_lib/` (Cyrillic support — S-3 verified). Tests: render PDF + DOCX with Cyrillic body; path-guard rejection cases. SKILL.md MUST contain H-13 "space after `:`" rule. IMPORTANT: directory name `tools/render_doc/` (underscore — pitfall #11). Single commit "phase 7: tools/render_doc/ fpdf2+docx renderer + SKILL + vendored DejaVu".
- **Test command:** `uv run pytest tests/test_tools_render_doc_cli.py -x`.
- **Merge gate:** test green + Cyrillic PDF render succeeds + path-guard tests pass.

**Wave 4 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict` after all 4 commits merged.

---

## Wave 5 — media/ sub-package + dispatch_reply (parallel ×2)

**Depends on:** Wave 3 merged (needs `MediaSettings` + adapter abstracts).
**Rationale for parallel:** commit 5 creates `src/assistant/media/` (new package, new files); commit 6 creates `src/assistant/adapters/dispatch_reply.py` (new file). Disjoint file sets. Both depend on commit 3+4 + Wave 2b deps.

### Commit 5 — `src/assistant/media/` sub-package (paths/download/sweeper/artefacts)

- **Branch:** `phase7-wave-5-commit-5-media-pkg`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-5-commit-5-media-pkg`
- **Files created:** `src/assistant/media/__init__.py`, `paths.py`, `download.py`, `sweeper.py`, `artefacts.py`, plus tests `tests/test_media_paths.py` (~40 LOC), `tests/test_media_download.py` (~100 LOC incl. S-6 A/B/C/D cases), `tests/test_media_sweeper.py` (~120 LOC).
- **Agent prompt:**
  > You are the Wave-5 coder for phase 7 (commit 5, media/ sub-package). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.2–§2.5 + §0 pitfalls #2/#3/#14 + §1 commit row 5 + §7 acceptance items. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 5. Build four modules per §2.2–§2.5 verbatim. CRITICAL: `_SizeCappedWriter` MUST implement BOTH `write(data: bytes) -> int` AND `flush() -> None` (C-3). `ARTEFACT_RE` MUST use v3 pattern from §2.5 (S-2 corpus 43/46 — v1 regex is WRONG per pitfall #2). Tests: port S-6 A/B/C/D cases to `test_media_download.py` (pre-flight cap, streaming cap via SizeCapExceeded, None-sized None-cap, partial-cleanup via unlink); `test_media_sweeper.py` covers age + LRU eviction. Single commit "phase 7: src/assistant/media/ — paths/download/sweeper/artefacts (v3 regex, SizeCappedWriter)".
- **Test command:** `uv run pytest tests/test_media_paths.py tests/test_media_download.py tests/test_media_sweeper.py -x && uv run mypy src/assistant/media --strict`.
- **Merge gate:** all three tests green + mypy clean + SizeCapExceeded propagation test green.

### Commit 6 — `adapters/dispatch_reply.py` + `_DedupLedger`

- **Branch:** `phase7-wave-5-commit-6-dispatch-reply`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-5-commit-6-dispatch-reply`
- **Files created:** `src/assistant/adapters/dispatch_reply.py` (~250 LOC: `_DedupLedger` + `dispatch_reply`), `tests/test_dispatch_reply_regex.py` (130 LOC — 46-case S-2 corpus port), `test_dispatch_reply_classify.py` (80), `test_dispatch_reply_path_guard.py` (100), `test_dispatch_reply_integration.py` (140), `test_dispatch_reply_dedup_ledger.py` (110, H-12 mock-clock + real-clock xfail variants).
- **Agent prompt:**
  > You are the Wave-5 coder for phase 7 (commit 6, dispatch_reply + dedup ledger). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.6 + §0 pitfalls #9, #10 + §1 commit row 6 + §4.1 H-12 (dedup ledger test split) + §7 acceptance. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 5. Build `_DedupLedger` per §2.6 (OrderedDict + TTL=300s + max_entries=256, `mark_and_check(key, now: float)` accepting INJECTED clock — NOT `time.monotonic()` inside the method so tests can mock). `dispatch_reply`: extract artefacts via `artefacts.ARTEFACT_RE` (assume `artefacts` module already merged via commit 5 dependency), path-guard via `resolve().is_relative_to(outbox_root) AND exists()` (pitfall #10), classify, send via adapter send_photo/document/audio with per-artefact try/except Exception + log.warning("artefact_send_failed"), THEN send cleaned text via send_text. Dedup ledger test MUST be split into `test_dedup_ttl_mock_clock` (authoritative, injects `now` float) and `test_dedup_ttl_real_clock` (real `time.monotonic()`, `@pytest.mark.xfail(strict=False)` per H-12). Port full 46-case S-2 corpus to `test_dispatch_reply_regex.py` (3 known failures marked xfail). Single commit "phase 7: adapters/dispatch_reply.py + _DedupLedger (I-7.5)".
- **Test command:** `uv run pytest tests/test_dispatch_reply_*.py -x && uv run mypy src/assistant/adapters/dispatch_reply.py --strict`.
- **Merge gate:** 43/46 regex corpus green + 3 xfail marked + mock-clock test strict-pass + mypy clean.

**Wave 5 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict` after both commits merged.

---

## Wave 6 — Bash allowlist extension (sequential, 1 agent)

**Depends on:** Wave 4 merged (needs all 4 tool scripts present on disk) + Wave 3 merged (`MediaSettings.data_dir` references).
**Rationale for sequential:** single file `src/assistant/bridge/hooks.py` — 4 new validators (`_validate_transcribe_argv`, `_genimage`, `_extract_doc`, `_render_doc`) + factory plumbing for optional `data_dir` kwarg (backward-compat). Cannot parallelise a single file's edits.

### Commit 11 — Bash allowlist + hook factory plumbing (`data_dir` optional)

- **Branch:** `phase7-wave-6-commit-11-bash-allowlist`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-6-commit-11-bash-allowlist`
- **Files modified:** `src/assistant/bridge/hooks.py` (+≈200 LOC across 4 validators + 3 factory signatures).
- **Files created:** `tests/test_bash_hook_transcribe_allowlist.py` (30), `_genimage_` (30), `_extract_doc_` (30), `_render_doc_` (50), `test_bash_hook_factory_backward_compat.py` (40).
- **Agent prompt:**
  > You are the Wave-6 coder for phase 7 (commit 11, Bash allowlist + hook plumbing). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §2.12 + §1 commit row 11 + §0 pitfall #11 (underscore directory names — allowlist keyed on `tools/transcribe/main.py`, `tools/genimage/main.py`, `tools/extract_doc/main.py`, `tools/render_doc/main.py`). Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 6. Extend `src/assistant/bridge/hooks.py`: add `_validate_transcribe_argv` / `_genimage` / `_extract_doc` / `_render_doc` validators reusing existing phase-6 path/argv primitives. Update `make_pretool_hooks` / `make_bash_hook` / `make_file_hook` to accept `data_dir: Path | None = None` (KEYWORD DEFAULT — 9 existing test call-sites must stay green). When `data_dir is None` and argv targets `tools/render_doc/main.py`: explicit deny "render-doc requires data_dir-bound hooks". Write 4 per-tool allowlist tests + `test_bash_hook_factory_backward_compat.py` verifying `make_pretool_hooks(project_root)` (no data_dir) still works + render-doc argv → deny. Single commit "phase 7: Bash allowlist — transcribe/genimage/extract_doc/render_doc + data_dir optional factory".
- **Test command:** `uv run pytest tests/test_bash_hook_*.py -x && uv run mypy src/assistant/bridge/hooks.py --strict`.
- **Merge gate:** 5 new tests green + 9 existing hook-test files still green + mypy clean + `git grep "skill-installer\|_memlib"` in bash allowlist returns zero hits.

---

## Wave 7 — TelegramAdapter (Wave 6A) + handler/bridge envelope (Wave 6B) (parallel ×2)

**Depends on:** Wave 5 merged (both need dispatch_reply / MediaAttachment present) + Wave 6 merged (commit 13 references the new hook factory signature indirectly through Daemon, but no hooks.py modification in wave 7).
**Rationale for parallel (v2 fix-pack C-5):** commit 12 touches ONLY `src/assistant/adapters/telegram.py`. Commit 13 touches `src/assistant/handlers/message.py`, `src/assistant/bridge/claude.py`, `src/assistant/bridge/history.py`. Sets are disjoint. Both depend on commit 4 (already merged).

### Commit 12 — Wave 6A: TelegramAdapter media handlers + send_photo/document/audio + attachment-dedup (I-7.6)

- **Branch:** `phase7-wave-7-commit-12-telegram-media`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-7-commit-12-telegram-media`
- **Files modified:** `src/assistant/adapters/telegram.py` (+≈200 LOC).
- **Files created:** `tests/test_telegram_adapter_media_handlers.py` (~160 LOC — includes v2 cases L-20 send-FileNotFoundError, L-21 RetryAfter, C-6/I-7.6 attachment dedup, send-network-error retry×2).
- **Agent prompt:**
  > You are the Wave-7A coder for phase 7 (commit 12, TelegramAdapter media). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §3.4 + §0 pitfalls #3, #13, #17 + §1 commit row 12 + §4.1 test matrix additions. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 7A. Register 5 new handlers (`_on_voice`, `_on_audio`, `_on_photo`, `_on_document`, `_on_video_note`). Each: adapter-level size pre-check → `media.download.download_telegram_file(...)` → build `MediaAttachment(...)` → attachment dedup via `_emitted_attachments: OrderedDict[tuple[int, str], float]` (60s TTL, 128 LRU cap, per I-7.6). Implement `send_photo` / `send_document` / `send_audio` with `TelegramRetryAfter` retry + `TelegramNetworkError` exponential backoff + `FileNotFoundError/PermissionError/OSError` → log.warning("send_*_read_failed") + re-raise. Test cases MUST cover: retry-after honoured (L-21), FNF re-raised (L-20), network-error retries twice then raises, attachment-ingress dedup (same `local_path` fed twice within 60s → emit called once). DO NOT modify handlers/ or bridge/ — commit 13 owns those. Single commit "phase 7: TelegramAdapter media handlers + send_photo/document/audio + attachment-ingress dedup (I-7.6)".
- **Test command:** `uv run pytest tests/test_telegram_adapter_media_handlers.py -x && uv run mypy src/assistant/adapters/telegram.py --strict`.
- **Merge gate:** all new test cases green (incl. L-20, L-21, C-6 dedup) + mypy clean + `TelegramAdapter` no longer abstract-incomplete.

### Commit 13 — Wave 6B: Handler + bridge multimodal envelope (path_tool branch C-4, turn-id H-10)

- **Branch:** `phase7-wave-7-commit-13-handler-envelope`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-7-commit-13-handler-envelope`
- **Files modified:** `src/assistant/handlers/message.py`, `src/assistant/bridge/claude.py`, `src/assistant/bridge/history.py` (+≈105 LOC total).
- **Files created:** `tests/test_handler_multimodal_envelope.py` (80), `test_handler_photo_path_tool_fallback.py` (60, C-4), `test_handler_multimodal_all_photos_fail.py` (80, H-14), `test_handler_multimodal_real_photo.py` (90, C-2 — RUN_SDK_INT-gated; fixture `tests/fixtures/phase7/real_photo_3mb.jpg` required; optional `фото_3mb.jpg` bonus), `test_history_replay_photo_turn_ordering.py` (70, H-10).
- **Agent prompt:**
  > You are the Wave-7B coder for phase 7 (commit 13, handler + bridge envelope). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §3.1 (handler, INCLUDING explicit `elif att.kind == "photo" and photo_mode == "path_tool":` branch per C-4) + §3.2 (claude.ask envelope builder) + §3.3 (history placeholder with turn_id per H-10) + §0 pitfalls #4, #16 + §1 commit row 13 + §4.1 test matrix additions. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 7B. Handler: attachment loop must handle (a) `photo+inline_base64` (base64 encode + image_block + note; FNF/PermissionError/OSError → failure-note, log.warning, continue); (b) `photo+path_tool` EXPLICIT elif — note-only, no silent drop (C-4); (c) voice/audio/document/video_note notes. Do NOT deduplicate locally — adapter-level dedup (I-7.6) is invariant. Bridge `ClaudeBridge.ask` gains `image_blocks` kwarg; prompt_stream builds mixed content list (text → images → system-notes per S-0 Q0-5 order). History placeholder MUST embed `turn_id` per H-10. Create `tests/fixtures/phase7/real_photo_3mb.jpg` (real entropy JPEG ≥3 MB — can be fetched from a stock/CC0 source or generated via Pillow with real photo content; null-padded fixtures FORBIDDEN per C-2). The real-photo test gated by `RUN_SDK_INT=1`. DO NOT modify `adapters/telegram.py` — commit 12 owns it. Single commit "phase 7: handler+bridge multimodal envelope + path_tool branch (C-4) + turn-id placeholder (H-10)".
- **Test command:** `uv run pytest tests/test_handler_multimodal_*.py tests/test_handler_photo_path_tool_fallback.py tests/test_history_replay_photo_turn_ordering.py -x && RUN_SDK_INT=1 uv run pytest tests/test_handler_multimodal_real_photo.py -x && uv run mypy src/assistant/handlers src/assistant/bridge --strict`.
- **Merge gate:** all 5 new tests green + RUN_SDK_INT test green (or skipped with clear reason) + mypy clean.

**Wave 7 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict` after both commits merged.

---

## Wave 8 — SchedulerDispatcher + SubagentStop hook switches (parallel ×2)

**Depends on:** Wave 5 merged (dispatch_reply available) + Wave 3 merged (_DedupLedger referenced via __init__ kwarg).
**Rationale for parallel:** commit 14 modifies ONLY `src/assistant/scheduler/dispatcher.py`. Commit 15 modifies ONLY `src/assistant/subagent/hooks.py`. Disjoint files. Both are one-line swap `send_text → dispatch_reply` + kwarg plumbing.

### Commit 14 — `SchedulerDispatcher._deliver` → `dispatch_reply`

- **Branch:** `phase7-wave-8-commit-14-scheduler-dispatch`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-8-commit-14-scheduler-dispatch`
- **Files modified:** `src/assistant/scheduler/dispatcher.py` (`__init__` gains `dedup_ledger: _DedupLedger` param; `_deliver` call-site switches to `dispatch_reply(...)`).
- **Files created:** `tests/test_scheduler_dispatch_reply_integration.py` (~40 LOC).
- **Agent prompt:**
  > You are the Wave-8 coder for phase 7 (commit 14, scheduler switch). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §3.5 + §1 commit row 14. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 8. `SchedulerDispatcher.__init__` gains `dedup_ledger: _DedupLedger` kwarg (required — tests + Daemon wiring both pass it; keep it positional-friendly). `_deliver` call-site at line 216 replaces `await self._adapter.send_text(self._owner, joined)` with `await dispatch_reply(self._adapter, self._owner, joined, outbox_root=outbox_dir(self._settings.data_dir), dedup=self._dedup_ledger, log_ctx={"trigger_id": t.trigger_id, "schedule_id": t.schedule_id})`. Test: schedule fires with outbox-path-bearing text → exactly one send_photo call + send_text for cleaned tail. Single commit "phase 7: SchedulerDispatcher._deliver → dispatch_reply".
- **Test command:** `uv run pytest tests/test_scheduler_dispatch_reply_integration.py tests/test_scheduler_*.py -x && uv run mypy src/assistant/scheduler --strict`.
- **Merge gate:** new test green + existing scheduler tests green (updated to pass `dedup_ledger` fixture) + mypy clean.

### Commit 15 — `subagent/hooks.py::on_subagent_stop` → `dispatch_reply` (factory drops `outbox_root` — H-11)

- **Branch:** `phase7-wave-8-commit-15-subagent-hook-switch`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-8-commit-15-subagent-hook-switch`
- **Files modified:** `src/assistant/subagent/hooks.py` (`make_subagent_hooks` gains ONLY `dedup_ledger: _DedupLedger` param; `outbox_root` DERIVED inside hook closure via `outbox_dir(settings.data_dir)` — NOT threaded through factory signature).
- **Files created:** `tests/test_subagent_hooks_dispatch_reply.py` (~40 LOC).
- **Agent prompt:**
  > You are the Wave-8 coder for phase 7 (commit 15, subagent hook switch). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §3.6 + §1 commit row 15 + §0 commentary about H-11 **verbatim**. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 8. CRITICAL (v2 fix-pack H-11): `make_subagent_hooks` signature is `make_subagent_hooks(settings, store, picker, adapter, dedup_ledger)` — it does **NOT** take `outbox_root`. Derive `outbox_root = outbox_dir(settings.data_dir)` INSIDE the hook closure. If you thread `outbox_root` through the factory it will drift from `settings.data_dir` and regress. Replace `await asyncio.shield(adapter.send_text(callback_chat_id, body))` with `await asyncio.shield(dispatch_reply(adapter, callback_chat_id, body, outbox_root=outbox_root, dedup=dedup_ledger, log_ctx={"job_id": job_id}))`. Preserve phase-6 shielding semantics. Test: subagent Stop hook produces outbox path → dispatch_reply called with derived outbox_root. Single commit "phase 7: subagent/hooks.py::on_subagent_stop → dispatch_reply (derived outbox_root, H-11)".
- **Test command:** `uv run pytest tests/test_subagent_hooks_dispatch_reply.py tests/test_subagent_hooks_*.py -x && uv run mypy src/assistant/subagent/hooks.py --strict`.
- **Merge gate:** new test green + existing subagent-hook tests green (updated to pass `dedup_ledger`) + mypy clean + signature inspection shows NO `outbox_root` param.

**Wave 8 merge gate:** `uv run pytest -q && just lint && uv run mypy src --strict`.

---

## Wave 9 — Daemon integration (sequential, 1 agent)

**Depends on:** Waves 5, 8 merged (needs media sub-package + dispatch_reply + both switched call-sites).
**Rationale for sequential:** single file `src/assistant/main.py::Daemon` — weaves `ensure_media_dirs`, `media_sweeper_loop` bg task, `_DedupLedger` instance, and passes `dedup_ledger` into both `SchedulerDispatcher` and `make_subagent_hooks`. Cannot split.

### Commit 16 — `Daemon.start` integration

- **Branch:** `phase7-wave-9-commit-16-daemon-integration`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-9-commit-16-daemon-integration`
- **Files modified:** `src/assistant/main.py` (Daemon.__init__ + start + stop).
- **Files created:** `tests/test_daemon_media_integration.py` (~60 LOC).
- **Agent prompt:**
  > You are the Wave-9 coder for phase 7 (commit 16, Daemon integration). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §3.7 + §0 pitfall #14 (sweeper ordering) + §1 commit row 16. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 9. `Daemon.__init__`: construct `self._dedup_ledger = _DedupLedger()`. `Daemon.start`: CALL `await ensure_media_dirs(self._settings.data_dir)` **before** any bg task is spawned (pitfall #14); pass `self._dedup_ledger` into `SchedulerDispatcher(...)` and `make_subagent_hooks(...)`; spawn `media_sweeper_loop` bg task with `self._media_sweep_stop = asyncio.Event()`. `Daemon.stop`: `self._media_sweep_stop.set()` in the phase-5/6 drain order. Test: Daemon lifecycle init → start → stop; assert media dirs created, sweeper spawned+drained, dedup_ledger shared between Scheduler + subagent hooks. Single commit "phase 7: Daemon.start integration — ensure_media_dirs + media_sweeper_loop + _DedupLedger plumbing".
- **Test command:** `uv run pytest tests/test_daemon_media_integration.py tests/test_daemon_*.py -x && uv run mypy src/assistant/main.py --strict`.
- **Merge gate:** new + existing daemon tests green + no regression of phase-5/6 drain tests + mypy clean.

---

## Wave 10 — Integration E2E tests (sequential, 1 agent)

**Depends on:** Wave 9 merged (Daemon fully integrated).
**Rationale for sequential:** the E2E commit is fixture-heavy cross-cutting test code; a single coder keeps fixture wiring coherent.

### Commit 17 — Integration E2E tests

- **Branch:** `phase7-wave-10-commit-17-e2e`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-10-commit-17-e2e`
- **Files created:** `tests/test_phase7_e2e_voice_transcribe.py`, `test_phase7_e2e_photo_inline.py`, `test_phase7_e2e_document_extract.py`, `test_phase7_e2e_scheduler_media.py`, `test_phase7_e2e_double_delivery_dedup.py`, `test_task_spawn_media_worker.py` (~100 LOC total).
- **Agent prompt:**
  > You are the Wave-10 coder for phase 7 (commit 17, E2E tests). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §4.3 + description.md E2E scenarios + §7 acceptance checklist. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 10. Write 5 E2E flow tests covering: (1) voice → transcribe CLI → reply; (2) photo → inline_base64 → model describes; (3) document → extract_doc CLI → summary; (4) scheduler trigger → subagent returns outbox path → send_photo; (5) double-delivery race — main turn mentions path + SubagentStop hook both fire → ledger dedupes to exactly ONE send. Plus `test_task_spawn_media_worker.py` regression. All SDK-calling tests gated by `RUN_SDK_INT=1`. Reuse existing phase-6 fixtures where possible. Single commit "phase 7: integration E2E tests (voice/photo/document/scheduler/dedup-race)".
- **Test command:** `uv run pytest tests/test_phase7_e2e_*.py tests/test_task_spawn_media_worker.py -x && RUN_SDK_INT=1 uv run pytest tests/test_phase7_e2e_*.py -x`.
- **Merge gate:** all tests green (SDK-gated ones skip cleanly without flag) + no flakes.

---

## Wave 11 — Unit tests top-up (parallel partitions, ×4 per partition, up to 3 partitions)

**Depends on:** Wave 10 merged (all code + integration present).
**Rationale for parallel:** per-file test partitioning. Each partition targets 4 disjoint NEW test files or EXPANDS existing ones; max 4 coders concurrent (Q locked). Orchestrator iterates partitions sequentially. Files listed below are cross-cutting/top-up coverage NOT already shipped alongside their source commit.

**Scope:** commit 18 (per detailed-plan §19.2 row 18) is the "20 test files" umbrella. In practice, most of the per-commit tests are already written in Waves 1–10 (as mandated by §4 test-first rule). This wave delivers the **remaining cross-cutting tests** and **expansion of the existing corpus** flagged in implementation.md §7 acceptance list (SKILL.md H-13 assertion, `is_loopback_only` 11-case integration port, additional dedup scenarios, full regex corpus cleanup, etc.).

### Partition 11.A (parallel ×4)

| # | Test file / expansion | Branch | Worktree |
|---|---|---|---|
| 18a | `tests/test_skills_colon_space_rule.py` (H-13 SKILL.md + system_prompt assertion across all 4 phase-7 SKILL.md files + `bridge/system_prompt.md`) | `phase7-wave-11-commit-18a-skills-colon` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18a-skills-colon` |
| 18b | `tests/test_is_loopback_only_integration.py` (11-case S-1 corpus applied to both `tools/transcribe/_net_mirror.py` and `tools/genimage/_net_mirror.py`) | `phase7-wave-11-commit-18b-loopback-integration` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18b-loopback-integration` |
| 18c | `tests/test_media_sweeper_concurrency.py` (concurrent sweep + ongoing write — sweeper must not unlink a file being written) | `phase7-wave-11-commit-18c-sweeper-concurrency` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18c-sweeper-concurrency` |
| 18d | `tests/test_media_download_cyrillic_filename.py` (optional C-2 bonus — UTF-8 path through full adapter/handler/CLI chain) | `phase7-wave-11-commit-18d-cyrillic-filename` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18d-cyrillic-filename` |

### Partition 11.B (parallel ×4)

| # | Test file / expansion | Branch | Worktree |
|---|---|---|---|
| 18e | `tests/test_dispatch_reply_partial_send_fail.py` (artefact send fails mid-list → remaining artefacts + cleaned text still delivered) | `phase7-wave-11-commit-18e-partial-send` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18e-partial-send` |
| 18f | `tests/test_scheduler_dispatch_reply_race.py` (scheduler + main turn both mention same path within 300s window → exactly one send) | `phase7-wave-11-commit-18f-sched-race` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18f-sched-race` |
| 18g | `tests/test_history_replay_multi_image_turn.py` (2 images in same turn + 1 text — placeholders emit with correct `turn_id` each) | `phase7-wave-11-commit-18g-history-multi-image` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18g-history-multi-image` |
| 18h | `tests/test_genimage_quota_midnight_rollover.py` (S-5 full 4-scenario port) | `phase7-wave-11-commit-18h-quota-rollover` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18h-quota-rollover` |

### Partition 11.C (parallel ×4)

| # | Test file / expansion | Branch | Worktree |
|---|---|---|---|
| 18i | `tests/test_telegram_adapter_oversize_reject.py` (pre-flight `file_size > cap` → reject before download; None-size path covered) | `phase7-wave-11-commit-18i-oversize-reject` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18i-oversize-reject` |
| 18j | `tests/test_extract_doc_defusedxml_zip_bomb.py` (zip-bomb + entity-expansion rejection cases for XLSX/DOCX) | `phase7-wave-11-commit-18j-defusedxml` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18j-defusedxml` |
| 18k | `tests/test_render_doc_path_guard.py` (explicit rejection matrix for out-of-stage body-file + out-of-outbox --out) | `phase7-wave-11-commit-18k-render-pathguard` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18k-render-pathguard` |
| 18l | `tests/test_daemon_media_sweeper_stop_ordering.py` (pitfall #14 regression — ensure_media_dirs MUST run before sweeper spawn) | `phase7-wave-11-commit-18l-sweeper-ordering` | `/tmp/0xone-phase7/wt_phase7-wave-11-commit-18l-sweeper-ordering` |

**Per-partition agent prompt template (one per sub-commit):**
> You are a Wave-11 coder for phase 7 (commit 18{x}, unit-test top-up). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §4 + §7 acceptance checklist + the specific acceptance item this test enforces (cross-referenced in the branch name). Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 11 partition table. Create ONLY the single test file listed for this sub-commit. Do NOT modify production code — if the test fails because of a bug, report it; do not fix production here. Single commit "phase 7: test — <test file short description>".

**Per-partition test command:** `uv run pytest tests/test_<new_file>.py -x` per worktree.
**Per-partition merge gate (after all 4 commits in a partition merged):** `uv run pytest -q && just lint && uv run mypy src --strict`.
**Partition ordering:** 11.A → 11.B → 11.C sequentially. Within a partition, all 4 run in parallel.

---

## Wave 12 — Documentation update (sequential, 1 agent)

**Depends on:** Wave 11 merged.
**Rationale for sequential:** doc edits across `description.md` §82 wording fix + 4 phase-7 SKILL.md files + `system_prompt.md` + `summary.md`. Single coder, single commit.

### Commit 19 — Documentation update

- **Branch:** `phase7-wave-12-commit-19-docs`
- **Worktree:** `/tmp/0xone-phase7/wt_phase7-wave-12-commit-19-docs`
- **Files modified:** `plan/phase7/description.md` (§82 Pillow wording fix), `tools/transcribe/SKILL.md`, `tools/genimage/SKILL.md`, `tools/extract_doc/SKILL.md`, `tools/render_doc/SKILL.md` (§4.5 dedup + H-13 `:` space rule), `src/assistant/bridge/system_prompt.md` (H-13 rule mirror).
- **Files created:** `plan/phase7/summary.md` (new — phase-7 wrap-up, invariants for phase-8).
- **Agent prompt:**
  > You are the Wave-12 coder for phase 7 (commit 19, documentation). Read `/Users/agent2/Documents/0xone-assistant/plan/phase7/implementation.md` §0 pitfalls #9, #18 + §7 acceptance. Context: `/Users/agent2/Documents/0xone-assistant/plan/phase7/wave-plan.md` Wave 12. (1) Fix `description.md` §82 — remove "no Pillow" claim, replace with "Pillow is a required transitive dep via fpdf2". (2) Add §4.5 dedup-guidance paragraph + H-13 "always space after `:` before outbox path" with good/bad example to all 4 phase-7 SKILL.md files. (3) Mirror H-13 rule into `bridge/system_prompt.md`. (4) Write `plan/phase7/summary.md` documenting phase-7 deltas + invariants to preserve for phase-8 (dedup ledger TTL=300s stays in-memory; media retention 14d/7d/2GB LRU; `MediaSettings.photo_mode` default inline_base64; factory signatures for `make_subagent_hooks` / Bash hook `data_dir` kwarg). Single commit "phase 7: documentation — description §82 fix + SKILL.md H-13 + phase-7 summary".
- **Test command:** `just lint && uv run pytest -q` (docs don't break tests).
- **Merge gate:** lint clean + all phase-7 acceptance checklist items in implementation.md §7 marked complete in summary.md.

---

## Merge Coordinator Checklist

After each wave's merges:

1. Ensure worktree is clean: `cd <worktree> && git status` — zero modified files.
2. Rebase onto current main: `git fetch origin && git rebase origin/main`.
3. Run the wave's test command in the worktree; must be green.
4. Merge: `cd <main repo>; git merge --ff-only <branch>` (ff-only enforces the wave tag sequence). If non-ff: `git rebase origin/main` in worktree first.
5. Full verification: `uv run pytest -q && just lint && uv run mypy src --strict`. If red → spawn follow-up coder in same worktree (`git worktree list` to locate) with the diff + failure output. Max 3 retries, then sequential fallback.
6. Remove worktree: `git worktree remove <path>` (only after successful merge).
7. Tag next wave pre-start: `git tag phase7-pre-wave-<N+1>`.

### Merge-conflict playbook (parallel waves only)

For parallel waves (3, 4, 5, 7, 8, 11), orchestrator merges commits in the order listed in the wave section. Second+ merges in a wave may hit conflicts because the first merge added imports or modified shared files:

- **Wave 3 conflict risk:** low — `config.py` vs `adapters/base.py` are disjoint. Conflict only if an earlier wave left an unresolved import in shared `adapters/__init__.py` re-exports.
- **Wave 4 conflict risk:** minimal — four disjoint tool directories. Possible only in `pyproject.toml` if a coder adds a tool-specific script entry (don't — tools are invoked by path).
- **Wave 5 conflict risk:** low — new `media/` package + new `dispatch_reply.py`. Only `src/assistant/adapters/__init__.py` is a candidate if coder adds re-exports.
- **Wave 7 conflict risk:** MEDIUM — both touch `src/assistant/adapters/base.py` transitively via `IncomingMessage` / `MediaAttachment` imports only; commit 12 imports from base, commit 13 imports from base. Actual file sets disjoint. BUT both edit tests that import `MessengerAdapter` — watch `tests/test_adapters_*.py`.
- **Wave 8 conflict risk:** minimal — `scheduler/dispatcher.py` vs `subagent/hooks.py`. Only risk is `main.py` if a coder accidentally edits Daemon plumbing (forbidden — that's Wave 9 commit 16).
- **Wave 11 partitions:** zero intra-partition risk (each sub-commit creates ONE new test file). Inter-partition only if a file touches the same conftest fixture — forbid conftest edits in wave 11.

### Fallback

If >50% of waves require >1 follow-up, set `PHASE7_PARALLEL_DISABLED=1` and re-run Waves 3+ sequentially with a single coder per commit (sequential fallback, §7 parallel-split-agent.md).
