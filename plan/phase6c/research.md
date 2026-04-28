---
phase: 6c
title: Research — voice / audio / URL transcription via Mac mini Whisper sidecar
date: 2026-04-27
status: research v1 — closes RQ-C1 + RQ1..RQ8 from description.md
inputs:
  - plan/phase6c/description.md (spec v1, devil wave-1 closures)
  - plan/phase4 memory @tool baseline (memory.py + _memory_core.py)
  - midomis-bot/plan/phase-7-voice-transcription/PLAN.md (reference)
---

# Phase 6c — Researcher artifact

This document closes the open RQ list at the bottom of `description.md` (RQ1..RQ8) and adds RQ-C1 — the missing `save_transcript()` Python wrapper devil-1 flagged as the largest spec gap. Each RQ is structured: state-of-the-art (2026) → recommendation for THIS codebase → risks / failure modes → test strategy.

A short **Spec contradictions / extensions** section lives at the bottom for the orchestrator. The coder reads this file directly; no separate implementation skeleton document is produced (the wrapper skeleton lives inline in RQ-C1).

---

## RQ-C1 — `save_transcript()` Python wrapper API (TOP PRIORITY)

### Problem statement

Spec §"Long-form handling" + §"Architecture" both reference `memory_save_note` / `memory_store.store_note()` as if it were a callable Python function. Phase 4 (`tools_sdk/memory.py`) shipped 6 `@tool`-decorated MCP handlers — these are async coroutines designed to be invoked by the **Claude model** through the SDK's tool dispatch. They are NOT callable from a Python handler without going through the bridge → SDK → MCP server round-trip, which:

1. Adds 2-5s of latency (model decision + tool round-trip) for what should be a sub-100 ms file write.
2. Couples a deterministic side-effect ("save the transcript NOW") to a stochastic decision (the model might decide not to call the tool, or may rewrite the body).
3. Costs Anthropic API tokens for what is purely owner-content persistence.
4. Loses the chain of custody — the transcript that ends up in the vault may differ from the one returned by Whisper.

**The fix is a thin direct-callable wrapper around `_memory_core.write_note_tx`** that preserves every phase-4 invariant (path validation, body sanitisation, FTS5 index sync, atomic write, vault lock) but bypasses the `@tool` decorator + `_CTX` configure-step. This is a one-shot `save_transcript()` function that the audio handler calls in-process.

This is the same pattern phase 6a uses for extraction (handler calls `extract_pdf` / `extract_docx` directly without going through Claude) — voice transcripts are owner content, not model output, so the trust boundary is identical to a Telegram-uploaded PDF.

### Public API

New module: **`src/assistant/memory/store.py`** (~110 LOC including comments + invariant docstring).

```python
"""Phase 6c: direct-callable transcript wrapper around the phase-4
memory subsystem.

Audio handlers call ``save_transcript()`` to persist a Whisper output
as a vault note WITHOUT routing through the Claude SDK. This keeps the
transcript a deterministic side-effect of receiving audio, rather than
a stochastic decision the model might skip.

Invariants preserved (see ``tools_sdk/memory.py::memory_write``):
- ``created`` stamped server-side at first write; never owner-controlled.
- ``updated`` always now-time on every write.
- ``area`` validated as a top-level path segment matching ``_AREA_RE``.
- ``body`` sanitised via ``core.sanitize_body`` BUT — per spec H7 — a
  transcript matching the sentinel pattern is REPLACED, not rejected.
  Owner content cannot be lost.
- ``frontmatter`` serialised via ``core.serialize_frontmatter``.
- ``tags_json`` populated from the ``tags`` list.
- ``write_note_tx`` holds the same blocking ``vault_lock`` as the
  @tool layer — concurrent voice-saves serialise.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import structlog
import yaml

from assistant.tools_sdk import _memory_core as core

log = structlog.get_logger(__name__)

# Mirrors the phase-4 area regex — keep them in lock-step.
_AREA_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# Defence-in-depth body cap dedicated to transcripts (separate from the
# env-driven ``MEMORY_MAX_BODY_BYTES`` cap that protects the @tool
# write path). 200 KiB ~= 30 000 RU words ~= 2-3 hours of transcript.
TRANSCRIPT_MAX_BODY_BYTES = 200_000


class TranscriptSaveError(RuntimeError):
    """Raised on irrecoverable save failures (validation, IO, lock).

    The handler maps this to the spec's Russian transcript-save-failed
    reply. ``save_transcript`` NEVER raises for a sentinel hit — those
    are stripped + logged + retried.
    """


async def save_transcript(
    *,
    vault_dir: Path,
    index_db_path: Path,
    area: str,
    title: str,
    body: str,
    tags: list[str],
    source: str,                     # "voice" | "audio" | "url"
    duration_sec: int | None,
    language: str = "ru",
    extra_frontmatter: dict[str, str] | None = None,
    max_body_bytes: int = TRANSCRIPT_MAX_BODY_BYTES,
) -> Path:
    """Persist a transcript to ``vault_dir/<area>/<title>.md``.

    Returns the absolute path written (caller derives a vault-relative
    string for marker rendering via ``.relative_to(vault_dir)``).

    Raises :class:`TranscriptSaveError` only on validation / IO / lock
    failures — sentinel hits are silently scrubbed (see body).
    """
    import asyncio

    # -- 1. Argument validation --------------------------------------
    if not _AREA_RE.match(area):
        raise TranscriptSaveError(f"invalid area name {area!r}")
    if not isinstance(title, str) or not title.strip():
        raise TranscriptSaveError("title must be non-empty")
    if source not in {"voice", "audio", "url"}:
        raise TranscriptSaveError(f"invalid source {source!r}")
    if not isinstance(body, str):
        raise TranscriptSaveError("body must be a string")

    # -- 2. Path derivation + auto-mkdir (devil M9) ------------------
    rel = Path(area) / f"{title}.md"
    try:
        full = core.validate_path(str(rel), vault_dir)
    except ValueError as exc:
        raise TranscriptSaveError(f"path: {exc}") from exc
    full.parent.mkdir(parents=True, exist_ok=True)

    # -- 3. Body sanitise (H7: replace sentinel, never reject) -------
    # First strip any literal sentinel tokens — owner content trumps
    # the write-time reject. Replacement is `[redacted-tag]` per spec.
    scrubbed = core._SENTINEL_RE.sub("[redacted-tag]", body)
    if scrubbed != body:
        log.warning(
            "save_transcript_sentinel_scrubbed",
            area=area, title=title, source=source,
        )
    try:
        clean_body = core.sanitize_body(scrubbed, max_body_bytes)
    except ValueError as exc:
        # Bare-`---` line or oversize body — rare for transcripts but
        # not impossible (a Whisper hallucination of an Obsidian
        # frontmatter delimiter). Indent the offending line and retry
        # ONCE; if still bad, surface the error to the caller.
        if "bare '---'" in str(exc):
            indented = re.sub(r"(?m)^---$", r"  ---", scrubbed)
            try:
                clean_body = core.sanitize_body(indented, max_body_bytes)
            except ValueError as exc2:
                raise TranscriptSaveError(f"sanitize: {exc2}") from exc2
        else:
            raise TranscriptSaveError(f"sanitize: {exc}") from exc

    # -- 4. Frontmatter assembly --------------------------------------
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    fm: dict[str, object] = {
        "title": title,
        "tags": tags,
        "area": area,
        "created": now_iso,                   # NEVER owner-controlled
        "updated": now_iso,
        "source": source,
        "lang": language,
    }
    if duration_sec is not None:
        fm["duration_sec"] = int(duration_sec)
        fm["duration_human"] = _fmt_duration(int(duration_sec))
    for k, v in (extra_frontmatter or {}).items():
        fm.setdefault(k, str(v))

    try:
        content = core.serialize_frontmatter(fm, clean_body)
    except yaml.YAMLError as exc:
        raise TranscriptSaveError(f"frontmatter: {exc}") from exc

    # -- 5. Index row + atomic write under vault lock ----------------
    tags_json = json.dumps(tags, ensure_ascii=False)
    row = (str(rel), title, tags_json, area, clean_body, now_iso, now_iso)
    try:
        await asyncio.to_thread(
            core.write_note_tx,
            full, rel, row, content, vault_dir, index_db_path,
        )
    except TimeoutError as exc:
        raise TranscriptSaveError("vault lock contention") from exc
    except sqlite3.OperationalError as exc:
        raise TranscriptSaveError(f"index: {exc}") from exc
    except OSError as exc:
        raise TranscriptSaveError(f"vault io: {exc}") from exc

    log.info(
        "save_transcript_ok",
        area=area, title=title, source=source,
        duration_sec=duration_sec, body_bytes=len(clean_body),
        path=str(rel),
    )
    return full


def _fmt_duration(sec: int) -> str:
    """``312 -> "5m12s"``, ``45 -> "45s"``, ``3672 -> "1h1m12s"``."""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
```

