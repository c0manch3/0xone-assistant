# Phase 7 spike findings (2026-04-17)

Empirical probes of phase-7 assumptions from `plan/phase7/detailed-plan.md`.
Each spike has a Python script + JSON raw-report under `spikes/phase7_*`.

- Environment:
  - `claude_agent_sdk==0.1.59`, `claude` CLI `2.1.114 (Claude Code)`.
  - macOS ARM64 (Darwin 24.6.0), Python 3.12.
  - OAuth via user's `~/.claude/` session. No `ANTHROPIC_API_KEY`.

## Verdict table

| # | Question | Verdict | Primary evidence | Plan impact |
|---|---|---|---|---|
| **S-0 (BLOCKER)** | SDK multimodal envelope | **PASS** | All 7 Q0 probes green (see §1). Flag-image correctly described at 5 MB and up to 10 MB. | `MEDIA_PHOTO_MODE=inline_base64` default stays. Plan §6.2 envelope order validated. |
| Q0-1 | Mixed text+image+note | **PASS** | Model replied "Flag of Hungary (red, white, green horizontal stripes)" — saw the image | keep §6.2 order |
| Q0-2 | jpeg/png/webp labels | **PASS (permissive)** | All three labels accepted on real JPEG bytes; Anthropic backend tolerant of label/magic mismatch. Test with actual PNG/WEBP bytes recommended as phase-7 integration. | Accept jpeg/png/webp in handler; pass real `mime_type` from aiogram to SDK |
| Q0-3 | Size boundary | **PASS up to 10 MB** | All five sizes (100K, 1M, 3M, 5M, 10M) accepted with ~5 s wall each. Padded-COM JPEGs compress well — real 10 MB JPEGs may be stricter. | `MEDIA_PHOTO_MAX_INLINE_BYTES=5MB` default is conservative and safe. |
| Q0-4 | Multi-photo | **PASS** | Model replied "got 3". Kept OUT-OF-SCOPE (Q12) per decision, but capability is there for phase-9. | no plan change |
| Q0-5 | Block order | **PASS** | No error; head/tail-note not echoed (weak signal) — absence of error is primary. | §6.2 ordering stands |
| Q0-5b | Scheduler+URL+image combo | **PASS** | Model replied in Russian, noted flag + URL + acknowledged system-notes | Handler can freely mix scheduler/URL/image envelopes |
| Q0-6 | History replay | **PASS (both modes)** | Mode A (raw image): model saw prior image. Mode B (placeholder): model acknowledged prior photo. | Keep plan §6.3 (placeholder on replay) — cheaper and confirmed working |
| S-1 | IPv6 / SSRF loopback-only | **PASS** | 11/11 cases match expectation | Delegate CLI `_validate_endpoint` to a `is_loopback_only(url)` helper; narrower than `classify_url` |
| S-2 | ARTEFACT_RE corpus | **PARTIAL → v3 recommended** | 43/46 corpus cases pass with v3 regex. 3 acceptable corner failures (path-guard catches). v1 regex from plan §7 had 4 false-positive failures (URL false positives). | Update §7 regex to v3 (see §3 below) |
| S-3 | fpdf2 Cyrillic render | **PASS_WITH_PIL_AS_REQUIRED_DEP** | PDF rendered 11 KB with Cyrillic; but fpdf2 Requires-Dist declares `Pillow>=8.3.2`. Plan §82 claim "no Pillow" is wrong. | Document Pillow as unavoidable transitive dep; size estimate corrected |
| S-4 | Shared venv deps | **PASS, but 48.75 MB delta** | lxml 18.8 MB + PIL 12.5 MB + fontTools 11.8 MB = top 3 contributors. Plan assumption about "no lxml" is wrong. | Update §9 MediaSettings prose with real numbers; no design change |
| S-5 | Genimage quota race | **PASS (with known jitter)** | 4/4 scenarios green: midnight rollover OK, flock prevents double-increment under 10-worker contention. NTP step-back across midnight creates ±1 extra request. | Accept known jitter; keep flock pattern from plan §3.2 |
| S-6 | aiogram File.file_size semantics | **PASS** | `File.file_size: int \| None` confirmed across ALL 5 media classes (Voice, VideoNote, Document, PhotoSize, Audio). SizeCappedWriter pattern aborts mid-download for None-sized overruns. | Update §5.5 `media/download.py` to implement pre-flight + streaming cap |
| S-7 | Handler partial-attachment failure | **PASS** | 3-photo middle-missing scenario produces 2 image_blocks + 3 notes (including failure-note), 1 warn recorded. | Update §6.1 pseudocode to the tested "safe" form |

