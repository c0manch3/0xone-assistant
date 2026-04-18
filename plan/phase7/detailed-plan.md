# Phase 7 — detailed plan (stub)

See [description.md](./description.md) as the authoritative summary for this refresh. The Plan agent produced a ~1000-line draft covering the sections listed below, but the transcript of that draft could not be retrieved during the rewrite, so this file is a placeholder pointer until the Plan agent is rerun.

Expected sections (from the lost draft):

- Spike 0: multimodal envelope validation under `claude-agent-sdk` (inline base64 vs path+vision-tool vs external OCR).
- Mental model: adapter delivers files to disk + builds `MediaAttachment`; model decides what to do via CLI tools.
- AgentDefinition integration — explicitly NOT needed in phase 7, reuses phase-6 subagent infrastructure (`task spawn --kind worker`, `subagent_jobs`, SubagentStop hook).
- Four CLI contracts: `tools/transcribe/`, `tools/genimage/`, `tools/extract-doc/`, `tools/render-doc/` (thin HTTP clients over SSH tunnel to Mac, plus local extract/render fallback).
- `SKILL.md` per tool with `allowed-tools` scoped to `Bash` + `Read` for stage files.
- Adapter changes: new `_on_voice`, `_on_photo`, `_on_document`, `_on_audio`, `_on_video_note` handlers; outbound artifact detector on `<data_dir>/media/outbox`.
- Multimodal envelope construction in `ClaudeHandler` (image content-blocks + system-notes for non-image media).
- `dispatch_reply` shared helper consumed by `TelegramAdapter`, `SchedulerDispatcher`, and the phase-6 `SubagentStop` hook to unify outbound delivery (closes phase-4 tech debt around duplicated send paths).
- Bash allowlist additions (`_BASH_PROGRAMS` entries with structural validation on `--out`, `--body-file`, and file/URL arguments; SSRF guard reuse from phase-3).
- `MediaSettings(BaseSettings, env_prefix="MEDIA_")` with paths, caps, and provider keys; registered as `Settings.media`.
- Retention sweeper piggybacking on phase-3 `_sweep_run_dirs` (inbox >14d, outbox >7d, LRU eviction to stay under a total size cap).
- `_memlib` refactor — consolidate `sys.path.append` pattern from phases 4/5 into `tools/__init__.py` + `from tools.<name>._lib import …`, closing phase-4 tech debt.
- Integration with phase-6 `SubagentStop` hook — long-running transcribe/genimage runs as worker subagent, result delivered via `dispatch_reply`.
- 20+ tests across unit (per-CLI with mocked backends / fixture docs), integration (handler with fake `MediaAttachment`), and E2E (stub photo → bridge envelope carries image block; regression for phase-2 path-guard on outbox).

**Action item**: rerun Plan agent to regenerate the full ~1000-line plan, then overwrite this stub.