### Where to instantiate

`save_transcript` is **stateless** — it takes `vault_dir` + `index_db_path` as args every call. NO module-level `_CTX` dict, NO `configure_*` step. This is intentional: the function is a thin adapter, not a subsystem. The handler reads the two paths from `Settings` (already exposed as `settings.vault_dir` + `settings.memory_index_path` properties) and passes them in directly.

This avoids the test-isolation pain phase 4 hit (`reset_memory_for_tests()` exists because the module-level `_CTX` leaks between tests). Each test in `test_save_transcript.py` constructs its own `tmp_path` vault and passes it positionally.

### Path derivation + slugification

Spec calls for `vault/<area>/transcript-<YYYY-MM-DD-HHMM>-<source>.md`. The `<area>` part comes from the user's caption: caption "проект альфа" → area `proekt_alfa`. Russian transliteration is needed.

**Recommendation**: hand-roll a 30-line slugifier rather than pulling a new dep. The existing codebase has zero non-pure-python deps for memory; adding `transliterate` or `cyrtranslit` for one call site is heavy. Hand-roll uses a static dict mapping each Cyrillic letter to ASCII (Russian schoolbook table — 33 letters, 7 diphthong-like multi-char outputs for ж/ч/ш/щ/ю/я).

```python
_RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}

def slugify_area(caption: str | None) -> str:
    """``"проект альфа" -> "proekt_alfa"``; empty/invalid -> ``"inbox"``."""
    if not caption:
        return "inbox"
    out: list[str] = []
    for ch in caption.strip().lower():
        if ch.isascii() and (ch.isalnum() or ch in {"_", "-"}):
            out.append(ch)
        elif ch in _RU_TO_LAT:
            out.append(_RU_TO_LAT[ch])
        elif ch.isspace():
            out.append("_")
        # any other char (punctuation, emoji) silently dropped
    slug = "".join(out).strip("_-")
    if not slug or not _AREA_RE.match(slug):
        return "inbox"
    return slug[:32]  # match _AREA_RE 33-char cap (1 + 32)
```

This lives in the same `assistant/memory/store.py` module — caller (handler) does:
```python
area = slugify_area(caption)
title = f"transcript-{ts:%Y-%m-%d-%H%M}-{source}"
path = await save_transcript(...)
```

### Concurrency

`write_note_tx` already takes the blocking `vault_lock` (5s timeout). Two concurrent voice transcribes for the same chat would queue (5s should easily cover one save). Two concurrent for **different** areas serialise too — the lock is a vault-wide flock, not per-area. This matches phase-4 design: a single-user vault with one writer at a time. NO change needed.

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| Whisper hallucinates a sentinel-shape tag in a transcript and `sanitize_body` rejects | H7 closure: replace with `[redacted-tag]` BEFORE calling sanitize_body. Test: feed a deliberate sentinel string. |
| Whisper output contains a bare `---` line (rare but possible — TV show title, dialogue marker) | Indent the line (`  ---`) and retry sanitize once. If still bad, raise. Test: feed `text\n---\nmore` body. |
| Concurrent voice saves for same chat | flock serialises; both succeed. Test: spawn two `asyncio.gather` calls. |
| Path traversal via crafted caption | slugify drops every non-`[a-z0-9_-]` char; `validate_path` blocks `..` and absolute paths defensively. Test: caption `"../../etc/passwd"` → falls back to `inbox`. |
| Very long transcript (3 h speech ~= 90 KiB text) | TRANSCRIPT_MAX_BODY_BYTES=200KiB easily covers; if breached, raise. |
| Title collision (two transcripts in the same minute) | Title includes `<source>` suffix and minute precision; collision requires identical source within 60s — falls back to `INSERT OR REPLACE` so the older one is overwritten. Owner gets a warning log. Acceptable for v0; phase 6e could add msg_id suffix. |
| File auto-create of `<area>/` dir creates a phantom area | acceptable; spec H5 explicitly allows owner to ad-hoc create areas via caption. |

### Test strategy (8 tests in `tests/test_save_transcript.py`)

1. `test_save_transcript_basic` — happy path: voice, 30s, default tags. Verify file exists, frontmatter contains `created`/`updated`/`source`/`lang`/`duration_sec`/`duration_human`. Verify `INSERT INTO notes` row was written by querying the index DB.
2. `test_save_transcript_invariant_created_now` — verify `created` is NOW even if caller passes `created` in `extra_frontmatter` (it should be silently ignored).
3. `test_save_transcript_sentinel_replace` — body contains a sentinel-shape token → save succeeds, body contains `[redacted-tag]`, log warning emitted.
4. `test_save_transcript_bare_dash_indent_retry` — body contains a line `---` → save succeeds with `  ---` indented.
5. `test_save_transcript_auto_mkdir_area` — `area=newproj` not yet in vault → save creates `vault/newproj/` dir.
6. `test_save_transcript_slugify_russian` — `slugify_area("проект альфа") == "proekt_alfa"`. Six caption inputs covering apostrophes, emoji, multi-space, leading punctuation, non-Russian Cyrillic (Ukrainian "ї"), pure ASCII passthrough.
7. `test_save_transcript_concurrent_serialises` — `asyncio.gather(save_transcript(area="a"), save_transcript(area="b"))` both complete; index DB has both rows.
8. `test_save_transcript_invalid_area_raises` — `area="UPPERCASE"` raises `TranscriptSaveError`.

Bonus (optional): `test_save_transcript_idempotent_on_overwrite` — two saves with same path → second `INSERT OR REPLACE`s the first; `updated` advances.

---

## RQ1 — mlx-whisper version + model + macOS Sequoia compat (2026)

### State-of-the-art (April 2026)