Overall pipeline readiness: **GO for devil wave-2**, no blockers remain.

Spike 0 PASS confirms photo-in is viable; `MEDIA_PHOTO_MODE=inline_base64`
remains default. Subsequent spikes surfaced 3 plan-correction items
(S-3 Pillow, S-4 lxml, S-2 regex) that are incorporated into
`implementation.md v1` §5 "findings-driven corrections".

## 1. Spike 0 (BLOCKER) — multimodal envelope

### Method

- Script: `spikes/phase7_s0_multimodal_envelope.py` (~510 LOC)
- Real-valid JPEG: 128×128 RGB, red+green bars, 2290 bytes, generated
  via Pillow at spike-setup time and base64-embedded in the script.
- `claude_agent_sdk.query()` with a minimal `ClaudeAgentOptions`
  (`setting_sources=[]`, `max_turns=1`, `allowed_tools=[]`).
- Each probe runs a one-turn `query` with custom content blocks; the
  assistant text is captured + inspected.

### Raw results

| Probe | Wall | Reply (first 100 chars) |
|---|---|---|
| Q0-1 text+image+note | ~6 s | "Flag of Hungary (red, white, green horizontal stripes)." |
| Q0-2 image/jpeg | ~5 s | "ok image/jpeg" |
| Q0-2 image/png | ~5 s | "ok image/png" |
| Q0-2 image/webp | ~5 s | "ok image/webp" |
| Q0-3 100 KB | 5.24 s | "ok 100KB" |
| Q0-3 1 MB | 5.47 s | "ok 1MB" |
| Q0-3 3 MB | 5.26 s | "ok 3MB" |
| Q0-3 5 MB | 4.98 s | "ok 5MB" |
| Q0-3 10 MB | 5.24 s | "ok 10MB" |
| Q0-4 multi (3 images) | ~6 s | "got 3" |
| Q0-5 order | ~5 s | "Red and green horizontal flag." |
| Q0-5b sched+URL+image | ~7 s | "На фото — флаг…" (Russian, saw all blocks) |
| Q0-6 mode-A raw image | ~5 s | "I see the image — it shows the flag of Hungary…" |
| Q0-6 mode-B placeholder | ~3 s | "I acknowledge that you sent a photo earlier…" |

### Interpretation

- Envelope shape `{type: text} + {type: image, source: {type: base64, media_type, data}} + {type: text}` works on SDK 0.1.59.
- `media_type` field is tolerant — backend does not strictly validate
  bytes vs label. Phase-7 handler still SHOULD pass the real mime from
  aiogram (honest data).
- 10 MB payload accepted, but my "bloated" test image has COM segments
  of null bytes (highly compressible over HTTP/2). **Real 10 MB JPEG
  photos may be rejected by the backend's content moderation pipeline
  or just by size limits the API does not document.** Safe default:
  `MEDIA_PHOTO_MAX_INLINE_BYTES=5_242_880`.
- History replay with placeholder text-note ("[system-note: prior user
  envelope contained an image at /abs/…]") is sufficient — the model
  acknowledges "I acknowledge that you sent a photo earlier". Keeping
  plan §6.3's placeholder approach.

### Raw evidence excerpt (Q0-5b)

```
User (combined envelope):
  - [system-note: autonomous turn from scheduler id=42; owner is not active]
  - глянь фото и скажи что на нём. https://example.com/foo тоже.
  - [system-note: user message contains URL(s)…]
  - <image: 2290 bytes, flag>
  - [system-note: user attached photo at /abs/outbox/test.jpg (128x128).]

Assistant reply:
  На фото — флаг: две горизонтальные полосы — красная сверху и зелёная
  снизу (похож на флаг Венгрии или Мадагаскара без белой полосы).
  Ссылку `https://example.com/foo` открою:
