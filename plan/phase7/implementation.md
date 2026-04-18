# Phase 7 — Implementation v2 (fix-pack, 2026-04-17)

Thin layer over phase-6 subagent infrastructure + adapter extensions.
Media path: Telegram voice/photo/document/audio/video_note → handler
envelope → model decides inline CLI vs `task spawn --kind worker` →
tools/* returns a path in `<data_dir>/media/outbox/` → `dispatch_reply`
detects the path and sends as photo/document/audio.

Empirical backing:
- `spikes/phase7_s0_multimodal_envelope.py` — SDK multimodal envelope (BLOCKER).
- `spikes/phase7_s1_endpoint_ssrf.py` — IPv6/SSRF loopback-only guard.
- `spikes/phase7_s2_artefact_regex.py` — 46-case regex corpus; v3 recommended.
- `spikes/phase7_s3_fpdf2_cyrillic.py` — Cyrillic PDF (Pillow required).
- `spikes/phase7_s4_venv_deps.py` — 48.75 MB dep delta.
- `spikes/phase7_s5_quota_race.py` — flock quota race at midnight.
- `spikes/phase7_s6_bot_download.py` — None-tolerant size cap pattern.
- `spikes/phase7_s7_handler_partial_fail.py` — safe multi-attachment pseudocode.
- `spikes/phase7_s8_x86_64_linux_wheels.py` — manylinux wheel audit (v2 fix-pack).
- `plan/phase7/spike-findings.md` — consolidated verdict + evidence.

Companion docs (coder **must** read first):
- `plan/phase7/description.md` — E2E scenarios.
- `plan/phase7/detailed-plan.md` — canonical spec §1–§20.
- `plan/phase7/parallel-split-agent.md` — wave-plan generator.
- `plan/phase6/implementation.md` — style precedent.
- `plan/phase6/summary.md` — phase-6 invariants this phase preserves.

**Auth:** OAuth via `claude` CLI. No `ANTHROPIC_API_KEY`.

## Revision history

- **v1** (2026-04-17, after S-0..S-7): initial coder-ready spec.
- **v2** (2026-04-17, fix-pack): closes devil wave-2 blockers C-1..C-6 and
  highs H-7..H-15. Key deltas:
  - **C-1:** root `pyproject.toml` now carries phase-7 deps (single venv,
    Option A). Per-tool `pyproject.toml` files demoted to doc artefacts;
    coder ships only `__init__.py` markers under `tools/<name>/`.
  - **C-2:** real-photo fixture requirement promoted to REQUIRED in §7.
  - **C-3:** aiogram 3.26 source-audited — `__download_file_binary_io`
    does NOT swallow `SizeCapExceeded`; pattern validated. `SizeCappedWriter`
    MUST implement `write()` AND `flush()` (aiogram calls both).
  - **C-4:** `photo_mode="path_tool"` silent-drop fixed — explicit
    `elif` branch in §3.1 handler.
  - **C-5:** Wave 6 split into parallel Wave 6A (commit 12) + Wave 6B
    (commit 13) — disjoint file sets.
  - **C-6:** adapter-level dedup of `IncomingMessage.attachments` by
    `local_path` (invariant I-7.6).
  - **H-7/S-8:** x86_64 manylinux_2_28 wheel audit passed (9/9 packages).
  - **H-8:** `Pillow>=10.4,<13` pinned at root; fpdf2 capped `<3`.
  - **H-9:** exact `_memlib` test-file list (8 import-level + conftest.py).
  - **H-10:** history placeholder now includes turn_id.
  - **H-11:** `make_subagent_hooks` drops `outbox_root` param (derived inside).
  - **H-12:** dedup ledger test split into real-clock (xfail-if-flaky)
    and mock-clock (authoritative) variants.
  - **H-13:** `colon_before` regex fallback fixed at prompt level.
  - **H-14:** all-photos-fail scenario covered.
  - **H-15:** wave-plan.md invalidation rule added to `parallel-split-agent.md`.

## 0. Pitfall box (MUST READ) — spike-verified

Hard rules from spike evidence + phase-5/6 hard-won lessons. Each
either has a spike citation or a phase-summary cross-ref.

1. **DO NOT** claim "fpdf2 renders without Pillow". S-3 confirmed
   `fpdf2>=2.7` declares `Pillow>=8.3.2` in `Requires-Dist`; importing
   `fpdf` at any point triggers PIL module load (22 PIL submodules
   observed in `sys.modules`). **Plan `description.md` §82 is wrong and
   must be edited in the Plan-revision commit.** Accept Pillow as a
   required transitive dep. **(v2 fix-pack, H-8):** Pillow pinned
   `>=10.4,<13` at root `pyproject.toml`. 10.4 is the minimum release
   without known CVE-2023-50447/CVE-2024-28219 class decoder RCEs at
   planning time. Upper bound `<13` protects against API churn. Review
   Pillow CVE feed before each upgrade — it is on the hot path
   (photo decode + fpdf2 render).

2. **DO NOT** use plan §7's regex as written. S-2 found 4 real false
   positives (URL containing a `.png` path; relative `./outbox/x.png`;
   adjacent paths). Use **v3** (spike-verified):
   ```python
   _ARTEFACT_RE = re.compile(
       r"(?<![\w/.:])(/[^\s`\"'<>()\[\]]+?"
       rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
       r"(?=[\s`\"'<>()\[\].,;:!?/]|$)",
       re.IGNORECASE,
   )
   ```

3. **DO NOT** rely on `MediaAttachment.file_size` for download-cap
   enforcement alone. S-6 confirmed aiogram's `File.file_size: int |
   None` for all 5 media kinds. Pre-flight check is insufficient;
   pair it with a streaming `SizeCappedWriter` that aborts when the
   wrapped writer exceeds `cap` bytes. On abort: `dest_path.unlink
   (missing_ok=True)` to clean up the partial file.

   **(v2 fix-pack, C-3):** aiogram 3.26 `Bot.download_file` source
   audit (`.venv/lib/python3.12/site-packages/aiogram/client/bot.py`
   lines 371-444) verified that `__download_file_binary_io` is a bare
   `async for chunk in stream: destination.write(chunk); destination.
   flush()` loop with NO try/except. `SizeCapExceeded` raised inside
   `write()` propagates upward through the async generator and out of
   `download_file` — the cap IS enforced. Implementation MUST:
   - Pass a custom BinaryIO wrapper (NOT `str | Path`) so aiogram
     takes the `BinaryIO` branch (line 439-441). A string/Path sink
     would go to `__download_file` which uses `aiofiles.open(...)`
     directly and bypasses our wrapper.
   - Implement BOTH `write(data: bytes) -> int` AND `flush() -> None`
     on `SizeCappedWriter` (aiogram calls both per chunk).
   - Catch `SizeCapExceeded` in the caller, `unlink(missing_ok=True)`
     the partial file, re-raise or reject with appropriate reply.
   - Fallback plan documented (deferred, not implemented): if a
     future aiogram release DOES swallow the exception, the pre-flight
     `file.file_size` check still rejects known-oversize files; the
     residual risk is a None-sized oversize file slipping through.
     Mitigation: post-download `dest_path.stat().st_size > cap` check
     with explicit unlink + reject (deferred to phase 9 unless CI
     catches a regression).

4. **DO NOT** pass plan-as-written §6.1 pseudocode into the handler;
   S-7 showed a missing-photo in position 2 of 3 would crash the turn.
   Use the safe variant (§3.1 in this file, tested as PASS). Catch
   `(FileNotFoundError, PermissionError, OSError)` per attachment,
   push a failure-note in place, `log.warning(...)` with `exc_info=True`,
   continue.

5. **DO NOT** delegate CLI `_validate_endpoint(url)` to phase-3's
   `classify_url`. S-1 showed `classify_url` allows `10.x`, `192.168.x`
   because those are "private" — but they are NOT loopback, and the
   phase-7 host is reachable only via SSH reverse tunnel on
   `127.0.0.1:<port>`. Implement a narrower `is_loopback_only(url)`
   that DNS-resolves the hostname and requires EVERY resolved address
   to be loopback. Mirror the helper into the CLI module (stdlib-only)
   just like `_net_mirror.py`.

6. **DO NOT** under-estimate dep size. S-4 measured +48.75 MB venv
   delta; top contributors are lxml (18.8 MB via `python-docx`),
   Pillow (12.5 MB via `fpdf2`), fontTools (11.8 MB via `fpdf2`).
   Update `detailed-plan.md §9` MediaSettings prose.

7. **DO NOT** write the quota file without `fcntl.flock(fd, LOCK_EX)`.
   S-5 R-3 confirmed 10 concurrent workers race without it; with flock,
   exactly one wins. NTP rollback across midnight allows an extra
   request on the prior day — accepted as known ±1 jitter (document
   in SKILL.md).

8. **DO NOT** expect Spike 0's 10 MB PASS to translate to real photos.
   The spike used padded-COM JPEGs with null-byte payloads (highly
   compressible over HTTP/2 HPACK + gzip; real entropy ~7.5 bits/byte
   JPEGs wire-transfer at ~1.33× their base64-encoded size). Real
   10 MB JPEGs may be rejected by content-moderation or compressed-size
   caps the API does not publicise. Keep `MEDIA_PHOTO_MAX_INLINE_BYTES
   =5_242_880` (plan default).

   **(v2 fix-pack, C-2):** integration test `test_handler_multimodal
   _real_photo.py` is now REQUIRED (not "SHOULD"). Fixture: real JPEG
   ≥3 MB at `tests/fixtures/phase7/real_photo_3mb.jpg` (real entropy,
   NOT null-padded). If SDK rejects real-photo, fallback to
   `MEDIA_PHOTO_MODE=path_tool` (see pitfall 16 below). Optional bonus:
   a Cyrillic-filename variant (`tests/fixtures/phase7/фото_3mb.jpg`)
   to shake out UTF-8 path-handling bugs in the adapter/handler/CLI
   chain.

9. **DO NOT** dedup only in `dispatch_reply` at send-time — also add
   **prompt-level guidance** in SKILL.md §4.5 (and `system_prompt.md`)
   telling the model NOT to mention absolute paths in main-turn final
   text after `task spawn --kind worker`. Two-level mitigation (plan
   §7, §12.3) survives model disobedience.

10. **DO NOT** skip the `exists()` check after path-guard. Regex v3
    may extract `/abs/outbox/x.png` from `/abs/outbox/x.png/y`; only
    the combination `resolve().is_relative_to(outbox) AND exists()`
    distinguishes real artefacts from noise.

11. **DO NOT** hard-code hyphenated dir names as Python package
    imports. `tools/skill-installer/` → `tools/skill_installer/`
    (detailed-plan §11.1) is load-bearing. Ditto `tools/extract-doc/`
    → `tools/extract_doc/` and `tools/render-doc/` → `tools/render_doc/`.
    Bash allowlist stays keyed on the underscore form (§13.1 note).

12. **DO NOT** close `subagent/store.py` / `subagent/picker.py` /
    `subagent/definitions.py` — phase-6 invariants preserved. Only
    `subagent/hooks.py::on_subagent_stop` changes
    `adapter.send_text(...)` → `dispatch_reply(...)` (plan §12.2).

13. **DO NOT** spam Telegram with `adapter.send_photo` on every
    artefact without `TelegramRetryAfter` retry wrapping. Mirror the
    `send_text` wave-2 retry pattern (§5.3 new abstract methods).

14. **DO NOT** launch `media_sweeper_loop` before `Daemon.start()`
    has called `ensure_media_dirs()`. Sweeper on a non-existing inbox
    would log `FileNotFoundError` on first tick. Order in
    `Daemon.start`: `ensure_media_dirs()` → `_spawn_bg(media_sweeper_loop(...))`.

15. **DO NOT** bump `HISTORY_MAX_SNIPPET_TOTAL_BYTES` in phase 7.
    Still phase-9 tech debt (plan description §14). Photos add
    `[image: <path>]` placeholder to history per S-0 Q0-6 mode-B —
    negligible byte cost.

16. **(v2 fix-pack, C-4) DO NOT** leave `photo_mode="path_tool"` as
    a silent no-op. The v1 handler had only the
    `photo_mode=="inline_base64"` branch — setting
    `MEDIA_PHOTO_MODE=path_tool` silently dropped photo attachments
    (no `image_block` AND no note → model had no idea a photo was
    sent). v2 §3.1 adds an explicit `elif att.kind == "photo" and
    photo_mode == "path_tool":` branch that emits a note-only
    envelope steering the model to a future vision CLI (or graceful
    acknowledgement). Covered by
    `test_handler_photo_path_tool_fallback.py`.

17. **(v2 fix-pack, C-6) DO NOT** assume Telegram delivers each
    attachment exactly once per `IncomingMessage`. Edit-message
    updates and media-group regrouping can re-fire the same
    file_id/local_path in one update cycle. Dedup invariant I-7.6:
    `TelegramAdapter` normalises `IncomingMessage.attachments` to
    contain unique `local_path` values before dispatching. Duplicates
    are dropped at adapter level with `log.debug("attachment_dedup",
    path=..., chat_id=...)`. Handler code (§3.1) loops assuming the
    invariant; re-dedup there would only hide adapter bugs.

18. **(v2 fix-pack, H-13) DO** include "always put a space after `:`
    before an outbox path" as an explicit prompt-level rule in every
    phase-7 `SKILL.md` + `bridge/system_prompt.md`. Regex v3 rejects
    `готово:/abs/outbox/x.png` (no space) to avoid false positives on
    URL scheme colons; the model must compensate with a space before
    the path. Good: `готово: /abs/outbox/x.png`. Bad: `готово:/abs/
    outbox/x.png`. Integration test asserts regex matches the
    space-separated form produced by the skill-guided reply.

## 1. Commit plan (20 commits, parallel-wave annotated) — v2

Each commit under ~500 LOC diff. Coder runs `just lint && uv run pytest -x`
between commits. Wave markers reference `detailed-plan.md §19.3` so the
parallel-split agent can translate them to worktree assignments.

**(v2 fix-pack, C-1):** commit 2b inserted — root `pyproject.toml`
update runs BEFORE any tool/media commit. Per-tool `pyproject.toml`
files (listed in original §13.1) are NOT shipped; only `__init__.py`
markers under `tools/<name>/`. Single shared venv (Q-7-5).

| # | Title | Wave | Dependencies |
|---|-------|------|--------------|
| 1 | Spike 0 findings + spike scripts (+ this doc) | standalone | — |
| 2 | `_memlib` → `_lib` refactor (Q9a tech debt close) | seq | #1 |
| 2b | **(v2 new, C-1)** Root `pyproject.toml` — add phase-7 deps (Pillow/pypdf/python-docx/openpyxl/striprtf/defusedxml/fpdf2/lxml) with CVE-floor pins | seq | #2 |
| 3 | `MediaSettings` config (env_prefix MEDIA_) | seq | #2b |
| 4 | `MediaAttachment` + `IncomingMessage.attachments` + adapter abstracts | seq | #2b |
| 5 | `src/assistant/media/` sub-package (paths, download, sweeper, artefacts) | **Wave B** | #3 |
| 6 | `adapters/dispatch_reply.py` + `_DedupLedger` | **Wave B** | #4 |
| 7 | `tools/transcribe/` + skill + thin-HTTP client | **Wave A** | #2b |
| 8 | `tools/genimage/` + skill + flock quota | **Wave A** | #2b |
| 9 | `tools/extract_doc/` + skill + local extractor | **Wave A** | #2b |
| 10 | `tools/render_doc/` + skill + fpdf2/docx render | **Wave A** | #2b |
| 11 | Bash allowlist: `_validate_transcribe_argv` / `_genimage` / `_extract_doc` / `_render_doc` + factory plumbing | seq | #7-#10 |
| 12 | `TelegramAdapter` media handlers + `send_photo/document/audio` + attachment-dedup (I-7.6) | **Wave 6A** | #4, #6 |
| 13 | Handler + bridge multimodal envelope (safe pseudocode per S-7; v2: path_tool branch, turn-id placeholder) | **Wave 6B** | #4, #11 |
| 14 | `SchedulerDispatcher._deliver` → `dispatch_reply` switch | **Wave C** | #6 |
| 15 | `subagent/hooks.py::on_subagent_stop` → `dispatch_reply` switch (v2: factory drops `outbox_root` param — derived inside) | **Wave C** | #6 |
| 16 | `Daemon.start` integration (`ensure_media_dirs`, media-sweeper bg, `_dedup_ledger`) | seq | #3, #5, #14, #15 |
| 17 | Integration E2E tests | seq | #11-#16 |
| 18 | Unit tests (~20 files) | **Wave D** (partitions of 4) | all code |
| 19 | Documentation update (description wording fix, SKILL.md §4.5 + `:` space rule H-13) | seq | all |

Total: ~2030 LOC src + ~740 LOC modified + ~1500 LOC tests = ~4270 LOC.

**(v2 fix-pack, C-5)** Wave 6 SPLIT into 6A (commit 12, `adapters/
telegram.py` +200 LOC) and 6B (commit 13, `handlers/message.py`,
`bridge/claude.py`, `bridge/history.py` +105 LOC). File sets are
disjoint — parallelisable. `parallel-split-agent.md §3` wave template
updated.

## 2. Per-file signature specs

### 2.1 `src/assistant/adapters/base.py`

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["voice", "photo", "document", "audio", "video_note"]

@dataclass(frozen=True, slots=True)
class MediaAttachment:
    kind: MediaKind
    local_path: Path
    mime_type: str | None = None
    file_size: int | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    filename_original: str | None = None
    telegram_file_id: str | None = None

@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    text: str
    message_id: int | None = None
    origin: Origin = "telegram"
    meta: dict[str, Any] | None = None
    attachments: tuple[MediaAttachment, ...] | None = None  # NEW

class MessengerAdapter(ABC):
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...
    # NEW:
    @abstractmethod
    async def send_photo(self, chat_id: int, path: Path, *, caption: str | None = None) -> None: ...
    @abstractmethod
    async def send_document(self, chat_id: int, path: Path, *, caption: str | None = None) -> None: ...
    @abstractmethod
    async def send_audio(self, chat_id: int, path: Path, *, caption: str | None = None) -> None: ...
```

### 2.2 `src/assistant/media/paths.py`

```python
from pathlib import Path

def inbox_dir(data_dir: Path) -> Path: ...  # <data_dir>/media/inbox
def outbox_dir(data_dir: Path) -> Path: ...  # <data_dir>/media/outbox
def stage_dir(data_dir: Path) -> Path: ...   # <data_dir>/run/render-stage

async def ensure_media_dirs(data_dir: Path) -> None:
    """Create inbox/outbox/stage with 0700 perms. Idempotent."""
```

### 2.3 `src/assistant/media/download.py`

```python
from aiogram import Bot
from aiogram.types import File
from pathlib import Path

class SizeCapExceeded(Exception): ...

class _SizeCappedWriter:
    def __init__(self, dest: BinaryIO, cap: int) -> None: ...
    def write(self, data: bytes) -> int: ...

async def download_telegram_file(
    bot: Bot,
    file_id: str,
    dest_dir: Path,
    suggested_filename: str,
    *,
    max_bytes: int,
    timeout_s: int = 30,
) -> Path:
    """Download a Telegram file to <dest_dir>/<uuid>.<ext>.

    - Pre-flight: if file.file_size is not None AND > max_bytes, raise
      SizeCapExceeded immediately.
    - Streaming: open dest with 'wb', wrap in _SizeCappedWriter(cap=max_bytes),
      call bot.download_file(file.file_path, destination=sink). On
      SizeCapExceeded: unlink partial file, re-raise.
    - Return resolved absolute Path of the saved file.

    Cap taxonomy (per phase-7 MediaSettings defaults):
      voice/audio: 15 MB  (Bot API cap is 20 MB; leave 25% margin)
      photo:       10 MB  (oversize → skip inline, keep path)
      document:    20 MB  (at-ceiling)
    """
```

### 2.4 `src/assistant/media/sweeper.py`

```python
import asyncio
from pathlib import Path

async def sweep_media_once(data_dir: Path, settings: Settings, log) -> dict[str, int]:
    """One-shot pass: (1) age-based unlink inbox>14d / outbox>7d;
    (2) LRU evict oldest if total>2GB (outbox evicted first).
    Returns {"removed_old": N, "removed_lru": M, "bytes_freed": ...}.
    """

async def media_sweeper_loop(
    data_dir: Path,
    settings: Settings,
    stop_event: asyncio.Event,
    log,
) -> None:
    """Infinite loop; sleeps settings.media.sweep_interval_s between sweeps.
    Yields to stop_event.wait() for shutdown responsiveness.
    """
```

### 2.5 `src/assistant/media/artefacts.py`

```python
import re
from pathlib import Path

_PHOTO_EXT = (".png", ".jpg", ".jpeg", ".webp")
_AUDIO_EXT = (".mp3", ".ogg", ".oga", ".wav", ".m4a", ".flac")
_DOC_EXT = (".pdf", ".docx", ".txt", ".xlsx", ".rtf")
_ALL_EXT = _PHOTO_EXT + _AUDIO_EXT + _DOC_EXT

# v3 per S-2 spike (see §3 in spike-findings.md).
ARTEFACT_RE: re.Pattern[str] = re.compile(
    r"(?<![\w/.:])(/[^\s`\"'<>()\[\]]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?=[\s`\"'<>()\[\].,;:!?/]|$)",
    re.IGNORECASE,
)

def classify_artefact(path: Path) -> str:
    """Return one of {'photo', 'audio', 'document'} based on suffix.lower()."""
```

### 2.6 `src/assistant/adapters/dispatch_reply.py`

```python
import time
from collections import OrderedDict
from pathlib import Path

_DEDUP_TTL_S = 300.0
_DEDUP_MAX_ENTRIES = 256

class _DedupLedger:
    """Per-daemon in-process LRU+TTL ledger for dispatch_reply.

    Key: (resolved_path_str, chat_id). Value: monotonic timestamp.
    Mark-and-check:
      * if key seen within TTL: return True (caller SKIPS send) + refresh LRU pos.
      * else: record, LRU-trim to max_entries, return False.
    Side-effect: evict-expired runs on every call (O(n) worst case,
    bounded by max_entries=256).
    """

    def __init__(self, *, ttl_s: float = _DEDUP_TTL_S, max_entries: int = _DEDUP_MAX_ENTRIES) -> None: ...
    def mark_and_check(self, key: tuple[str, int], now: float) -> bool: ...
    def _evict_expired(self, now: float) -> None: ...

async def dispatch_reply(
    adapter: MessengerAdapter,
    chat_id: int,
    text: str,
    *,
    outbox_root: Path,
    dedup: _DedupLedger,
    log_ctx: dict[str, Any] | None = None,
) -> None:
    """Extract media artefacts, send via adapter.send_photo/document/audio,
    send cleaned text. Idempotent via `dedup` within 300 s sliding window.
    I-7.5 invariant.
    """
```

### 2.7 `tools/transcribe/main.py` (HTTP thin client, stdlib-only)

```python
# CLI: python tools/transcribe/main.py <path> [--language X] [--timeout-s N]
#      [--format text|segments] [--endpoint URL]

# sys.path pragma (per §11.2) for cwd + module invocation parity:
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Structure:
def main() -> int:
    args = _parse_argv(sys.argv[1:])
    _validate_path(args.path)
    _validate_endpoint(args.endpoint)  # loopback-only via _is_loopback_only
    # Build multipart body (stdlib: urllib + mimetypes + email)
    body, content_type = _encode_multipart(...)
    # POST
    req = Request(args.endpoint, data=body, method="POST",
                  headers={"Content-Type": content_type})
    try:
        with urlopen(req, timeout=args.timeout_s) as resp:
            data = json.load(resp)
    except HTTPError as exc:
        return _exit(4, f"server returned {exc.code}")
    except URLError as exc:
        return _exit(4, f"endpoint unreachable: {exc.reason}")
    except TimeoutError:
        return _exit(4, f"timeout after {args.timeout_s}s")
    # Emit JSON on stdout
    print(json.dumps(data))
    return 0

# Exit codes: 0 OK, 2 argv invalid, 3 path-guard, 4 network, 5 unknown.
```

### 2.8 `tools/genimage/main.py` (HTTP + flock quota)

```python
# CLI: python tools/genimage/main.py --prompt TEXT --out PATH [--width N] ...

def _check_and_increment_quota(path: Path, cap: int) -> bool:
    """fcntl.flock(LOCK_EX); read {"date":"YYYY-MM-DD","count":N};
    if date != today: reset; if count>=cap: return False; else ++ and
    write; release. Returns True iff allowed. See S-5 spike.
    """
```

Exit codes: 0 OK, 2 argv, 3 path, 4 network, 5 unknown, **6 quota**.

### 2.9 `tools/extract_doc/main.py` (local)

```python
# CLI: python tools/extract_doc/main.py <path> [--max-chars N] [--pages N-M]

# Deps: pypdf>=4.0, python-docx>=1.0, openpyxl>=3.1, striprtf>=0.0.28, defusedxml>=0.7
# XML parsing via defusedxml (zip-bomb guard + entity-expansion guard)
```

### 2.10 `tools/render_doc/main.py` (local, fpdf2 + python-docx)

```python
# CLI: python tools/render_doc/main.py --body-file PATH --out PATH [--title T] [--font DejaVu]

# sys.path pragma
# Path-guards: --body-file under <data_dir>/run/render-stage/
#              --out under <data_dir>/media/outbox/

# fpdf2 imports PIL (see S-3). Accept.
# DejaVu Sans TTF vendored at tools/render_doc/_lib/DejaVuSans.ttf
```

### 2.11 `src/assistant/config.py` (+50 LOC MediaSettings)

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

class MediaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEDIA_", env_file=".env", extra="ignore")

    # Photo path
    photo_mode: Literal["inline_base64", "path_tool"] = "inline_base64"  # S-0 PASS default
    photo_max_inline_bytes: int = 5_242_880   # 5 MB (S-0 Q0-3)
    photo_download_max_bytes: int = 10_485_760  # 10 MB

    # Voice / audio
    voice_max_sec: int = 1800
    voice_inline_threshold_sec: int = 30
    voice_max_bytes: int = 15_000_000  # S-6: below 20 MB Bot-API cap
    audio_max_bytes: int = 50_000_000

    # Document
    document_max_bytes: int = 20_971_520

    # Transcribe (HTTP client)
    transcribe_endpoint: str = "http://localhost:9100/transcribe"
    transcribe_language_default: str = "auto"
    transcribe_timeout_s: int = 60
    transcribe_max_input_bytes: int = 25_000_000

    # Genimage (HTTP client + quota)
    genimage_endpoint: str = "http://localhost:9101/generate"
    genimage_daily_cap: int = 1
    genimage_steps_default: int = 8
    genimage_timeout_s: int = 120

    # Extract / Render
    extract_max_input_bytes: int = 20_000_000
    render_max_body_bytes: int = 512_000
    render_max_output_bytes: int = 10_485_760

    # Retention
    retention_inbox_days: int = 14
    retention_outbox_days: int = 7
    retention_total_cap_bytes: int = 2_147_483_648  # 2 GB
    sweep_interval_s: int = 3600

class Settings(BaseSettings):
    # ... existing fields ...
    media: MediaSettings = Field(default_factory=MediaSettings)
```

### 2.12 `src/assistant/bridge/hooks.py` — factory signature (backward-compat)

```python
def make_pretool_hooks(
    project_root: Path,
    data_dir: Path | None = None,  # NEW — optional for backward-compat
) -> dict[str, list[HookMatcher]]: ...

def make_bash_hook(
    project_root: Path,
    data_dir: Path | None = None,  # NEW
) -> HookFn: ...

def make_file_hook(
    project_root: Path,
    data_dir: Path | None = None,  # NEW — allows writes to stage-dir
) -> HookFn: ...
```

Backward-compat: 9 existing test files (see detailed-plan §8.1) call
factories WITHOUT `data_dir`. Keyword-default `None` keeps them green.
When `data_dir is None` and argv targets `tools/render_doc/main.py`,
the hook returns an explicit deny (reason: "render-doc requires
data_dir-bound hooks").

### 2.13 `src/assistant/bridge/claude.py::ClaudeBridge.ask` extension

```python
async def ask(
    self,
    chat_id: int,
    user_text: str,
    history: list[dict[str, Any]],
    *,
    system_notes: list[str] | None = None,
    image_blocks: list[dict[str, Any]] | None = None,  # NEW
) -> AsyncIterator[Any]: ...
```

In `prompt_stream()`:
- If `image_blocks` OR `system_notes`: build mixed content list
  (`{"type":"text", "text": user_text}` → image_blocks in order →
  system-notes as `{"type":"text","text":"[system-note: ...]"}`).
- Else: `user_content = user_text` (unchanged).

Order MATCHES S-0 Q0-5b probe: `text → image → system-notes`. Plan §6.2
stands.

## 3. Per-file edit specs

### 3.1 `src/assistant/handlers/message.py` (safe multi-attachment envelope) — v2 fix-pack (C-4)

Insertion point: after `system_notes` build, before `self._bridge.ask`
call (~line 206 in current file).

**v2 deltas:**
- New explicit `elif att.kind == "photo" and photo_mode == "path_tool":`
  branch — closes C-4 silent-drop bug.
- Handler relies on adapter-level dedup (I-7.6, C-6); no per-call
  `seen_paths` loop here.

```python
import base64
# ...

# Phase 7: attachments → image_blocks + notes (safe per S-7).
image_blocks: list[dict] = []
photo_mode = self._settings.media.photo_mode
for att in msg.attachments or ():
    if att.kind == "photo" and photo_mode == "inline_base64":
        cap = self._settings.media.photo_max_inline_bytes
        if att.file_size is not None and att.file_size > cap:
            notes.append(
                f"user attached photo at {att.local_path} but size "
                f"{att.file_size} exceeds inline cap {cap}; skipped."
            )
            continue
        try:
            raw = att.local_path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            notes.append(
                f"user attempted to attach photo at {att.local_path} "
                f"but read failed: {type(exc).__name__}."
            )
            log.warning(
                "media_photo_read_failed",
                path=str(att.local_path),
                chat_id=msg.chat_id,
                turn_id=turn_id,
                exc_info=True,
            )
            continue
        mime = att.mime_type or "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        image_blocks.append(
            {"type": "image",
             "source": {"type": "base64", "media_type": mime, "data": b64}}
        )
        notes.append(
            f"user attached photo at {att.local_path} ({att.width}x{att.height})"
        )
    elif att.kind == "photo" and photo_mode == "path_tool":
        # (v2 fix-pack, C-4) Explicit fallback branch — env flag
        # MEDIA_PHOTO_MODE=path_tool disables inline base64. Model
        # still gets a note so the photo isn't silently lost.
        notes.append(
            f"user attached photo at {att.local_path} "
            f"({att.width}x{att.height}). Photo-inline mode disabled "
            f"(path_tool fallback); describe via vision tool if "
            f"available, or acknowledge receipt."
        )
    elif att.kind in ("voice", "audio"):
        notes.append(
            f"user attached {att.kind} (duration={att.duration_s}s) at "
            f"{att.local_path}. use tools/transcribe/; if >30s spawn worker."
        )
    elif att.kind == "document":
        notes.append(
            f"user attached document '{att.filename_original}' at "
            f"{att.local_path}. use tools/extract-doc/."
        )
    elif att.kind == "video_note":
        notes.append(
            f"user attached video_note (duration={att.duration_s}s) at "
            f"{att.local_path}. video out of scope phase 7."
        )
    else:
        notes.append(f"unknown attachment kind={att.kind!r} at {att.local_path}")

system_notes = notes or None

# Pass image_blocks to bridge.ask (new kwarg):
async for item in self._bridge.ask(
    msg.chat_id, msg.text, history,
    system_notes=system_notes,
    image_blocks=image_blocks or None,
):
    ...
```

**Test matrix additions (v2):**
- `test_handler_photo_path_tool_fallback.py` — env
  `MEDIA_PHOTO_MODE=path_tool` → photo produces note-only envelope
  (no `image_blocks`), no silent drop. Asserts note text contains
  "path_tool fallback".
- `test_handler_multimodal_all_photos_fail.py` (H-14) — 3 photos,
  every `read_bytes()` raises `FileNotFoundError` (simulate sweeper
  eviction mid-turn). Assert envelope has 3 failure-notes + zero
  `image_blocks`, `bridge.ask` is still invoked (no crash), 3
  `log.warning("media_photo_read_failed", ...)` calls recorded.
- `test_handler_multimodal_real_photo.py` (C-2) — fixture JPEG
  ≥3 MB real entropy. Assert SDK invocation succeeds (gated by
  `RUN_SDK_INT=1`).

### 3.2 `src/assistant/bridge/claude.py::ClaudeBridge.ask` prompt_stream

Modify existing `prompt_stream` inner `if system_notes:` branch to also
handle `image_blocks`:

```python
async def prompt_stream() -> AsyncIterator[dict[str, Any]]:
    for envelope in history_to_user_envelopes(
        history, chat_id, tool_result_truncate=truncate
    ):
        yield envelope
    if system_notes or image_blocks:
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": user_text},
        ]
        for blk in image_blocks or ():
            content_blocks.append(blk)
        for note in system_notes or ():
            content_blocks.append({"type": "text", "text": f"[system-note: {note}]"})
        user_content: str | list[dict[str, Any]] = content_blocks
    else:
        user_content = user_text
    yield {
        "type": "user",
        "message": {"role": "user", "content": user_content},
        "parent_tool_use_id": None,
        "session_id": f"chat-{chat_id}",
    }
```

### 3.3 `src/assistant/bridge/history.py` (photo row → placeholder) — v2 fix-pack (H-10)

Per S-0 Q0-6 mode-B: convert historic image content blocks into a
synthetic text note. Insertion point in `history_to_user_envelopes`
after the `role=="user"` block-walking loop.

**v2 fix-pack (H-10):** placeholder includes `turn_id` so the note
anchors to its originating turn even when block order within a row
is flaky (phase-2 `ConversationStore.append` stores blocks in
insertion order; replay flattens them — without `turn_id`, a model
replaying >1 image turn couldn't distinguish which image belonged
to which turn).

```python
# Phase 7 (v2 H-10): image blocks from prior turns → synthetic
# placeholder text anchored to originating turn.
for row in by_turn[turn_id]:
    if row["role"] == "user":
        for block in row["content"]:
            if isinstance(block, dict) and block.get("type") == "image":
                # Placeholder; don't replay raw bytes.
                src = block.get("source") or {}
                media_type = src.get("media_type", "image/?")
                user_texts.append(
                    f"[system-note: in turn {turn_id} user sent image "
                    f"({media_type}) — raw bytes omitted from replay]"
                )
```

Note: phase-2 `ConversationStore.append` stores `[{"type":"image", ...}]`
on the user row; we only emit the placeholder on replay.

**Test additions (v2):**
- `test_history_replay_photo_turn_ordering.py` — 2 user turns, each
  with 1 image + 1 text. Assert replay produces 2 distinct placeholder
  notes with correct `turn_id` embedded; notes appear in the right
  conversational slot relative to the text block of their turn.

### 3.4 `src/assistant/adapters/telegram.py` (new handlers + send methods) — v2 fix-pack (C-6, L-20, L-21)

Insertion after `self._dp.message.register(self._on_text, F.text)`:

```python
# Phase 7 media handlers
self._dp.message.register(self._on_voice, F.voice)
self._dp.message.register(self._on_audio, F.audio)
self._dp.message.register(self._on_photo, F.photo)
self._dp.message.register(self._on_document, F.document)
self._dp.message.register(self._on_video_note, F.video_note)
```

Each handler:
1. Adapter-level size pre-check (reject early with "файл слишком большой").
2. Call `media/download.download_telegram_file(bot, file_id, inbox_dir(...), suggested, max_bytes=...)`.
3. Build `MediaAttachment(...)` tuple.
4. **(v2 fix-pack, C-6, I-7.6)** Before passing to handler, dedup by
   `local_path`: if the adapter already emitted an
   `IncomingMessage` with the same `local_path` to the same `chat_id`
   within the current update cycle (e.g. media_group regrouping,
   edit_message replay), skip. Implementation: keep a small
   per-adapter LRU `_emitted_attachments: OrderedDict[tuple[int,str],
   float] = {}` with 60 s TTL + 128 entries cap (mirrors
   `_DedupLedger` shape but scoped to attachment ingress).
5. Dispatch through existing handler path (emit/send).

```python
# (v2 C-6) adapter-level attachment dedup — I-7.6
key = (chat_id, str(local_path))
now = time.monotonic()
self._evict_expired_attachments(now)
if key in self._emitted_attachments:
    log.debug("attachment_dedup",
              chat_id=chat_id, path=str(local_path),
              last_seen=self._emitted_attachments[key])
    return
self._emitted_attachments[key] = now
```

**Send methods (v2 fix-pack, L-20, L-21):** mirror `send_text`'s
`TelegramRetryAfter` retry wrapper from phase-5. Explicit spec:

```python
async def send_photo(self, chat_id: int, path: Path, *, caption: str | None = None) -> None:
    for attempt in range(self._send_max_retries):
        try:
            await self._bot.send_photo(chat_id, FSInputFile(path), caption=caption)
            return
        except TelegramRetryAfter as exc:
            # (L-21) Retry-After honoured — same pattern as send_text
            await asyncio.sleep(exc.retry_after + 0.25)
            continue
        except TelegramNetworkError:
            if attempt == self._send_max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
            continue
        except (FileNotFoundError, PermissionError, OSError) as exc:
            # (L-20) file-read errors during send are NOT retryable;
            # log.warning + re-raise so caller (dispatch_reply)
            # can swallow and continue to send_text for remaining content.
            log.warning("send_photo_read_failed",
                        chat_id=chat_id, path=str(path),
                        exc_info=True)
            raise
```

Analogous structure for `send_document` / `send_audio`. The
`dispatch_reply` caller wraps each `send_*` in try/except
`Exception:` (already shown in §7 detailed-plan.md) and logs
`artefact_send_failed` without aborting remaining sends or the
cleaned-text `send_text` at the end.

**Test coverage additions (v2):**
- `test_telegram_adapter_media_handlers.py` (§14.3) — add cases:
  - `test_send_photo_retry_after_honoured` (L-21).
  - `test_send_document_file_not_found_logs_and_raises` (L-20).
  - `test_send_audio_network_error_retries_twice_then_raises`.
  - `test_attachment_ingress_dedup_within_update_cycle` (C-6,
    I-7.6) — same `local_path` fed to `_on_photo` twice →
    `emit` called exactly once.

### 3.5 `src/assistant/scheduler/dispatcher.py:216`

```python
# Before:
if joined:
    await self._adapter.send_text(self._owner, joined)
# After:
if joined:
    await dispatch_reply(
        self._adapter, self._owner, joined,
        outbox_root=outbox_dir(self._settings.data_dir),
        dedup=self._dedup_ledger,  # passed in __init__
        log_ctx={"trigger_id": t.trigger_id, "schedule_id": t.schedule_id},
    )
```

`SchedulerDispatcher.__init__` gains a `dedup_ledger: _DedupLedger`
parameter.

### 3.6 `src/assistant/subagent/hooks.py:270` (shielded) — v2 fix-pack (H-11)

```python
# Before:
await asyncio.shield(adapter.send_text(callback_chat_id, body))
# After:
await asyncio.shield(
    dispatch_reply(
        adapter, callback_chat_id, body,
        outbox_root=outbox_dir(settings.data_dir),  # derived inside
        dedup=dedup_ledger,
        log_ctx={"job_id": job_id},
    )
)
```

**v2 fix-pack (H-11):** `make_subagent_hooks` signature gains ONLY
`dedup_ledger: _DedupLedger`. `outbox_root` is NOT added as a
factory parameter — it is derived inside the hook closure via
`outbox_dir(settings.data_dir)` (where `settings` is already in
the factory's closure from phase-6). This keeps the factory
surface minimal and avoids threading the same derived value
through three call-sites. Rationale: `outbox_root` is a pure
function of `settings.data_dir` which the factory already holds
— passing it separately invites the two to drift.

```python
# make_subagent_hooks signature (v2)
def make_subagent_hooks(
    settings: Settings,               # already there from phase-6
    store: SubagentStore,              # already there
    picker: SubagentRequestPicker,    # already there
    adapter: MessengerAdapter,         # already there
    dedup_ledger: _DedupLedger,       # NEW (v2: only param added)
) -> dict[str, list[HookMatcher]]:
    ...
```

Inside `on_subagent_stop`:

```python
from assistant.media.paths import outbox_dir
outbox_root = outbox_dir(settings.data_dir)  # derived, not passed
```

### 3.7 `src/assistant/main.py::Daemon` (integration)

```python
class Daemon:
    def __init__(self, ...) -> None:
        # ...
        self._dedup_ledger = _DedupLedger()  # NEW

    async def start(self) -> None:
        # ...
        await ensure_media_dirs(self._settings.data_dir)  # NEW, before bg tasks

        # ... bridge + picker set up ...
        # Pass _dedup_ledger + outbox_root into hook factories + SchedulerDispatcher

        self._media_sweep_stop = asyncio.Event()
        self._spawn_bg(
            media_sweeper_loop(
                self._settings.data_dir, self._settings, self._media_sweep_stop, log
            ),
            name="media_sweeper_loop",
        )

    async def stop(self) -> None:
        # ... phase-5/6 drain order preserved ...
        self._media_sweep_stop.set()
        # sweep_loop drains naturally from stop_event; bg-drain already in pattern
```

## 4. Test-first order

Tests MUST land alongside their commit where feasible (test-first).

### 4.1 Per-commit test files

| Commit | Test file | Size |
|---|---|---|
| #2 (memlib) | `test_memlib_refactor_regression.py` | ~60 LOC |
| #2b (root pyproject) | no new test — `uv sync` smoke in CI | — |
| #3 (config) | `test_media_settings.py` | 40 LOC |
| #4 (base/attachment) | `test_media_attachment_dataclass.py` | 50 LOC |
| #5 (media/) | `test_media_paths.py`, `test_media_download.py`, `test_media_sweeper.py` | 40+100+120 LOC |
| #6 (dispatch_reply) | `test_dispatch_reply_regex.py` (corpus from S-2), `test_dispatch_reply_classify.py`, `test_dispatch_reply_path_guard.py`, `test_dispatch_reply_integration.py`, `test_dispatch_reply_dedup_ledger.py` (v2: split real-clock/mock-clock per H-12) | 130+80+100+140+110 LOC |
| #7 (transcribe) | `test_tools_transcribe_cli.py`, `test_bash_hook_transcribe_allowlist.py` | 120+30 LOC |
| #8 (genimage) | `test_tools_genimage_cli.py`, `test_bash_hook_genimage_allowlist.py` | 120+30 LOC |
| #9 (extract_doc) | `test_tools_extract_doc_cli.py`, `test_bash_hook_extract_doc_allowlist.py` | 80+30 LOC |
| #10 (render_doc) | `test_tools_render_doc_cli.py`, `test_bash_hook_render_doc_allowlist.py` | 80+50 LOC |
| #11 (hooks) | `test_bash_hook_factory_backward_compat.py` | 40 LOC |
| #12 (telegram) | `test_telegram_adapter_media_handlers.py` (v2: + attachment-dedup + RetryAfter + send-fail cases per C-6/L-20/L-21) | 160 LOC |
| #13 (handler/bridge) | `test_handler_multimodal_envelope.py`, `test_handler_photo_path_tool_fallback.py` (v2 C-4), `test_handler_multimodal_all_photos_fail.py` (v2 H-14), `test_handler_multimodal_real_photo.py` (v2 C-2, RUN_SDK_INT gated), `test_history_replay_photo_turn_ordering.py` (v2 H-10) | 80+60+80+90+70 LOC |
| #14 (sched switch) | `test_scheduler_dispatch_reply_integration.py` | 40 LOC |
| #15 (hook switch) | `test_subagent_hooks_dispatch_reply.py` | 40 LOC |
| #16 (daemon) | `test_daemon_media_integration.py` | 60 LOC |
| #17 (E2E) | (existing tests + smoke cross-system) | ~100 LOC |
| (regression) | `test_task_spawn_media_worker.py` | 20 LOC |

**v2 fix-pack (H-12) — dedup ledger test split:**
`test_dispatch_reply_dedup_ledger.py` contains TWO variants:

```python
# Variant 1 — authoritative, injects mock clock
def test_dedup_ttl_mock_clock():
    ledger = _DedupLedger(ttl_s=10.0)
    # Pass an explicit `now: float` parameter to mark_and_check
    assert ledger.mark_and_check(("p", 1), now=0.0) is False
    assert ledger.mark_and_check(("p", 1), now=5.0) is True   # within TTL
    assert ledger.mark_and_check(("p", 1), now=15.0) is False # past TTL

# Variant 2 — real-clock smoke, xfail-if-flaky
@pytest.mark.xfail(
    reason="real clock dependent; Variant 1 is authoritative",
    strict=False,
)
def test_dedup_ttl_real_clock():
    ledger = _DedupLedger(ttl_s=0.010)
    import time
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is False
    time.sleep(0.001)
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is True
    time.sleep(0.020)
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is False
```

**v2 fix-pack (H-9) — exact `_memlib` test-file inventory:**

Phase-4 imports via `sys.path` shim; current state:

| File | Line | Import / Reference |
|---|---|---|
| `tests/test_memory_lock_probe.py` | 11 | `from _memlib import fts as fts_mod` |
| `tests/test_memory_vault_dir_mode_0o700.py` | 8 | `from _memlib.vault import ensure_vault` |
| `tests/test_memory_atomic_write_fsync.py` | 10 | `from _memlib.vault import atomic_write` |
| `tests/test_memory_frontmatter_roundtrip.py` | 7 | `from _memlib.frontmatter import FrontmatterError, parse_note, serialize_note` |
| `tests/test_tmp_dir_chmods_loose_perms.py` | 8 | `from _memlib.vault import ensure_vault` |
| `tests/test_sanitize_body_fence_awareness.py` | 5 | `from _memlib.frontmatter import sanitize_body` |
| `tests/test_memory_wikilinks_preserved.py` | 7 | `from _memlib.frontmatter import extract_wikilinks` |
| `tests/test_memory_write_body_with_frontmatter_marker_sanitized.py` | 7 | `from _memlib.frontmatter import sanitize_body` |
| `tests/conftest.py` | 14-18, 26-28 | `_INSTALLER_DIR` / `_MEMORY_DIR` `sys.path.insert` shims + comment |

Total: **8 `from _memlib` import rewrites** + **1 conftest.py shim
removal** = **9 test-side files touched** by the memlib refactor
(commit #2). Coder search-and-replace: `from _memlib` →
`from tools.memory._lib` in the 8 test files (fts, vault, frontmatter
modules all live in `tools/memory/_lib/`). The `conftest.py` shims
(`_INSTALLER_DIR`, `_MEMORY_DIR`) get removed entirely; pytest + the
new `tools/__init__.py` make Python-package imports work without
`sys.path` tricks.

**Note on `tools/skill-installer/_lib/`:** already `_lib` in the
current tree (verified 2026-04-17). The refactor only RENAMES the
directory `tools/skill-installer/` → `tools/skill_installer/`
(hyphen → underscore) — the inner `_lib/` is already correctly
named. This simplifies commit #2: no `_memlib` → `_lib` rename
inside `tools/skill-installer/` (it's already done).

### 4.2 Corpus ports

`test_dispatch_reply_regex.py` MUST port the 46-case S-2 corpus verbatim.
Mark the 3 known corner-case failures as `pytest.mark.xfail(reason="...")`
per spike findings §3.

### 4.3 Integration test gates

`test_subagent_e2e.py` and any test that actually talks to the SDK
stays gated by `RUN_SDK_INT=1` (phase-6 pattern, preserved).

## 5. Spike findings cited inline

| Citation | Plan section | Evidence |
|---|---|---|
| "S-0 Q0-3 confirms 5 MB cap reasonable" | §2.11 MediaSettings | all-sizes-pass up to 10 MB padded JPEG; 5 MB safe default |
| "S-0 Q0-2 accepts jpeg/png/webp labels" | §2.7 handler mime_type | all three labels → green replies |
| "S-0 Q0-6 mode-B placeholder works" | §3.3 history.py | model acknowledges "photo earlier" without raw bytes |
| "S-1 loopback-only tightest rule" | §2.7 transcribe CLI | 11/11 classify cases correct |
| "S-2 v3 regex 43/46 corpus" | §2.5 artefacts.py | §3 spike-findings |
| "S-3 Pillow required" | pitfall #1 + render_doc/pyproject.toml | `Requires-Dist: Pillow>=8.3.2` |
| "S-4 lxml/PIL/fontTools 48.75 MB" | detailed-plan §9 prose | measured values |
| "S-5 flock race-free" | §2.8 genimage quota | 10-worker contention → 1 allowed |
| "S-6 File.file_size nullable" | §2.3 download.py | 5/5 media kinds confirmed `int \| None` |
| "S-7 safe partial-attachment" | §3.1 handler edit | 3 scenarios all PASS |

## 6. Open questions — v2 disposition

All v1 questions resolved by devil wave-2 + fix-pack:

1. ~~Real-PNG/WEBP fixture corpus~~ — **CLOSED (C-2).** Real JPEG ≥3 MB
   fixture mandatory; Cyrillic-filename variant optional. PNG/WEBP
   corpus deferred to phase 9 (regex v3 already handles all three
   extensions; the SDK was Q0-2-permissive on label/magic pairs).

2. ~~Pillow pin upper bound~~ — **CLOSED (H-8, L-16).** `Pillow>=10.4,
   <13` pinned at root `pyproject.toml`. `fpdf2>=2.7,<3` prevents
   upstream tightening the Pillow range past 13.

3. ~~Size-capped writer + aiogram chunk_size~~ — **DEFERRED (L-18).**
   64 KB overrun latency accepted. Setting `chunk_size=8192` would
   quadruple syscalls; measurable cost without empirical pressure.
   Revisit if production shows abuse patterns.

4. ~~IPv6 link-local (`fe80::…`)~~ — **CLOSED.** `is_loopback_only`
   docstring explicitly documents `fe80::/10` rejection. Test case
   added.

5. ~~Spike-0 large size realism~~ — **CLOSED (C-2).** Real 3 MB JPEG
   fixture + `test_handler_multimodal_real_photo.py` (RUN_SDK_INT).

New v2 open items (not blocking):

| # | Q | Status |
|---|---|---|
| Q-v2-1 | Pillow CVE monitoring cadence | documented in pitfall #1; operator responsibility |
| Q-v2-2 | Dedup TTL=300 s rationale | deferred (L-19) — measure in phase-8 telemetry, revisit |
| Q-v2-3 | aiogram swallow-exception regression watch | covered by `test_media_download.py` SizeCapExceeded cases + CI re-run on aiogram version bumps |

## 7. Acceptance checklist addendum (vs detailed-plan §20) — v2 fix-pack

Added from spike findings, over and above plan §20:

- [ ] `_ARTEFACT_RE` matches the v3 pattern (S-2 verified). Corpus port
  in `test_dispatch_reply_regex.py`.
- [ ] **(v2 H-8)** `Pillow>=10.4,<13` pinned at root `pyproject.toml`.
  `fpdf2>=2.7,<3` pinned. `description.md` §82 wording corrected.
- [ ] `media/download.py` uses `_SizeCappedWriter`; 4 unit cases (S-6
  A/B/C/D) in `test_media_download.py`.
- [ ] **(v2 C-3)** `_SizeCappedWriter` implements BOTH `write` and
  `flush` (aiogram 3.26 source audit confirmed `flush()` is called
  per chunk in `__download_file_binary_io`). Unit test verifies
  both methods are exercised.
- [ ] `_validate_endpoint` delegates to `_is_loopback_only`, NOT
  `classify_url`. 11-case port from S-1.
- [ ] Handler safe-pseudocode (S-7) in `handlers/message.py` with 3
  scenarios in `test_handler_multimodal_envelope.py`.
- [ ] **(v2 C-4)** `test_handler_photo_path_tool_fallback.py` covers
  the `MEDIA_PHOTO_MODE=path_tool` branch (note-only, no silent drop).
- [ ] **(v2 C-2, REQUIRED)** `tests/fixtures/phase7/real_photo_3mb.jpg`
  (real entropy JPEG, NOT null-padded) + `test_handler_multimodal
  _real_photo.py` (RUN_SDK_INT-gated). Optional Cyrillic-filename
  variant bonus.
- [ ] **(v2 H-14)** `test_handler_multimodal_all_photos_fail.py` —
  3 photos all unreadable → envelope with 3 failure-notes + zero
  `image_blocks`, model-call still invoked.
- [ ] **(v2 C-6, I-7.6)** `test_telegram_adapter_media_handlers.py`
  covers attachment-ingress dedup (same `local_path` emitted twice
  within 60 s → one `IncomingMessage`).
- [ ] **(v2 H-10)** `test_history_replay_photo_turn_ordering.py` —
  placeholder note contains `turn_id`, anchored to its originating
  turn.
- [ ] **(v2 H-12)** `test_dispatch_reply_dedup_ledger.py` has
  mock-clock authoritative variant + real-clock xfail-if-flaky
  variant.
- [ ] **(v2 H-13)** All 4 phase-7 SKILL.md files + `bridge/
  system_prompt.md` contain the "always space after `:`" rule with
  good/bad example. Integration-test assertion present.
- [ ] **(v2 L-20)** `send_photo`/`_document`/`_audio` on
  `FileNotFoundError` / `PermissionError` / `OSError`:
  `log.warning("send_*_read_failed", ...)` + re-raise so
  `dispatch_reply` swallows and continues to text.
- [ ] **(v2 L-21)** `send_photo`/`_document`/`_audio` wrap
  `TelegramRetryAfter` per phase-5 `send_text` pattern. Test case
  present.
- [ ] `genimage` quota file has 4 spike scenarios ported
  (`test_tools_genimage_cli.py`), including R-3 flock contention.
- [ ] Spike 0 evidence references preserved in commit #1 body.
- [ ] **(v2 H-7)** S-8 report committed (`spikes/phase7_s8_report.json`);
  manylinux_2_28 wheels verified for all C-extension deps.
- [ ] `detailed-plan.md §9` prose updated with real 48.75 MB venv delta.
- [ ] `is_loopback_only` docstring explicitly documents IPv6 link-local
  (`fe80::/10`) rejection.
- [ ] **(v2 C-5)** `parallel-split-agent.md §3` Wave 6 template shows
  two parallel sub-agents (6A: commit 12, 6B: commit 13).
- [ ] **(v2 H-15)** `parallel-split-agent.md §4` or §8 contains the
  wave-plan.md invalidation rule: orchestrator must not invoke
  parallel-split before fix-pack v2 merge; stale v1 wave-plan.md
  (if any) is invalidated.
- [ ] **(v2 C-1)** Commit 2b lands BEFORE any tool/media commit;
  `uv sync` smoke-check passes in CI after commit 2b merge.

### Critical Files for Implementation

- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/base.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/telegram.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/adapters/dispatch_reply.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/handlers/message.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/hooks.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/claude.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/bridge/history.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/config.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/main.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/scheduler/dispatcher.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/subagent/hooks.py
- /Users/agent2/Documents/0xone-assistant/src/assistant/media/paths.py (new)
- /Users/agent2/Documents/0xone-assistant/src/assistant/media/download.py (new)
- /Users/agent2/Documents/0xone-assistant/src/assistant/media/sweeper.py (new)
- /Users/agent2/Documents/0xone-assistant/src/assistant/media/artefacts.py (new)
- /Users/agent2/Documents/0xone-assistant/tools/transcribe/main.py (new)
- /Users/agent2/Documents/0xone-assistant/tools/genimage/main.py (new)
- /Users/agent2/Documents/0xone-assistant/tools/extract_doc/main.py (new)
- /Users/agent2/Documents/0xone-assistant/tools/render_doc/main.py (new)