- **mlx-whisper** is the canonical Apple MLX port of OpenAI Whisper, maintained in the `ml-explore/mlx-examples` repo. PyPI publishes from there; latest release as of April 2026 sits in the 0.4.x line. Ecosystem packages (e.g. `wyoming-mlx-whisper` 1.4.0 from January 2026) wrap it actively, indicating ongoing maintenance.
- **Model**: `mlx-community/whisper-large-v3-turbo` is the de-facto choice for turbo-speed real-time-or-better Russian transcription on Apple Silicon. 809 M params, **~1.6 GB on disk**, **~6 GB RAM at inference** (large-v3 needs ~10 GB). Roughly **4× realtime on M1**; on M4 the same model benchmarks at **~8× realtime** for sustained transcription.
- **Russian recall**: turbo loses ~1-2 percentage points WER vs full large-v3. Both are state-of-the-art for Russian; the gap is below noise for owner UX (transcripts are summarised by Claude downstream anyway).
- **macOS Sequoia (15.x)**: MLX requires Metal 3 (macOS 13.5+). Sequoia is fully supported. M4 hardware specifically benefits — the unified memory + new Neural Engine path means the model loads once and stays resident.
- **Python compat**: mlx-whisper 0.4.x supports Python 3.10–3.13. By April 2026 Homebrew default may be 3.13; `python@3.12` is still in `brew` and is the safer pin (mlx itself ships 3.13 wheels but yt-dlp + a few transitives have edge cases on 3.13). Recommend **pin Python 3.12** for the Mac sidecar.

### Recommendation for THIS codebase

Pin in `whisper-server/requirements.txt`:
```
fastapi>=0.135,<0.140
uvicorn[standard]>=0.30,<0.40
mlx-whisper>=0.4,<0.5
yt-dlp>=2026.4.0
python-multipart>=0.0.20,<0.1
pydantic-settings>=2.6,<3
httpx>=0.27,<1
```

Setup script (`whisper-server/setup-mac-sidecar.sh`) installs Python 3.12 explicitly:
```bash
brew install python@3.12 ffmpeg
python3.12 -m venv ~/whisper-server/.venv
source ~/whisper-server/.venv/bin/activate
pip install -r ~/whisper-server/requirements.txt
```