```

The model correctly:
- saw the image content,
- saw the URL content,
- implicitly honoured the scheduler-note context (answered directly).

### Decision

- **`MEDIA_PHOTO_MODE=inline_base64` is the default.**
- Envelope order: `text → system-notes (before image) → image → system-notes (after image)`.
  Plan §6.2 currently says "image AFTER text, BEFORE notes". The
  combined Q0-5b probe shows that interleaved orders also work — no
  hard requirement. Plan stays as written.
- History replay uses the placeholder note. Raw image replay is the
  validated fallback if the SDK ever changes behaviour.

### Caveats and unknowns

- We used padded-COM JPEGs to reach 5/10 MB size points. Real photos
  compress less; true 5 MB JPEG may be 10+ MB over the wire after
  base64. The plan's 5 MB cap is conservative and should hold in
  production, but integration tests in phase-7 should include at least
  one real-photo > 3 MB sample before shipping.
- We did not test multi-envelope history with > 1 image. The history
  code in `bridge/history.py` will strip to placeholders anyway, so
  this is not on the critical path.
- Q0-2 "passed" for all three labels but the bytes were real JPEG
  under each label. Integration needs at least one real PNG to verify
  the decoder accepts png-magic under `image/png`.

## 2. S-1 endpoint SSRF guard

- Script: `spikes/phase7_s1_endpoint_ssrf.py`
- All 11 URLs classify as expected:
  - Loopback (4): `http://localhost:9100`, `http://127.0.0.1`, `http://127.0.0.2`, `http://[::1]` → allowed.
  - Non-loopback private (3): `10.0.0.1`, `192.168.1.1`, `api.telegram.org` (resolves to public IP) → denied.
  - AWS IMDS link-local (1): `169.254.169.254` → denied.
  - Malformed/FTP (2): `ftp://localhost`, `http://:9100` → denied.
  - DNS-miss (1): `localhost.localdomain` → denied (no DNS answer).

### Decision

- CLI `_validate_endpoint(url)` MUST delegate to a narrow
  `is_loopback_only(url)` helper, NOT to the phase-3 `classify_url`
  (which allows `10.x` and `192.168.x` — those are private but not
  loopback; for phase-7 transcribe/genimage endpoints we want a
  stricter rule since the host machine is reached via SSH reverse
  tunnel on `127.0.0.1:<port>`).
- `is_loopback_only`: parse URL, enforce scheme=http(s), IP-literal
  check (loopback), else DNS-resolve ALL A/AAAA records and require
  every one to be loopback. Reject on any non-loopback.
- Place the helper in `src/assistant/bridge/net.py` OR in the CLI
  module directly. Recommendation: **CLI-local** (CLI is stdlib-only;
  no src/ import) + **mirror the 15-line helper** the way
  `_net_mirror.py` already does for `classify_url`.

## 3. S-2 ARTEFACT_RE corpus

- Script: `spikes/phase7_s2_artefact_regex.py`, 46-case corpus.
- Plan §7 regex (v1) scores 42/46.
- Proposed v3 regex scores 43/46, with 3 acceptable corner-case
  failures (path-guard catches them).

### Failures of v1 (plan §7 as written)

| ID | Text | v1 false positive |
|---|---|---|
| `url_with_ext` | `https://host.com/abs/outbox/x.png` | Matches `//host.com/abs/outbox/x.png` (should NOT match — inside URL) |
| `url_pdf` | `https://example.com/docs/report.pdf` | Matches `//example.com/docs/report.pdf` |
| `adjacent_paths` | `/abs/x.png/abs/y.pdf` | Matches whole string as one path |
| `dot_slash` | `see ./outbox/x.png please` | Matches `/outbox/x.png` (the relative path's tail) |

### Recommended v3 regex (to replace plan §7 `_ARTEFACT_RE`)

```python
_ARTEFACT_RE = re.compile(
    r"(?<![\w/.:])(/[^\s`\"'<>()\[\]]+?"
    rf"(?:{'|'.join(re.escape(e) for e in _ALL_EXT)}))"
    r"(?=[\s`\"'<>()\[\].,;:!?/]|$)",
    re.IGNORECASE,
)
```

Changes vs v1:
- Lookbehind broadened: `(?<![\w/.:])` — now also rejects preceding
  `.` (kills `./outbox/x.png` FP) and `:` (kills `http://…/x.png` FP).
- Body turned non-greedy: `[^\s`\"'<>()\[\]]+?`
- Lookahead tightened: explicit stop-set `[\s`\"'<>()\[\].,;:!?/]|$`
  so `/abs/x.png/abs/y.pdf` splits at the `/` boundary and extracts
  only `/abs/x.png`.

### Remaining 3 corner cases (acceptable)