**Cold-start mitigation** (CRITICAL for AC#1's 30 s end-to-end target — first call after launchd boot would otherwise eat 4-7 s on model load):

```python
# whisper-server/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
import asyncio
import mlx_whisper

# Module-level globals, populated at startup by lifespan.
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
_MODEL_LOADED = False


async def _prewarm() -> None:
    """Run a 0.5 s silence sample through the model so weights land in
    unified memory before the first real request. Without this the
    first POST /transcribe pays 4-7 s on M4."""
    global _MODEL_LOADED
    import numpy as np
    silence = np.zeros(8000, dtype=np.float32)  # 0.5 s @ 16 kHz
    await asyncio.to_thread(
        mlx_whisper.transcribe,
        silence,
        path_or_hf_repo=MODEL_REPO,
        language="ru",
    )
    _MODEL_LOADED = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _prewarm()
    yield
    # No teardown needed — MLX releases on process exit.


app = FastAPI(lifespan=lifespan)
```

mlx-whisper has no separate `load_model()` API in 0.4.x — the path is "first call to `transcribe(path_or_hf_repo=...)` warms the cache; subsequent calls reuse it via mlx's internal weights cache". The 0.5 s silence input is the cheapest known warmup. Confirmed pattern from `WhisperLiveKit` and `mlx-openai-server`.

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| HuggingFace rate-limit on first model download (~1.6 GB) | `setup-mac-sidecar.sh` runs `mlx_whisper.transcribe(<silence>)` ONCE during install to prepopulate `~/.cache/huggingface/`. Owner sees download progress synchronously. |
| Sequoia 15.4+ "MetalRT" feature changes break MLX | Pinned mlx-whisper version + Renovate auto-update minor/patch with manual review on major. CI nightly health-probe (out of scope for v0; defer phase 6e). |
| OOM on M4 8 GB Mac (entry-level mini) | M4 16 GB minimum recommended. Document in `whisper-server/README.md`. Owner Mac mini is not entry-level (this is a known constraint). |
| Cold-start race: FastAPI accepts request before lifespan finishes | `lifespan` blocks `yield` until `_prewarm` returns; uvicorn won't open the listening socket until `yield`. No race. |
| Model repo URL changes | `mlx-community/whisper-large-v3-turbo` has been stable since 2024-09; if Apple flips the canonical repo, swap MODEL_REPO env. |

### Test strategy

- `whisper-server/tests/test_health.py` — `GET /health` after lifespan returns `{"status": "ok", "model_loaded": true}`. Run with `httpx.AsyncClient(transport=ASGITransport(app=app))`.
- `whisper-server/tests/test_transcribe_smoke.py` — feed a 5 s pre-recorded RU sample (committed to `whisper-server/tests/fixtures/sample-ru-5s.ogg`); assert response includes `text` field and `duration ≈ 5.0 ± 0.5`.
- Owner smoke: AC#1's 30 s budget split: 1 s download + 1-2 s transcribe + 5-15 s Claude turn = headroom.

---

## RQ2 — Tailscale on macOS + ACL JSON

### State-of-the-art (April 2026)

- **Tailscale ACLs use HuJSON** (`*.hujson`) — JSON with comments + trailing commas. Admin console accepts both JSON and HuJSON; ship the snippet as HuJSON in `whisper-server/README.md` for clarity, but `tailscale set --acls=file.json` accepts plain JSON too.
- **Tag ownership**: the `tagOwners` block declares which user/group can stamp a node with a given tag. For a single-user tailnet, owner is `autogroup:admin`.
- **Auto-approvers**: not required for our use case (we don't advertise routes/exit nodes/services; just plain MagicDNS).
- **Containers and tags**: a Docker container running the official `tailscale/tailscale` image joins the tailnet via `TS_AUTH_KEY` (or OAuth client) and advertises tags via `TS_EXTRA_ARGS=--advertise-tags=tag:bot-vps`.
- **Mac sidecar**: brew cask `tailscale-app` (recommended for scriptability + plain `tailscale up --advertise-tags=tag:whisper-mac` CLI) versus Mac App Store version (no CLI binary outside the app sandbox). **Use brew**.
- **2026 security note**: containers on a host running Tailscale on the host's network namespace can reach the tailnet through iptables side-channels. Mitigation: run the Tailscale container in a separate userspace network namespace (already the default `tailscale/tailscale` Docker pattern) — phase 5d compose stack should NOT bind `network_mode: host` to the bot.

### Recommendation for THIS codebase

**Tailscale node topology**:
- VPS bot daemon: tag `tag:bot-vps`. Joins via dedicated `tailscale/tailscale` sidecar container in the `deploy/docker/docker-compose.yml`. Bot container uses `network_mode: "service:tailscale"` (or `network_mode: "container:tailscale"`) to share the sidecar's netns.
- Mac mini whisper: tag `tag:whisper-mac`. Joins via brew cask, started by `setup-mac-sidecar.sh`.

**ACL JSON snippet** (`whisper-server/README.md` § "Tailscale setup"):

```hujson
{
  "tagOwners": {
    "tag:bot-vps":     ["autogroup:admin"],
    "tag:whisper-mac": ["autogroup:admin"],
  },

  // Default-deny posture: only the rules below match.
  "acls": [
    // Bot VPS may reach the whisper-mac on port 9000 only.
    {
      "action": "accept",
      "src":    ["tag:bot-vps"],
      "dst":    ["tag:whisper-mac:9000"],
    },

    // Owner laptop (untagged, owned by your Tailscale user) can SSH
    // to either tag for ops. Drop this rule if you SSH via direct
    // public IP / OAuth.
    {
      "action": "accept",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:bot-vps:22", "tag:whisper-mac:22"],
    },
  ],

  "ssh": [
    {
      "action": "check",
      "src":    ["autogroup:admin"],
      "dst":    ["tag:bot-vps", "tag:whisper-mac"],
      "users":  ["autogroup:nonroot", "root"],
    },
  ],
}
```

**Bot side — `deploy/docker/docker-compose.yml` extension**:
```yaml
  tailscale:
    image: tailscale/tailscale:latest
    container_name: tailscale-bot
    hostname: bot-vps
    environment:
      TS_AUTHKEY: ${TS_AUTHKEY}
      TS_EXTRA_ARGS: --advertise-tags=tag:bot-vps
      TS_STATE_DIR: /var/lib/tailscale
      TS_USERSPACE: "false"
    volumes:
      - tailscale-state:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add:
      - NET_ADMIN
      - NET_RAW
    restart: unless-stopped

  bot:
    # ...existing image: ghcr.io/c0manch3/0xone-assistant:<TAG>
    network_mode: "service:tailscale"
    depends_on:
      - tailscale

volumes:
  tailscale-state:
```

`TS_AUTHKEY` is generated as a **reusable, ephemeral=false, preauthorized=true** key in the Tailscale admin console with `tag:bot-vps` pre-attached. Stored in `~/.config/0xone-assistant/secrets.env` on VPS (mode 600), not committed.

**Mac side — `whisper-server/setup-mac-sidecar.sh` Tailscale block**:
```bash
# Tailscale (skip if already up)
if ! command -v tailscale >/dev/null 2>&1; then
  brew install --cask tailscale-app
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Starting Tailscale; opening browser for auth"
  sudo tailscale up --advertise-tags=tag:whisper-mac --ssh
fi
TS_HOSTNAME=$(tailscale status --json | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
echo "MagicDNS hostname: ${TS_HOSTNAME}"
echo "Set WHISPER_API_URL=http://${TS_HOSTNAME}:9000 in your bot .env"
```

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| TS_AUTHKEY leak via `docker inspect` | env file mode 600; do not echo. Use `--env-file` not inline `environment:` for the key. |
| `tag:bot-vps` not approved in admin console (default-deny on first connect) | `setup-mac-sidecar.sh` echoes both magic DNS + a "first connect needs admin approval" hint. |
| Tailscale CGNAT issue (4via6) | known stable in 2026; n/a. |
| MagicDNS uses HTTPS via Tailscale's TLS cert provisioning — port 9000 over plain HTTP is allowed within tailnet | spec uses `http://`; bearer token still required (defense-in-depth). |
| Mac sidecar IP change on tailnet | MagicDNS hostname is stable across reconnects; pin by name not IP. |
| Owner runs `tailscale logout` on Mac → bot health-check fails | spec already handles: `transcription temporarily unavailable`. |

### Test strategy

- Manual owner smoke: from VPS container, `curl -H "Authorization: Bearer $WHISPER_API_TOKEN" http://whisper-mac:9000/health` returns 200.
- From Mac, `nc -zv bot-vps 9000` should FAIL (default-deny + no rule for whisper→bot). Confirms ACL is correctly bidirectional-deny.
- From a third unrelated Tailscale node (owner laptop with no tag), `curl http://whisper-mac:9000/health` should TCP-RST (default-deny).

---

## RQ3 — yt-dlp anti-bot 2026 + safe invocation

### State-of-the-art (April 2026)

- **yt-dlp release cadence**: typically 2-4 patch versions per month. Pin to a floor (`>=2026.4.0`) and let the daily auto-update plist track latest.
- **PoToken** (Proof-of-Origin Token) is YouTube's anti-bot mechanism. As of 2026 YouTube binds tokens to **video ID**, so manual extraction is no longer viable; the recommended path is a **PoToken provider plugin**:
  - `bgutil-ytdlp-pot-provider` — a separate Node.js process or HTTP server that yt-dlp calls per video.
  - For owner-Mac use, the simpler path is `--cookies-from-browser` against an authenticated browser (Safari/Chrome) profile owned by the same OS user. This works for podcasts / music videos / lectures the owner is logged into; risk of "bot detection" is much lower for residential IP + real cookies.
- **`--cookies-from-browser` on macOS**: Safari is usable; Chrome locks the cookie DB while running. Recommend **Firefox** or have the owner close Chrome before running yt-dlp. Or skip cookies entirely for non-YouTube URLs (90% of podcast / Spotify / SoundCloud / Vimeo URLs don't need them).
- **Format selector** for audio-only with size cap: `bestaudio[filesize<500M]/bestaudio[abr<=128]/best`. The `[filesize<N]` filter only works when yt-dlp's pre-flight resolves a `filesize` (always for YouTube; sometimes None for other extractors). Adding `--max-filesize 500M` belt-and-suspenders aborts the download if a chunk exceeds 500 MB regardless of pre-flight.
- **Python API vs CLI subprocess**: Python API (`yt_dlp.YoutubeDL`) is more controllable but pins your code to specific yt-dlp internals. Daily auto-update + Python API is risky — internal API breaks land routinely. **Recommend CLI subprocess** with hardened invocation. Same approach midomis-bot uses.
- **Subprocess safety**: shell=False, args list, no shell metacharacter expansion. Pass URL as a list element. CVE-2023-40581 (yt-dlp) was a `--exec` shell-substitution vulnerability — we never use `--exec`, so we're not affected, but document.

### Recommendation for THIS codebase

**yt-dlp invocation** (`whisper-server/main.py`):

```python
import asyncio
import shutil
from pathlib import Path

YT_DLP_BIN = shutil.which("yt-dlp") or "yt-dlp"  # resolved at startup

async def extract_audio(url: str, work_dir: Path, timeout: int = 600) -> Path:
    """Download URL audio to work_dir/audio.<ext>. Raise on timeout
    or non-zero exit. Returns absolute path."""
    out_template = str(work_dir / "audio.%(ext)s")
    args = [
        YT_DLP_BIN,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--restrict-filenames",
        "--no-live-from-start",
        "--max-filesize", "500M",
        "-f", "bestaudio[filesize<500M]/bestaudio[abr<=128]/bestaudio/best",
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "-o", out_template,
        "--socket-timeout", "30",
        "--retries", "2",
        # NEVER pass --exec; CVE-2023-40581 lessons.
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise YtDlpTimeoutError(f"yt-dlp timed out after {timeout}s")
    if proc.returncode != 0:
        raise YtDlpExtractError(stderr.decode()[:1000])
    candidates = list(work_dir.glob("audio.*"))
    if not candidates:
        raise YtDlpExtractError("no audio file produced")
    return candidates[0]
```

Note: **no `--cookies-from-browser` in v0**. Owner MAY add it manually post-shipping if specific YouTube videos hit the bot wall; documented in README. Keeping the default invocation cookie-less avoids the "Chrome locked DB" pitfall and the privacy concern of yt-dlp reading every cookie in the profile.

**Pre-check disk space** (devil H4):
```python
import shutil
free = shutil.disk_usage(work_dir).free
if free < 2 * 1024 * 1024 * 1024:  # 2 GB
    raise YtDlpDiskFullError("less than 2 GB free; refusing extract")
```

**Auto-update plist** — single launchd job (simpler than two):
```xml
<!-- ~/Library/LaunchAgents/com.zeroxone.yt-dlp-update.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.zeroxone.yt-dlp-update</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/USER/whisper-server/.venv/bin/pip</string>
        <string>install</string>
        <string>-U</string>
        <string>yt-dlp</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>4</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key>
    <string>/Users/USER/whisper-server/logs/yt-dlp-update.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/USER/whisper-server/logs/yt-dlp-update.err</string>
</dict>
</plist>
```

Loaded with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.zeroxone.yt-dlp-update.plist`.

**Version-floor check at FastAPI startup** (devil H8):
```python
async def _check_yt_dlp() -> None:
    proc = await asyncio.create_subprocess_exec(
        YT_DLP_BIN, "--version",
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    version = out.decode().strip()       # "2026.04.01"
    minimum = "2026.04.01"
    if version < minimum:                # lexicographic OK for YYYY.MM.DD
        log.warning("yt_dlp_version_below_floor", version=version, min=minimum)
```

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| YouTube returns 403 / "Sign in to confirm" | Map yt-dlp stderr containing "confirm you" / "bot" / "captcha" → 422 with Russian "YouTube требует логин — пришли мне аудио файлом" reply. Owner falls back to direct upload. |
| yt-dlp daily update breaks (signature rotation) | Version-floor check at startup logs warning but does not block boot. Renovate-style discipline by owner. |
| `--restrict-filenames` produces ambiguous output path | use `glob("audio.*")` and assert exactly one match. |
| Transcoded mp3 uses LAME default (q=5 ~ 130 kbps VBR) | spec calls for ffmpeg invocation in next step; mp3 → wav 16k mono before Whisper. Transcoding twice is cheap (~1 s for 1 h audio) and guarantees Whisper input shape. |
| CVE-2023-40581-style shell injection | Never use `--exec`, never use shell=True, args list. Done. |
| Disk full mid-download | `--max-filesize 500M` aborts. `shutil.disk_usage` pre-check catches the pre-existing-low case. |
| Live streams (`/live/`) | yt-dlp can record them, length unknown — `--no-live-from-start` drops them entirely. |

### Test strategy

- `whisper-server/tests/test_yt_dlp.py` — mock the async subprocess factory to return a known mp3 path; verify args list contains all hardening flags.
- Owner smoke: short YouTube clip (Apple keynote 30 s) → extract + transcribe + summary.
- Owner smoke: invalid URL → 422 + Russian error reply.

---

## RQ4 — FastAPI async wrapper for sync mlx-whisper

### State-of-the-art (April 2026)

- `mlx_whisper.transcribe()` is **synchronous** (still as of 0.4.x). No async API.
- MLX kernels are C++/Metal and **release the Python GIL** during compute — `asyncio.to_thread` works correctly to keep the event loop unblocked.
- FastAPI's default starlette ThreadPoolExecutor is sized at 40 threads. Single-user → no contention; one whisper at a time saturates the GPU anyway.
- Pre-warm pattern: load via lifespan (RQ1 above). The first call after warmup costs ~the audio duration in compute.

### Recommendation

```python
# whisper-server/main.py — transcribe handler
from fastapi import FastAPI, UploadFile, HTTPException, Depends
import asyncio
import tempfile
from pathlib import Path

@app.post("/transcribe")
async def transcribe(
    file: UploadFile,
    language: str = "ru",
    _: None = Depends(verify_token),
) -> dict:
    if file.size and file.size > 100 * 1024 * 1024:        # 100 MB cap
        raise HTTPException(413, "file too large")
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        in_path = td / (file.filename or "audio.bin")
        with in_path.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        wav_path = td / "audio.wav"
        await _convert_to_wav(in_path, wav_path)
        try:
            result = await asyncio.to_thread(
                mlx_whisper.transcribe,
                str(wav_path),
                path_or_hf_repo=MODEL_REPO,
                language=language,
                word_timestamps=False,
                fp16=True,
            )
        except Exception as exc:
            log.exception("transcribe_failed", error=repr(exc))
            raise HTTPException(500, "transcription failed") from exc
    return {
        "text": result["text"].strip(),
        "language": result.get("language", language),
        "duration": result.get("duration", 0.0),
        "segments": result.get("segments", []),
    }


async def _convert_to_wav(src: Path, dst: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(dst),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(415, f"ffmpeg: {stderr.decode()[:500]}")
```

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| Two concurrent transcribes from the bot | bot side has per-chat lock + single-user discipline; the only way to overlap is bot retry while previous still in flight. ThreadPool absorbs; second blocks on first GPU. |
| Very short audio (<1 s) | mlx-whisper handles fine; produces empty text or "Спасибо за просмотр." (training-data leak hallucination). Add post-filter: if `duration < 1.0` and `len(text.strip()) > 50` → return text but flag in log. |
| ffmpeg exits non-zero on broken upload | 415 with stderr tail (capped 500 chars). Bot maps to Russian "не смог обработать аудио, попробуй другой формат". |
| 30 s `socket-timeout` triggers spuriously on slow podcasts | yt-dlp socket timeout is per-chunk; raise to 60 s if owner reports issues. |

### Test strategy

- `test_transcribe_smoke` — POST a 5 s ru sample fixture, assert `text` non-empty, `duration` ≈ 5.
- `test_transcribe_corrupt_audio` — POST a 1 KB random binary, expect 415.
- `test_transcribe_oversize` — POST 101 MB fake file, expect 413 BEFORE ffmpeg runs.

---

## RQ5 — Telegram audio download size + format invariants

### State-of-the-art (April 2026)

- **Bot API `getFile` cap is still 20 MB** (April 2026). This is per-bot regardless of premium status.
- `sendVoice`/`sendAudio` upload cap is 50 MB; that's the OUT direction (bot → user) — irrelevant for our INPUT-only path.
- aiogram 3.27: `Message.voice.file_size`, `Message.audio.file_size`, `Message.document.file_size` are all `Optional[int]`. **Always treat as `(file_size or 0)`** — Telegram occasionally omits it for forwards.
- `Message.audio.mime_type` distribution (real-world):
  - iPhone Voice Memos shared as audio: `audio/mp4` or `audio/x-m4a`, file_name typically `Memo {NN}.m4a` (sometimes None).
  - Telegram native voice (`F.voice`): always `audio/ogg`, no file_name; synthetic name from `file_unique_id`.
  - Android Recorder: `audio/mp4` or `audio/3gpp`, file_name varies.
  - Desktop / forwarded mp3: `audio/mpeg`, file_name preserved.
- `_on_document` route for "send as file" m4a: file_name is preserved; mime_type may be `application/octet-stream` (Telegram doesn't sniff for documents).

### Recommendation

**Pre-download size guard** (mirrors phase 6a):
```python
# in _on_voice handler
voice = message.voice
if voice is None:
    return
file_size = voice.file_size or 0
if file_size > TELEGRAM_DOC_MAX_BYTES:  # 20 MB
    await message.reply("аудио файл больше 20 МБ — Telegram не отдаёт его боту")
    return
duration = voice.duration or 0
mime = "audio/ogg"          # F.voice is ALWAYS ogg/opus
```

**Kind detection priority** (devil H3 — suffix as PRIMARY):
```python
def _detect_audio_kind(*, attachment_filename: str | None, mime_type: str | None) -> str:
    """Return "ogg"|"mp3"|"m4a"|"wav"|"opus"; raise on unknown.

    Suffix-of-filename wins over mime; mime is fallback. Fixes
    iPhone-Voice-Memo route where mime is sometimes ``application/octet-stream``.
    """
    if attachment_filename:
        suf = Path(attachment_filename).suffix.lower().lstrip(".")
        if suf in AUDIO_KINDS:
            return suf
    if mime_type:
        m = mime_type.lower()
        if "ogg" in m or "opus" in m: return "ogg"
        if "mp4" in m or "m4a" in m: return "m4a"
        if "mpeg" in m or "mp3" in m: return "mp3"
        if "wav" in m: return "wav"
    raise ValueError(f"cannot detect audio kind: filename={attachment_filename!r} mime={mime_type!r}")
```

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| 20 MB voice = ~30 min OGG/Opus / ~10 min MP3 — owner records longer | Reject pre-download (devil M11). User error mode is the most likely AC#6 trigger anyway (3 h cap is server-side via duration probe, but 20 MB cap fires earlier). |
| `file_size` missing on forward | `(file_size or 0) > 20MB` branch is NEVER hit (0 < 20MB), so `bot.download` runs and Telegram returns "file too big" → catch `TelegramBadRequest` with "file is too big" substring → user-friendly reply. Same as phase 6a. |
| `audio/x-m4a` + missing file_name + missing mime | Force `.m4a` fallback (most likely iPhone). Log warning. ffmpeg will tell us if we're wrong. |
| 3GPP audio | Map to `m4a` for download path; ffmpeg handles it. |

### Test strategy

- `test_voice_size_guard_rejects_oversize` — file_size = 25 MB → reject reply, no download.
- `test_voice_size_missing_proceeds_then_telegrambadrequest_caught` — file_size=None, bot.download mock raises TelegramBadRequest("file is too big") → user gets size-related reply.
- `test_audio_kind_detection_priority` — 6 fixtures: `(filename="voice.m4a", mime="application/octet-stream") -> "m4a"`; `(filename=None, mime="audio/ogg") -> "ogg"`; etc.

---

## RQ6 — `/extract` endpoint JSON request/response shape

### Recommendation

**Request model**:
```python
from pydantic import BaseModel, HttpUrl, Field

class ExtractRequest(BaseModel):
    url: HttpUrl
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    max_duration_sec: int = Field(default=10800, ge=1, le=10800)  # 3 h cap
```

**Response model**:
```python
class ExtractSegment(BaseModel):
    start: float
    end: float
    text: str

class ExtractResponse(BaseModel):
    text: str
    language: str
    duration: float
    title: str | None = None
    channel: str | None = None
    upload_date: str | None = None       # YYYYMMDD from yt-dlp metadata
    segments: list[ExtractSegment] = Field(default_factory=list)
```

**Endpoint**:
```python
@app.post("/extract", response_model=ExtractResponse)
async def extract(
    req: ExtractRequest,
    _: None = Depends(verify_token),
) -> ExtractResponse:
    with tempfile.TemporaryDirectory() as tmpdir:
        td = Path(tmpdir)
        try:
            audio_path = await extract_audio(str(req.url), td, timeout=600)
            metadata = await yt_dlp_metadata(str(req.url))
        except YtDlpDiskFullError:
            raise HTTPException(507, "insufficient disk")
        except YtDlpTimeoutError:
            raise HTTPException(504, "yt-dlp timeout")
        except YtDlpExtractError as exc:
            raise HTTPException(422, f"extract: {exc}"[:500])

        duration = await ffprobe_duration(audio_path)
        if duration > req.max_duration_sec:
            raise HTTPException(413, f"duration {duration}s exceeds cap {req.max_duration_sec}s")

        wav_path = td / "audio.wav"
        await _convert_to_wav(audio_path, wav_path)
        result = await asyncio.to_thread(
            mlx_whisper.transcribe,
            str(wav_path),
            path_or_hf_repo=MODEL_REPO,
            language=req.language,
        )

    return ExtractResponse(
        text=result["text"].strip(),
        language=result.get("language", req.language),
        duration=result.get("duration", duration),
        title=metadata.get("title"),
        channel=metadata.get("channel") or metadata.get("uploader"),
        upload_date=metadata.get("upload_date"),
        segments=[
            ExtractSegment(start=s["start"], end=s["end"], text=s["text"])
            for s in result.get("segments", [])
        ],
    )
```

**Russian-friendly error mapping** (in bot side `transcription.py`):
```python
_HTTP_ERR_MAP = {
    400: "не получилось разобрать ссылку",
    413: "слишком длинная запись (>3 часа), разбей на части",
    422: "не смог извлечь аудио из ссылки (yt-dlp не справился)",
    504: "yt-dlp таймаут (>10 мин на скачивание)",
    507: "Mac sidecar — закончилось место",
}
```

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| `HttpUrl` rejects schemes pydantic doesn't recognize | acceptable — only http/https are valid for yt-dlp anyway. |
| yt-dlp metadata missing `title` (e.g. raw `.mp3` direct link) | optional fields. |
| Segments balloon to 50 KB JSON for 3 h transcript | bot side does NOT use segments today; consider `segments=[]` on response if `len(segments) > 500`. v0 keeps full segments — owner can later turn into clip extracts. |

### Test strategy

- `test_extract_request_validation` — invalid URL, invalid language → 422.
- `test_extract_too_long_rejects` — mock duration probe = 4 h → 413.
- `test_extract_full_path` — mock yt-dlp + mlx_whisper → assert response shape.

---

## RQ7 — Bearer token generation + rotation

### Recommendation

**Token generation** (in `setup-mac-sidecar.sh`):
```bash
TOKEN_FILE=~/.config/whisper-server/.env
mkdir -p ~/.config/whisper-server
chmod 700 ~/.config/whisper-server
if [ ! -f "$TOKEN_FILE" ] || ! grep -q WHISPER_API_TOKEN "$TOKEN_FILE"; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  echo "WHISPER_API_TOKEN=${TOKEN}" >> "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "Generated WHISPER_API_TOKEN — copy this line to ~/.config/0xone-assistant/secrets.env on VPS:"
  grep WHISPER_API_TOKEN "$TOKEN_FILE"
fi
```

**FastAPI auth dep**:
```python
# whisper-server/main.py
import os
import secrets
from fastapi import HTTPException, Header, status

_EXPECTED_TOKEN = os.environ.get("WHISPER_API_TOKEN", "")
if len(_EXPECTED_TOKEN) < 32:
    raise SystemExit("WHISPER_API_TOKEN missing or too short (require >=32 chars)")


async def verify_token(
    authorization: str | None = Header(default=None),
) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[len("Bearer "):].strip()
    if not secrets.compare_digest(presented, _EXPECTED_TOKEN):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid bearer",
            headers={"WWW-Authenticate": "Bearer"},
        )
```

`secrets.compare_digest` is mandatory (constant-time comparison; `==` leaks length via timing).

**Bot side — `Settings` extension** (`assistant/config.py`):
```python
# Phase 6c
whisper_api_url: str | None = None              # e.g. "http://whisper-mac:9000"
whisper_api_token: str | None = None            # set via WHISPER_API_TOKEN env
whisper_timeout: int = 3600
yt_dlp_timeout: int = 600
voice_vault_threshold_seconds: int = 120
voice_meeting_default_area: str = "inbox"
claude_voice_timeout: int = 900                 # C3 closure
```

**Bot httpx client** sets header per request:
```python
headers = {"Authorization": f"Bearer {self._settings.whisper_api_token}"}
async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as c:
    ...
```

**Rotation**: manual. Edit both `.env` files (Mac + VPS), restart whisper-server (`launchctl kickstart -k gui/$(id -u)/com.zeroxone.whisper-server`) and bot (`docker compose restart bot`). NO automated rotation v0. Document in `whisper-server/README.md`.

### Risks / failure modes

| Risk | Mitigation |
|---|---|
| Token leaked in logs | log NEVER prints `authorization` header; structlog filter recommended (defer to phase 9 — single-user trust). |
| Token check before lifespan model load completes | uvicorn doesn't open socket until lifespan returns; can't happen. |
| Empty token in env (whisper-server boots in unauth mode) | startup-time `len() < 32` check exits 1. |
| Bot uses `whisper_api_token=None` accidentally | httpx sends `Authorization: Bearer None` → 401 → spec's "transcription unavailable" reply. Better: settings validator that requires both `whisper_api_url` and `whisper_api_token` to be set together. |

Add `Settings.model_validator` that asserts both are None or both set.

### Test strategy

- `test_verify_token_missing_header` → 401.
- `test_verify_token_wrong` → 401.
- `test_verify_token_valid` → no exception.
- `test_settings_pair_validator` — `whisper_api_url` set but `whisper_api_token` None → ValueError.

---

## RQ8 — Voice marker format consistency

### Problem

Phase 6b uses `[photo: <name> | seen: <200 chars>]`. Spec v1 of phase 6c uses `[voice: D:DD | "<200 chars>" | vault: <path>]` — different prefix (`seen:` missing) and different quoting style. Devil M6 flagged the inconsistency.

### Recommendation — UNIFY on phase 6b's `seen:` semantic

`seen:` represents "what model saw at this turn" — for voice, what the model saw IS the transcript (post-summary or full). Reusing the prefix means:
- The model's mental model of "all attachments produce a `seen:` summary" stays consistent.
- Future history rendering can grep `\[(?:photo|voice|file)-?[^]]*\bseen:` uniformly.

**Final marker format**:

| Case | Marker |
|---|---|
| Voice ≤2 min | `[voice: D:DD \| seen: "<first 200 chars>"]` |
| Voice >2 min | `[voice: D:DD \| seen: "<first 200 chars of summary>" \| vault: <path>]` |
| Audio file (m4a / mp3 attachment) | `[audio: <filename> \| D:DD \| seen: "<200 chars>" \| vault: <path>]` (filename is the original; `vault:` only if >2 min) |
| URL extraction | `[voice-url: <truncated 80-char url> \| D:DD \| seen: "<200 chars>" \| vault: <path>]` |
| H5 trigger (caption matches `сохрани`/`запиши`/`vault`/`note`) | `[voice: D:DD \| seen: "<200 chars>" \| (saved by user-request)]` (model called `memory_write`; we do NOT auto-save) |

**Rules**:
- `D:DD` for <1 h, `H:MM:SS` for >1 h. `_fmt_duration_marker(sec)` helper.
- `seen:` content is the truncated transcript (≤2 min) OR truncated Claude-generated summary (>2 min, which is the user-facing reply anyway). Trim at 200 chars at word boundary; append `…` if truncated.
- `<path>` is vault-relative (`inbox/transcript-2026-04-27-1530-voice.md`).
- URL truncation at 80 chars with trailing `…` if longer; **never** truncates the path (could break Obsidian wikilinks if rendered).

**Sample examples**:
```
[voice: 0:23 | seen: "Привет, можешь напомнить про созвон в среду в 15:00, обсудить релиз"]

[voice: 8:12 | seen: "Краткое саммари: обсуждали архитектуру FastAPI сервиса, решили использовать Tailscale, задача — настроить ACL до пятницы…" | vault: inbox/transcript-2026-04-27-1530-voice.md]

[audio: meeting-q2-review.m4a | 47:33 | seen: "Q2 ретроспектива: rev +18% YoY, churn -2pp, новый канал acquisition…" | vault: meetings/transcript-2026-04-27-1530-audio.md]

[voice-url: https://www.youtube.com/watch?v=dQw4w9WgXcQ | 3:32 | seen: "Песня про never gonna give you up…"]

[voice-url: https://very-long-podcast-host.example.com/episodes/2026-… | 1:24:08 | seen: "Тезисы: Apple перевела…" | vault: inbox/transcript-2026-04-27-1532-url.md]
```

### Test strategy

- `test_marker_voice_short` — 23 s voice → `[voice: 0:23 | seen: "..."]`.
- `test_marker_voice_long_includes_vault` — 4 min voice → marker contains both `seen:` and `vault:`.
- `test_marker_url_truncates_long_url` — 200-char URL → 80 chars + `…`.
- `test_marker_h5_user_request_save` — caption "сохрани" → marker says `(saved by user-request)`, no `vault:` segment.

---

## Cross-cutting concerns

### `bridge.ask` `timeout_override` kwarg (C3 closure implementation)

`src/assistant/bridge/claude.py:292` currently uses `asyncio.timeout(self._settings.claude.timeout)` (300s). Add an optional override:

```python
async def ask(
    self,
    chat_id: int,
    user_text: str,
    history: list[dict[str, Any]],
    *,
    system_notes: list[str] | None = None,
    image_blocks: list[dict[str, Any]] | None = None,
    timeout_override: int | None = None,
) -> AsyncIterator[Any]:
    ...
    timeout_s = timeout_override or self._settings.claude.timeout
    async with self._sem:
        try:
            async with asyncio.timeout(timeout_s):
                ...
```

Handler call (audio branch) passes `timeout_override=settings.claude_voice_timeout` (900s). Default 300 s preserved for text/photo paths. **No semaphore change** — `self._sem` (bounded by `claude.max_concurrent=2`) still applies; voice turns just hold the slot longer. With single-user discipline this is fine; document.

Add log line: when `timeout_override` differs from `claude.timeout`, log `event="claude_ask_timeout_override", timeout_s=900`.

### Initial ack mechanism (C2 closure implementation)

The phase 6a/6b chunks/emit pattern accumulates output and flushes only after `handler.handle()` returns. For voice/url, we need to deliver the ack message BEFORE the per-chat lock + transcribe + Claude call (which can take 20+ minutes). Solution: bypass `chunks.append` for the ack only, send directly via `bot.send_message`. Acquire lock AFTER ack. Simplified pseudocode for `_on_voice`:

```python
async def _on_voice(self, message: Message) -> None:
    voice = message.voice
    if voice is None: return
    file_size = voice.file_size or 0
    if file_size > TELEGRAM_DOC_MAX_BYTES:
        await message.reply("аудио файл больше 20 МБ"); return
    duration_sec = voice.duration or 0
    if duration_sec > 3 * 3600:
        await message.reply("слишком длинная запись (>3 часа)"); return

    # Initial ack — bypasses chunks accumulator
    ack = _format_initial_ack(duration_sec, source="voice")
    await self._bot.send_message(message.chat.id, ack)

    # Now download + dispatch via standard handler.handle() route
    tmp_path = await self._download_voice(voice)
    incoming = IncomingMessage(
        chat_id=message.chat.id,
        message_id=message.message_id,
        text=message.caption or "",
        attachment=tmp_path,
        attachment_kind="ogg",
        attachment_filename=tmp_path.name,
        audio_duration=duration_sec,
        audio_mime_type="audio/ogg",
    )
    chunks: list[str] = []
    async def emit(text: str) -> None: chunks.append(text)
    typing_task = asyncio.create_task(self._periodic_typing(message.chat.id))
    try:
        await self._handler.handle(incoming, emit)
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task
    full = "".join(chunks).strip() or "(пустой ответ)"
    for part in _split_for_telegram(full, limit=TELEGRAM_MSG_LIMIT):
        await self._bot.send_message(message.chat.id, part)


async def _periodic_typing(self, chat_id: int) -> None:
    """Manual replacement for ChatActionSender.typing — survives errors."""
    while True:
        try:
            await self._bot.send_chat_action(chat_id, "typing")
        except Exception as exc:                       # noqa: BLE001
            log.debug("typing_action_skip", error=repr(exc))
        await asyncio.sleep(4.5)                       # Telegram TTL ~5s
```

Document the deviation in module docstring: "voice path bypasses the chunks accumulator pattern because the initial ack must reach owner BEFORE per-chat lock acquisition."

### Russian initial-ack format

Spec example: `"⏳ получил аудио 1:30:00, начинаю транскрибацию (~22 мин при 4x realtime)"`.

Helper:
```python
def _format_initial_ack(duration_sec: int, *, source: str) -> str:
    h, rem = divmod(duration_sec, 3600)
    m, s = divmod(rem, 60)
    if h: dur = f"{h}:{m:02d}:{s:02d}"
    else: dur = f"{m}:{s:02d}"
    eta_sec = max(int(duration_sec / 4), 5)            # 4x realtime on M4
    eta_min = max(eta_sec // 60, 1)
    if source == "url":
        return f"⏳ получил ссылку, скачиваю аудио и транскрибирую (~{eta_min} мин при 4x realtime)"
    return f"⏳ получил аудио {dur}, начинаю транскрибацию (~{eta_min} мин при 4x realtime)"
```

### Settings additions (final list)

```python
# Phase 6c additions to assistant/config.py Settings:
whisper_api_url: str | None = None
whisper_api_token: str | None = None
whisper_timeout: int = 3600
yt_dlp_timeout: int = 600
voice_vault_threshold_seconds: int = 120
voice_meeting_default_area: str = "inbox"
claude_voice_timeout: int = 900

# Validator: both whisper_api_url AND whisper_api_token must be set together.
@model_validator(mode="after")
def _validate_whisper_pair(self) -> "Settings":
    if bool(self.whisper_api_url) ^ bool(self.whisper_api_token):
        raise ValueError(
            "WHISPER_API_URL and WHISPER_API_TOKEN must both be set or both unset"
        )
    return self
```

---

## Anti-patterns to avoid

1. **Calling `memory_write` @tool from the handler** — the entire reason for RQ-C1's wrapper. Don't do it. (And don't try to invoke the tool dispatcher manually with crafted args — the trust boundary semantics differ.)
2. **Pre-loading mlx-whisper at module-import time** — breaks FastAPI test isolation. Use lifespan.
3. **`ChatActionSender.typing()` for voice path** — it cancels its task on aiogram disconnect → silent typing dropouts during a 20-min transcribe. Use the manual periodic loop (H2 closure).
4. **shell=True in subprocess** — yt-dlp + ffmpeg both safe via args list. Do not concatenate URL into a shell string.
5. **`==` for token comparison** — timing leak. Use `secrets.compare_digest`.
6. **Unbounded `bot.download` timeout** — phase 6a uses `timeout=90`. For voice, 60 s is plenty (20 MB / typical Telegram CDN). Use 90 s for parity.
7. **Auto-saving every voice >2 min regardless of caption** — H5 closure: caption containing "сохрани/запиши/vault/note" defers to model. Do not double-save.
8. **Skipping `validate_path` because the path is "trusted"** — caption-derived areas can contain weird unicode; validate_path is the choke point.
9. **Trusting `ResultMessage.duration_ms` from Claude as transcribe duration** — the marker uses Whisper's `result["duration"]` (audio duration), not Claude's call duration. Document the field source clearly.

---

## Spec contradictions / extensions surfaced during research

1. **RQ-C1 (NEW)**: spec leaks "memory_save_note Python API" but phase 4 only ships @tool handlers. Extension: new `assistant/memory/store.py` module with `save_transcript()` direct-callable wrapper. Without this, AC#2/AC#3/AC#4 are not implementable.
2. **H7 sentinel handling** — spec says "replace, never reject"; phase-4 `core.sanitize_body` rejects on sentinel. Extension: `save_transcript` does pre-sanitize sentinel-strip via `core._SENTINEL_RE.sub("[redacted-tag]", body)` BEFORE calling `sanitize_body`. Documented in module docstring.
3. **RQ8 marker format** — spec v1 uses `"<200 chars>"` plain quotes; phase 6b uses `seen: "<...>"` prefix. Unify on `seen:` (matches "what model saw" semantic).
4. **C3 timeout override** — `claude_voice_timeout=900` only fires for voice/url. Phase-6b photo turns and phase-6a file turns keep 300 s. Document in `bridge.ask` docstring.
5. **C2 ack pattern** — first deviation from the chunks/emit accumulator pattern in this codebase. Document with rationale: ack MUST reach owner BEFORE long-running transcribe, but emit() is post-handler. Direct `bot.send_message` for ack only.
6. **Tailscale sidecar in compose** — phase 5d compose stack didn't include Tailscale. Phase 6c adds it as a sidecar service with `network_mode: "service:tailscale"` for the bot. New env var `TS_AUTHKEY` in `secrets.env`.
7. **yt-dlp URL trigger word** — C4 closure says trigger is `транскрибируй <URL>` or `/voice <URL>`. Recommend supporting BOTH with a single regex: `r"^(?:транскрибируй|/voice)\s+(https?://\S+)\s*$"`. The text path checks this match BEFORE the existing `_URL_RE` (phase 3 installer hint). If matched, route to voice/url path with `_URL_RE` skipped.
8. **`AnyHttpUrl` in pydantic v2** — deprecated alias for `HttpUrl` with extra schemes. Use `HttpUrl` (rejects ftp/file).
9. **3 h cap enforcement location** — bot side (pre-flight via `voice.duration` from Telegram) AND server side (post-extract via `ffprobe`). Belt-and-suspenders. yt-dlp does NOT pre-validate duration before download — bot's pre-flight only catches `voice` and `audio` Telegram metadata; URL path needs server-side check.

---

## Key references

- mlx-whisper PyPI: https://pypi.org/project/mlx-whisper/
- mlx-examples whisper README: https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md
- mlx_whisper transcribe source: https://github.com/ml-explore/mlx-examples/blob/main/whisper/mlx_whisper/transcribe.py
- mlx-community/whisper-large-v3-turbo: https://huggingface.co/mlx-community/whisper-large-v3-turbo
- whisper.cpp vs MLX 2026 benchmark: https://notes.billmill.org/dev_blog/2026/01/updated_my_mlx_whisper_vs._whisper.cpp_benchmark.html
- MLX vs Ollama 2026: https://willitrunai.com/blog/mlx-vs-ollama-apple-silicon-benchmarks
- Tailscale Docker docs: https://tailscale.com/docs/features/containers/docker
- Tailscale ACL syntax: https://tailscale.com/docs/reference/syntax/policy-file
- Tailscale ACL tags blog: https://tailscale.com/blog/acl-tags-ga
- Tailscale ACL examples (HuJSON): https://github.com/tailscale-dev/docker-guide-code-examples/blob/main/example-acls.hujson
- Tailscale + Docker deep dive: https://tailscale.com/blog/docker-tailscale-guide
- yt-dlp PoToken Guide: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- yt-dlp 2026 cookies guide: https://dev.to/osovsky/6-ways-to-get-youtube-cookies-for-yt-dlp-in-2026-only-1-works-2cnb
- yt-dlp tutorial 2026: https://ostechnix.com/yt-dlp-tutorial/
- yt-dlp PyPI: https://pypi.org/project/yt-dlp
- FastAPI lifespan events: https://fastapi.tiangolo.com/advanced/events/
- FastAPI security HTTPBearer: https://fastapi.tiangolo.com/tutorial/security/first-steps/
- FastAPI request files: https://fastapi.tiangolo.com/tutorial/request-files/
- aiogram 3 getFile docs: https://docs.aiogram.dev/en/latest/api/methods/get_file.html
- aiogram 3 File type: https://docs.aiogram.dev/en/latest/api/types/file.html
- aiogram 20 MB getFile cap discussion: https://github.com/aiogram/aiogram/discussions/557
- launchd info: https://launchd.info/
- Apple launchd docs: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- Python `asyncio.to_thread` docs: https://docs.python.org/3/library/asyncio-task.html
- Python `secrets.compare_digest`: https://docs.python.org/3/library/secrets.html
- Python subprocess security: https://docs.python.org/3/library/subprocess.html#security-considerations
- transliterate library (alternative to hand-roll): https://pypi.org/project/transliterate/