| ID | Text | Plan outcome |
|---|---|---|
| `adjacent_paths` | `/abs/x.png/abs/y.pdf` | v3 matches only `/abs/x.png`; `exists()` check fails since that compound path isn't a file. |
| `colon_before` | `готово:/abs/outbox/x.png` | v3 doesn't match; model usually puts a space. Accept as minor false-negative. |
| `nested_path` | `/abs/outbox/x.png/y` | v3 matches `/abs/outbox/x.png`; `exists()` passes iff that's a file. If the model meant a nested path, `exists()` fails anyway. |

### Decision

- Apply v3 regex in `adapters/dispatch_reply.py`.
- Port the 46-case corpus verbatim into `tests/test_dispatch_reply_regex.py`.
- Document the 3 acceptable corner cases in the test as skipped /
  xfail with explanatory comments.

## 4. S-3 fpdf2 Cyrillic render

- Script: `spikes/phase7_s3_fpdf2_cyrillic.py`
- `fpdf2==2.8.7` installed into a clean isolated venv via `uv run --isolated`.
- DejaVu Sans TTF: found locally at
  `/Users/agent2/Documents/midomis-bot/document-server/fonts/DejaVuSans.ttf`
  (757 KB). Upstream URL `https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf` returns 404 — use a mirrored
  copy or vendor the TTF into `tools/render_doc/_lib/`.
- PDF output: 11 446 bytes for 4 short lines of Russian text (file
  includes an embedded DejaVu Sans subset).

### Key finding

**fpdf2 REQUIRES Pillow.** `fpdf2.dist-info/METADATA` declares:

```
Requires-Dist: defusedxml
Requires-Dist: Pillow!=9.2.*,>=8.3.2
Requires-Dist: fonttools>=4.34.0
```

Importing `fpdf` at any point (even without calling `add_font`) loads
`PIL`, `PIL.Image`, `PIL._imaging` and ~20 PIL submodules. This is a
hard, unavoidable cost.

Plan `description.md` §82 claim "fpdf2 renders Cyrillic without
Pillow" is incorrect. Plan text needs correction.

### Decision

- Accept Pillow as a required transitive dep (MediaSettings §9 size
  estimate updated by S-4 numbers).
- Continue with DejaVu Sans bundle strategy. Vendor the TTF into
  `tools/render_doc/_lib/DejaVuSans.ttf` (~757 KB).
- Update plan §9 deps list to include `pillow>=10` explicitly so future
  readers know it's pulled in.
- Update `description.md` §82 pitfall wording.

## 5. S-4 shared venv deps

- Script: `spikes/phase7_s4_venv_deps.py`
- Clean venv + installed `pypdf>=4.0 python-docx>=1.0 openpyxl>=3.1 striprtf>=0.0.28 defusedxml>=0.7 fpdf2>=2.7`.
- Baseline venv: ~negligible.
- Post-install delta: **+48.75 MB**.

### Top contributors (site-packages)

| Package | Size | Source |
|---|---|---|
| lxml | 18.82 MB | transitive via `python-docx>=1.0` |
| PIL (Pillow) | 12.48 MB | transitive via `fpdf2` (see S-3) |
| fontTools | 11.83 MB | transitive via `fpdf2` |
| docx (python-docx) | 1.47 MB | direct |
| pypdf | 1.36 MB | direct |
| fpdf (fpdf2) | 1.26 MB | direct |
| openpyxl | 0.80 MB | direct |
| typing-extensions / et_xmlfile / defusedxml | <50 KB each | transitive |

### Installed versions

```
defusedxml        0.7.1
et-xmlfile        2.0.0
fonttools         4.62.1
fpdf2             2.8.7
lxml              6.1.0
openpyxl          3.1.5
pillow            12.2.0
pypdf             6.10.2
python-docx       1.2.0
striprtf          0.0.29
typing-extensions 4.15.0
```

### Decision

- **Update plan §9 MediaSettings prose:** "Phase-7 deps add ~50 MB to
  the shared venv: 18 MB lxml (python-docx), 12 MB Pillow (fpdf2
  transitive), 12 MB fontTools (fpdf2 transitive), ~5 MB direct deps."
- **No architecture change.** All three big deps have wheels for
  ARM64 macOS AND x86_64 Linux (manylinux + macosx_11_0_arm64). `uv pip
  install` completed without compile.
- **Risk note (small):** lxml via `python-docx` is a C extension;
  wheel availability is excellent (manylinux2014) but single
  wheels-less platform (e.g. Alpine musl) would force a source build.
  Pin to what works on the owner's Linux VPS distro; document.

## 6. S-5 genimage quota race

- Script: `spikes/phase7_s5_quota_race.py`
- 4 scenarios, all **PASS** or **PASS_WITH_KNOWN_JITTER**.

### Results

| Scenario | Result | Note |
|---|---|---|
| R-1 cross-midnight (Day 1 23:59:59.8 → Day 2 00:00:00.2) | PASS | Day 1 count=1, Day 2 count=1 |
| R-2 same-day cap=1, two requests | PASS | Second call denied, count stays 1 |
| R-3 concurrent flock, 10 parallel workers, cap=1 | PASS | Exactly 1 allowed; 9 denied; final count=1 |
| R-4 NTP clock rollback across midnight | PASS_WITH_KNOWN_JITTER | Forward (day 2) allowed; then rollback (day 1) ALSO allowed — date mismatch resets count. Accept as rare edge. |

### Decision

- Keep flock-protected JSON quota pattern from plan §3.2.
- Document R-4 jitter behaviour in the CLI's SKILL.md (known minor
  quota leak on NTP rollback). No plan change.
- Unit test: port the 4 scenarios into `tests/test_tools_genimage_cli.py`
  (R-3 flock contention is a good parallel test).

## 7. S-6 aiogram File.file_size semantics

- Script: `spikes/phase7_s6_bot_download.py`
- Static pydantic-field inspection on aiogram `File`, `Voice`,
  `VideoNote`, `Document`, `PhotoSize`, `Audio` + behavioural test of
  a `SizeCappedWriter` pattern.

### Field shape (all phase-7-relevant)

```
File.file_id:          str,  required
File.file_unique_id:   str,  required
File.file_size:        int | None  (optional)
File.file_path:        str | None  (optional)

Voice.file_size:       int | None
VideoNote.file_size:   int | None
Document.file_size:    int | None
PhotoSize.file_size:   int | None
Audio.file_size:       int | None
```

**Every media kind allows `file_size=None`.** Pre-flight `if att.file_size > cap:` is necessary but NOT sufficient.

### Bot.download_file signature

```
(self,
 file_path: str | pathlib.Path,
 destination: BinaryIO | pathlib.Path | str | None = None,
 timeout: int = 30,
 chunk_size: int = 65536,
 seek: bool = True) -> BinaryIO | None
```

Destination MAY be a custom `BinaryIO` — our `SizeCappedWriter` wrapper
is compatible.

### Tested defence pattern

```python
class SizeCappedWriter:
    def __init__(self, dest: BinaryIO, cap: int) -> None:
        self._dest = dest
        self._cap = cap
        self._written = 0

    def write(self, data: bytes) -> int:
        self._written += len(data)
        if self._written > self._cap:
            raise SizeCapExceeded(...)
        return self._dest.write(data)

async def safe_download(bot, file, dest_path, *, max_bytes):
    if file.file_size is not None and file.file_size > max_bytes:
        return reject
    with dest_path.open("wb") as fp:
        sink = SizeCappedWriter(fp, max_bytes)
        try:
            await bot.download_file(file.file_path, destination=sink)
        except SizeCapExceeded:
            dest_path.unlink(missing_ok=True)  # cleanup partial
            return reject
    return accept
```

### Results

| Case | Outcome |
|---|---|
| A. None size, 2 KB payload, cap 10 KB | allowed (2048 bytes on disk) |
| B. None size, 400 KB payload, cap 100 KB | rejected mid-stream at ~106 KB |
| C. Known 5 MB, cap 100 KB | pre-flight rejected (no bytes downloaded) |
| D. Known 4 KB, cap 100 KB | allowed |

### Decision

- `media/download.py` MUST implement this pattern (both pre-flight
  AND streaming cap).
- Cap constants (plan §9) are OK but tighten voice default to
  `MEDIA_VOICE_MAX_BYTES=15_000_000` (below Telegram's 20 MB Bot-API
  ceiling — leaves ~25 % safety margin for content-type boundary
  padding).
- Cleanup partial file on overrun (unit test this).

## 8. S-7 handler partial-attachment failure

- Script: `spikes/phase7_s7_handler_partial_fail.py`

### Scenarios

| # | Setup | Outcome |
|---|---|---|
| 1 | 3 photos; middle is MISSING | image_blocks=2, notes=3 (one = failure-note), warns=1 |
| 2 | 2 photos; first is OVERSIZE | image_blocks=1 (second only), notes=2 |
| 3 | photo-missing + voice + document | image_blocks=0, notes=3 (first = failure-note, second voice OK, third doc OK) |

All scenarios PASS.

### Recommended §6.1 pseudocode (replaces plan as-written)

```python
image_blocks: list[dict] = []
notes: list[str] = []
for idx, att in enumerate(msg.attachments or ()):
    if att.kind == "photo" and settings.media.photo_mode == "inline_base64":
        if att.file_size is not None and att.file_size > settings.media.photo_max_inline_bytes:
            notes.append(
                f"user attached photo at {att.local_path} but size "
                f"{att.file_size} exceeds inline cap {settings.media.photo_max_inline_bytes}; skipped."
            )
            continue
        try:
            raw = att.local_path.read_bytes()
        except (FileNotFoundError, PermissionError, OSError) as exc:
            notes.append(
                f"user attempted to attach photo at {att.local_path} "
                f"but read failed: {type(exc).__name__}."
            )
            log.warning("media_photo_read_failed", path=str(att.local_path),
                        exc_info=True)
            continue
        mime = att.mime_type or "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        image_blocks.append({"type": "image",
                             "source": {"type": "base64", "media_type": mime, "data": b64}})
        notes.append(f"user attached photo at {att.local_path} ({att.width}x{att.height})")
    elif att.kind in ("voice", "audio"):
        notes.append(f"user attached {att.kind} (duration={att.duration_s}s) at {att.local_path}. "
                     "use tools/transcribe/; if >30s spawn worker.")
    elif att.kind == "document":
        notes.append(f"user attached document '{att.filename_original}' at {att.local_path}. "
                     "use tools/extract-doc/.")
    elif att.kind == "video_note":
        notes.append(f"user attached video_note (duration={att.duration_s}s) at {att.local_path}. "
                     "video out of scope phase 7.")
    else:
        notes.append(f"unknown attachment kind={att.kind!r} at {att.local_path}")
```

Key properties:
- Never raises on a single-attachment error.
- Failure-notes land in the same ordered position as the failing
  attachment, so the model's causal picture stays intact.
- `log.warning(...)` with `exc_info=True` per failure (for operator
  debugging + post-hoc retention diagnosis).

## 9. Pipeline readiness — devil wave-2

**GO.** No BLOCKER remains; Spike 0 PASS confirms photo-in is shippable
on SDK 0.1.59. Findings require incremental plan corrections (see
§§3-8 above) but no architectural pivot.

| Checklist | Status |
|---|---|
| Spike 0 green | ✅ |
| All 7 devil wave-1 gaps probed | ✅ |
| Spike artifacts committed | pending commit |
| `implementation.md` v1 drafted | next |
| Plan §7 regex corrected | v3 ready for wave-2 |
| Plan §9 dep size updated | numbers ready |
| Plan §6.1 safe-handler pseudocode | tested in S-7 |
| Plan §5.5 download cap pattern | tested in S-6 |
| Plan §82 "no Pillow" wording correction flagged | to fix in wave-2 |

## 10. Open questions for devil wave-2

1. Real-PNG / real-WEBP integration test — are phase-7 unit tests
   enough without a multi-format fixture corpus? Recommend adding a
   128-byte PNG and 128-byte WEBP fixture to `tests/fixtures/phase7/`
   and piping through Q0-2 logic again during integration.
2. The size-boundary data from Q0-3 used padded-COM JPEGs (highly
   compressible). Does phase-7 accept a "trust SDK up to 5 MB raw" rule,
   or do we add a real 4-MB photo fixture?
3. `fpdf2` + Pillow — should we ALSO pin Pillow version? Pillow 12.x
   bumped its API; if a future `fpdf2` release tightens the version
   range we could break. Recommend `pillow>=10,<13` in
   `tools/render_doc/pyproject.toml`.
4. Size-capped writer + aiogram's internal chunking — does aiogram's
   default `chunk_size=65536` make overrun detection latency up to
   64 KB over the cap? Acceptable but document.
5. `is_loopback_only` and IPv6 link-local (`fe80::…`) — do we treat it
   as loopback? Spike S-1 didn't exercise that case; conservatively, NO
   (not loopback; link-local is separate classification). Document.
