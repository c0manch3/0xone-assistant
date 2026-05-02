# Phase 9 — Researcher Findings (verification of v2)

**Date:** 2026-05-02
**Researcher pass against:** `plan/phase9/description.md` (v2, 2552 lines)
**Method:** WebFetch + WebSearch against authoritative sources (pandoc.org, doc.courtbouillon.org, docs.aiogram.dev, openpyxl.readthedocs.io, packages.debian.org/bookworm, hackage.haskell.org).

## Verdict

**Mandatory v3 revision.** Two **blocking** factual errors in §2.6 that would silently neutralise the security pipeline (URLFetchingError vs FatalURLFetchingError; outdated WeasyPrint apt deps list). Several smaller corrections required.

Tier-1 result: **3 PASS, 4 FAIL/PARTIAL** (R1.1 partial, R1.2 fail, R1.3 partial, R1.4 partial, R1.5 pass, R1.6 pass, R1.7 pass).

---

## Tier 1 verifications

### R1.1 — Pandoc markdown variant flags

- **Verified:** **Partial.** Subtraction syntax works; extension names correct; one extension (`tex_math_single_backslash`) is **not default-enabled in markdown**, making the subtraction a no-op (harmless but misleading documentation).
- **Evidence:**
  - Pandoc source `Text.Pandoc.Extensions.pandocExtensions` (pandoc 3.9.0.2 hackage, lines 215-263): default `markdown` flavour DOES include `Ext_raw_html`, `Ext_raw_tex`, `Ext_raw_attribute`, `Ext_tex_math_dollars`. URL: <https://hackage-content.haskell.org/package/pandoc-3.9.0.2/docs/src/Text.Pandoc.Extensions.html>
  - **`Ext_tex_math_single_backslash` is NOT in `pandocExtensions`** (only in `gfmExtensions`, `commonmarkExtensions` — see line 508+, 604+). Subtracting a not-enabled extension is a silent no-op — does NOT error, but also does nothing. Same for `Ext_tex_math_double_backslash`.
  - The `markdown-X-Y-Z` subtraction syntax is documented in MANUAL.html "Specifying formats": *"Extensions can be individually enabled or disabled by appending `+EXTENSION` or `-EXTENSION` to the format name."* URL: <https://pandoc.org/MANUAL.html>
  - Pandoc 2.0+ on Debian bookworm = pandoc **2.17.1.1-2~deb12u1** (NOT 3.x). Spec text "current pandoc 3.x" is wrong about the runtime version. Extension list is identical between 2.17 and 3.9, so behaviour is the same. URL: <https://packages.debian.org/bookworm/pandoc>

- **Defaults INCLUDED that we have NOT disabled:**
  - `pipe_tables` — wanted (XLSX renderer parses pipe tables; HTML renderer also benefits).
  - `fenced_code_blocks` — wanted.
  - `footnotes` — wanted.
  - `pandoc_title_block` — accepts `% Title\n% Author\n% Date` at start of file. Probably OK but not documented in spec.
  - `yaml_metadata_block` — **POTENTIAL ATTACK SURFACE.** YAML block at file start can set `title`, `author`, `header-includes` etc. With `raw_html` stripped, `header-includes: <script>...</script>` won't render scripts but might be confusing in the title bar. Recommend explicitly subtracting `-yaml_metadata_block` to be safe.
  - `auto_identifiers` — wanted.
  - `smart` (smartypants) — replaces straight quotes with curly. Fine.

- **Spec change required (v3):**
  - §2.6 step 2: replace flags with:
    ```
    "markdown-raw_html-raw_tex-raw_attribute-tex_math_dollars-yaml_metadata_block"
    ```
    Drop `tex_math_single_backslash` (no-op; documenting "we disable it" creates a false sense of security). Add `-yaml_metadata_block` (closes a small smuggling surface).
  - §2.6 prose: drop "current pandoc 3.x" — actual version on Debian bookworm is **2.17.1.1**. Use neutral phrasing "pandoc as packaged on the runtime base image".
  - **Anti-pattern note for spec**: pandoc DOES NOT error when subtracting an extension that's already disabled. So the spec author cannot assume that "spec compiles" means "all subtractions are meaningful". Wave A spike test must `pandoc --list-extensions=markdown-...` AND grep that `+raw_html` etc. are absent.

### R1.2 — WeasyPrint `url_fetcher` API

- **Verified:** **FAIL.** Spec is wrong on **two** counts; one of them silently neutralises the security-test claim.
- **Evidence:**
  - **(A) Wrong exception class.** Spec imports `from weasyprint.urls import URLFetchingError`. The actual class hierarchy in `weasyprint/urls.py` is:
    ```python
    class URLFetchingError(IOError):
        """Some error happened when fetching an URL."""

    class FatalURLFetchingError(BaseException):
        """Some error happened when fetching an URL and must stop the rendering."""
    ```
    `URLFetchingError` extends `IOError` and is **caught internally by WeasyPrint** — it produces a warning and rendering continues with a placeholder/empty resource. `FatalURLFetchingError` extends `BaseException` and **aborts the render**. Source: <https://github.com/Kozea/WeasyPrint/blob/main/weasyprint/urls.py> + <https://doc.courtbouillon.org/weasyprint/stable/api_reference.html>.

    **Impact**: as written, `safe_url_fetcher` raises `URLFetchingError` for every URL → WeasyPrint catches each → emits warning → continues render with the URL blocked. **Net effect for owner**: PDF still renders successfully (no security risk, since the URL is never fetched), BUT the spec's audit/test claim "render aborts on URL access" is false. AC#16 ("malicious markdown blocks all url-fetches") would be partially correct (no fetch happens) but the test asserting "PDF render fails" will fail. More importantly, the v3 wave-A spike must verify the actual semantics.

    **Two valid implementation patterns:**
    1. **Raise `FatalURLFetchingError`** to abort render (recommended — catches "fetcher was bypassed somehow" bugs loudly).
    2. **Raise `URLFetchingError`** to silently log + continue (acceptable since the URL is never fetched anyway, but masks any future regression where WeasyPrint adds a new fetch path).

    Tighter pattern: raise `FatalURLFetchingError` → spec test asserts `weasyprint.HTML(...).write_pdf(...)` raises (caller catches and converts to `render_failed_input_syntax`). v3 should pick this and audit-row the abort.

  - **(B) Legacy fetcher signature on a deprecation runway.** Spec uses legacy form:
    ```python
    def safe_url_fetcher(url: str, timeout: int = 5):
    ```
    This is the legacy `default_url_fetcher` shape, deprecated as of WeasyPrint **68.0** (Jan 2026, CVE-2025-68616 fix) and scheduled for removal in **69.0**. Source: <https://github.com/Kozea/WeasyPrint/blob/main/weasyprint/urls.py> (deprecation warning text).

    Spec's pin `weasyprint>=63,<70` includes 68.x where the legacy form still works (with `DeprecationWarning`). **It will break on 69.0** — spec correctly excludes that. But the modern recommended form is:
    ```python
    from weasyprint.urls import URLFetcher, FatalURLFetchingError

    class SafeURLFetcher(URLFetcher):
        def fetch(self, url, headers=None):
            raise FatalURLFetchingError(
                f"render_doc: all url-fetches blocked (got url={url[:64]!r})"
            )

    weasyprint.HTML(string=html, base_url=staging_dir, url_fetcher=SafeURLFetcher())
    ```
    Both forms accept `url_fetcher=` parameter. The class form is forward-compatible to 69.0.

- **Spec change required (v3):**
  - Replace `URLFetchingError` import with `FatalURLFetchingError` (the spec's text "blocks every fetch surface" is only true with the Fatal variant; the IOError variant only logs).
  - Migrate to `URLFetcher` subclass form to survive the 68→69 deprecation removal (or extend the upper-bound discussion: "if pinning to <69, legacy fn-form OK; otherwise subclass URLFetcher").
  - Add Wave A spike assertion: `with pytest.raises(FatalURLFetchingError): weasyprint.HTML(string='<img src="file:///etc/passwd">', url_fetcher=...).write_pdf(io.BytesIO())`.

### R1.3 — aiogram 3.x `FSInputFile` + `Bot.send_document`

- **Verified:** **Partial.** `FSInputFile(filename=...)` keyword arg is correct, BUT `bot.send_document` parameter name is **`document`**, not positional `file`.
- **Evidence:**
  - `FSInputFile` constructor (aiogram 3.27.0): `FSInputFile(path: str | Path, filename: str | None = None, chunk_size: int = 65536)`. `filename` is a keyword arg with `None` default. Source: <https://docs.aiogram.dev/en/latest/api/types/input_file.html>
  - `Bot.send_document` signature: takes `chat_id: int` and `document: str | InputFile` as named parameters. `caption: str | None = None` is a separate kwarg on `send_document` (NOT on `FSInputFile`). Source: <https://docs.aiogram.dev/en/latest/api/methods/send_document.html>
  - Spec §2.5 currently has `await self._bot.send_document(chat_id, file, caption=caption)` — passing `file` as the second positional arg. In aiogram 3, the second positional after `chat_id` is named `document`. **Positional pass works** (aiogram does not enforce kw-only via `*`), but conventional usage is `bot.send_document(chat_id=chat_id, document=file_input, caption=caption)`. The spec snippet is functionally correct but the var name `file` shadows Python's builtin.

- **Spec change required (v3):**
  - §2.5 rename local `file` → `document` to match aiogram parameter name and avoid shadowing:
    ```python
    document = FSInputFile(path, filename=suggested_filename)
    await self._bot.send_document(chat_id, document, caption=caption)
    ```
  - Belt-and-suspenders: pass kw-args explicitly: `await self._bot.send_document(chat_id=chat_id, document=document, caption=caption)`. Survives any future aiogram 4 reordering.

### R1.4 — WeasyPrint Cyrillic + DejaVu fonts

- **Verified:** **Partial — PASS for the Cyrillic question, but spec lists wrong apt deps.**
- **Evidence:**
  - WeasyPrint uses Pango for text shaping, which on Linux discovers fonts via fontconfig. Any system-installed font with Cyrillic coverage (DejaVu, Noto, Liberation) will be picked up automatically. No `@font-face` or `font-family` CSS is required for default body text. Source: <https://doc.courtbouillon.org/weasyprint/stable/first_steps.html>; "Tuning Fontconfig" docs.
  - **fontconfig fallback default order on Debian** prefers Noto, then DejaVu (per Fedora wiki / Arch wiki). DejaVu has full LGC (Latin/Greek/Cyrillic) coverage — confirmed by `dejavu-fonts` upstream README.
  - Pandoc → html5 default output **does NOT include `<style>` block** unless `--standalone` is passed; pandoc 2.17 with `-o output.html` defaults to a minimal HTML fragment. WeasyPrint then uses its built-in default CSS (which sets `font-family: serif`) which fontconfig resolves to DejaVu Serif (or Noto Serif) on bookworm with `fonts-dejavu-core` installed.
  - **Spec apt-deps issue.** Spec lists `libcairo2 + libpango-1.0-0 + libpangoft2-1.0-0 + libgdk-pixbuf2.0-0 + fonts-dejavu-core`. WeasyPrint's official "First Steps" guide lists ONLY: `libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0`. Source: <https://doc.courtbouillon.org/weasyprint/stable/first_steps.html>.
    - `libcairo2` — NOT required by WeasyPrint v53+ (removed in favour of pure-pango rendering).
    - `libgdk-pixbuf2.0-0` — NOT required (image decoding moved to Pillow).
    - `libharfbuzz-subset0` — REQUIRED (used for font subsetting in PDF embedding) — **MISSING from spec list**.
    - `fonts-dejavu-core` — Recommended for Cyrillic but not strictly required (any font would do; DejaVu is conventional).

- **Spec change required (v3):**
  - §Wave A apt list: replace
    ```
    pandoc libcairo2 libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf2.0-0 fonts-dejavu-core
    ```
    with the WeasyPrint-official set:
    ```
    pandoc libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core
    ```
  - Document: "WeasyPrint 63+ uses pure-Pango rendering — `libcairo2` and `libgdk-pixbuf2.0-0` are no longer required. `libharfbuzz-subset0` is required for embedded-font subsetting in the output PDF."

### R1.5 — openpyxl 3.1.x: header bold + write_only mode

- **Verified:** **PASS** (with caveat that spec already accepts).
- **Evidence:**
  - `openpyxl.styles.Font(bold=True)` is the documented constructor; `bold=False` is the default. Source: <https://openpyxl.readthedocs.io/en/stable/styles.html>.
  - `WriteOnlyCell` accepts `font` attribute assignment (`cell.font = Font(bold=True)`). Source: <https://openpyxl.readthedocs.io/en/stable/optimized.html>.
  - **Column-width caveat (spec already correct):** WriteOnlyWorksheet does NOT support `column_dimensions[col].width` — confirmed by typeshed issue #11706 and openpyxl design docs. Spec §2.8 step 3 already says "Column widths auto-fit skipped в write_only mode (openpyxl ограничение); owner может расширить columns в Excel" — this is accurate.
  - The "max(len(cell)) + 2" formula appears in community recipes (with `* 1.2` multiplier in some) but is NOT a built-in feature even in standard mode. Excel's column width unit is "characters of the default font", which is a nontrivial conversion (kerning differs per char + Cyrillic averages wider than Latin). Spec's existing position (skip auto-fit in v1) is the right call.

- **Spec change required (v3):** none.

### R1.6 — `asyncio.to_thread` cancellability

- **Verified:** **PASS** (spec's honesty paragraph at §2.12 (iii) is accurate).
- **Evidence:**
  - Python 3.12 docs <https://docs.python.org/3.12/library/asyncio-task.html#asyncio.to_thread>: documentation describes only argument forwarding and `contextvars.Context` propagation. **No mention of cancellation propagation** to the underlying thread.
  - Implementation detail (since 3.9): `asyncio.to_thread` is a thin wrapper around `loop.run_in_executor(None, func)` against the default `ThreadPoolExecutor`. Cancelling the awaiting coroutine **only** cancels the future-level wait; the worker thread continues running until `func` returns naturally. Python threads cannot be forcibly interrupted from outside (PEP 8: no `Thread.kill()`).
  - Spec §2.12 (iii) explicitly accepts this: WeasyPrint thread orphan after timeout, OS reaps on process exit. This is documentation-correct.

- **Alternative considered:** `concurrent.futures.ProcessPoolExecutor`. Pros: cancellable via `future.cancel()` after process kill. Cons: (a) WeasyPrint's `HTML(string=...)` has GiB-class memory tied to that process — IPC round-trip overhead for `string=html` is bounded but the result PDF bytes are MiB-sized; (b) cold-start of a process worker for each render kills latency; (c) cairo/pango/fontconfig cache must be re-warmed in each subprocess. **Verdict**: the orphan-thread-on-stop residual risk (only triggered when `Daemon.stop()` runs DURING active render) is cheaper than process-pool overhead. Spec's choice is correct.

- **Spec change required (v3):** none. The honesty paragraph is well-written.

### R1.7 — Pandoc subprocess signal propagation

- **Verified:** **PASS** (spec §2.12 (ii) signal recipe is correct).
- **Evidence:**
  - Python 3.12 docs <https://docs.python.org/3.12/library/asyncio-subprocess.html>: `Process.wait()` and `Process.communicate()` documentation does NOT specify automatic signal delivery to the child on cancellation. The convention (and only correct pattern) is **explicit `proc.terminate()` / `proc.kill()`** in an exception handler.
  - Spec recipe in §2.12 (ii):
    ```python
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=settings.pandoc_sigterm_grace_s)
        except asyncio.TimeoutError:
            proc.kill()
            ...
        raise
    ```
    This is the canonical pattern (used by curio, trio's process API equivalents, asyncio cookbook). Sigterm grace before sigkill is correct.

- **Spec change required (v3):** none.

---

## Tier 2 verifications

### R2.8 — Pandoc cold-start latency

- **Verified:** PASS. Spec's 200-400ms estimate is consistent with community benchmarks.
- **Evidence:** GitHub issue jgm/pandoc#4226 shows 50× `__HelloWorld__` runs in ~12.6s on Pandoc 2.0 = **~250ms per invocation**. Pandoc 2.17 has the same Haskell runtime startup overhead (no major perf regression since 2.0). On Debian bookworm with cold filesystem cache, first invocation may hit 400-500ms; warm cache 100-200ms.
- **5s budget assessment:** spec claims `pdf_pandoc_timeout_s=20` is sufficient; with cold start ~500ms + actual conversion of 1MiB markdown ~1-3s, 20s is conservative. Even if owner's first render after deploy is slow (page cache cold), 5s wall-clock to "first byte received" is realistic.
- **Spec change required:** none.

### R2.9 — WeasyPrint memory profile

- **Verified:** Plausible but possibly under-estimated. Community reports range 500MB-2GB for 50-page documents.
- **Evidence:**
  - WeasyPrint issue #671: "consumes a lot of memory for long documents" — 50-page report ~1.4GB peak.
  - WeasyPrint issue #1950: "Memory heavy to render a 900kB pdf"
  - WeasyPrint issue #1104: "5000-row table → high RSS"
  - WeasyPrint issue #1496/2130/611: known memory leak across multiple renders (RSS grows 20-40MB per render, doesn't release).
- **Implications for spec:**
  - `max_input_bytes=1MiB` is reasonable upper bound (spec already enforces).
  - `pdf_max_bytes=20MiB` (post-render) is also reasonable — WeasyPrint output for 1 MiB markdown is typically <5 MB; 20 MB cap leaves headroom.
  - **However**, spec's HIGH-risk-row-§ "1MiB markdown can produce 50+ page PDF, 500MB-1GB peak RSS" UNDER-estimates the worst case. With render_max_concurrent=2, two simultaneous renders could hit 4 GB RSS. VPS RAM should be ≥ 4 GB to safely run 2 concurrent. Existing config validator at §2.9 already warns when `200 + concurrent*400 > 1024` — but 400 MB per slot may itself be optimistic.
- **Spec change required (v3 — non-blocking):** bump per-slot estimate in validator from 400MB to 700MB to better match observed worst-case (still warns at concurrent=2 on a 1GB VPS, doesn't error). Alternative: add memory_profiler-based assertion in Wave A spike to derive a real number from a synthetic 50-page input.

### R2.10 — WeasyPrint pin range

- **Verified:** PASS. Pin `weasyprint>=63,<70` is correct for spec's intended deprecation runway.
- **Evidence:**
  - Latest stable: **68.1** (Feb 2026).
  - Breaking change history 63→68:
    - 63.0 (Oct 2024): Python 3.13 supported, pydyf 0.11+, tinycss2 1.4+, tinyhtml5 2.0 (replaced html5lib)
    - 64.0: eInvoices, text/speed
    - 65.0 (Mar 2025): CSSSelect2 0.8.0 dependency
    - 67.0 (Dec 2025): Python 3.10+ required (drops 3.9)
    - **68.0 (Jan 2026): `default_url_fetcher` deprecated** (CVE-2025-68616), DocumentMetadata change. **69.0 will REMOVE legacy fetcher.**
  - Spec project requires `requires-python = ">=3.12,<3.13"` — compatible with all of 63-68.
  - Source: <https://doc.courtbouillon.org/weasyprint/stable/changelog.html>.
- **Spec change required:** none. (R1.2 already captures the migration path within this pin range.)

### R2.11 — Docker layer size delta

- **Verified:** **FAIL.** Spec budgets ≤95 MB; actual delta exceeds 200 MB just for direct apt deps.
- **Evidence (apt Installed-Size kB on amd64, Debian bookworm; ×1024 → bytes):**
  | Package | Direct Installed-Size (kB) |
  |---|---|
  | pandoc | **168,399** (~164 MiB) |
  | libpango-1.0-0 | 520 |
  | libpangoft2-1.0-0 | 142 |
  | libharfbuzz-subset0 | 2,577 |
  | fonts-dejavu-core | 2,960 |
  | (transitive: libcairo2, libpangocairo-1.0-0, libfontconfig1, libfreetype6, libgraphite2-3, libpixman-1-0, etc.) | ~10,000 (estimate) |
  | **Direct sum** | **~174 MiB** |
  | **With transitives** | **~185-220 MiB** |
- Pandoc alone is **~164 MiB installed**, blowing past the 95 MB budget by 70+ MiB. The Haskell static binary is huge.
- **Mitigation options:**
  1. **Accept the new floor.** Update spec budget from "≤95 MB" to "≤220 MB" and document that pandoc is the dominant contributor (~75% of delta). The compose stack already pulls 400 MB of `claude` ELF — another 200 MB doesn't change deployment economics much.
  2. **Drop pandoc, use Python-only stack.** WeasyPrint can render PDF directly from HTML; markdown→HTML can be done with `markdown` library (pure Python, ~200 KB). Loss: DOCX path requires a different library (e.g. `python-docx` for native generation OR `pypandoc` calling a smaller pandoc binary). Devil-w1's reasoning for keeping pandoc was DOCX quality; reopening this trade-off is in scope for v3.
  3. **Use `pandoc-cli` slim package if available** — Debian doesn't split pandoc this way; only `pandoc` (full).
- **Spec change required (v3 — high priority):**
  - §HIGH risk row "Docker image size" — bump budget to **≤230 MB** apt delta, document pandoc as 75% contributor.
  - OR (preferable for VPS RAM/disk hygiene): re-evaluate the pandoc dependency. Since WeasyPrint is Python and openpyxl is Python, only DOCX needs pandoc. If DOCX renderer is moved to `python-docx` direct generation (pure-Python wheel, already in pyproject.toml), pandoc dep can be dropped entirely → image delta drops to ~6 MB. **This is a substantial spec re-scope** and should be raised as Q for owner before coder phase.

### R2.12 — PDF magic bytes check

- **Verified:** PASS with caveat.
- **Evidence:**
  - PDF 1.7 spec (ISO 32000-1) requires `%PDF-` at byte 0 followed by version `1.x` and `%%EOF` at file end. WeasyPrint generates compliant output.
  - PDF spec ALSO permits binary marker bytes (4 high-bit bytes after the header) so file-detection tools treat it as binary. WeasyPrint's output starts with literal `%PDF-1.7\n%` then 4 binary bytes — `path.read_bytes()[:5] == b"%PDF-"` is the correct check.
  - `%%EOF` end check: WeasyPrint always appends; some PDF generators add trailing bytes after EOF. `path.read_bytes()[-6:].rstrip() == b"%%EOF"` is the more lenient check.
- **Spec change required:** AC#15a should specify "starts with `%PDF-`" (5-byte prefix, NOT 8-byte `%PDF-1.7`) to survive future PDF version bumps in WeasyPrint output.

---

## Tier 3 sanity

### R3.13 — Markdown table parser

- **Recommendation:** Use the `markdown` Python library's built-in `tables` extension (or `markdown-it-py` with `tables` plugin) rather than custom regex. CommonMark grandfather rules around escaped pipes (`\|`) and alignment markers (`:---`, `---:`, `:---:`) are nontrivial; community implementations have battle-tested edge cases.
- **Pros of using a library:**
  - Handles `\|` escaping automatically.
  - Honours alignment markers correctly.
  - Mistune (recommended) returns AST you can walk for table-only extraction.
- **Cons:** new dependency. `markdown` is ~250KB pure Python; `mistune` is ~100KB. Already-in-pyproject.toml libs (pypdf, openpyxl) don't help here.
- **Verdict:** **Recommend** custom regex remains acceptable IF Wave B includes ≥10 unit-test cases covering: escaped pipes, leading/trailing whitespace per cell, align markers, ragged rows (fewer cells than header). Otherwise add `mistune>=3,<4` (already MIT-licensed, popular, ~100KB wheel). Spec should make this trade-off explicit.

### R3.14 — Audit log truncation at 256 codepoints

- **Verified:** PASS. Python 3 `str` indexing IS codepoint-based. `s[:256]` truncates to 256 codepoints. Confirmed by `sys.maxunicode == 1114111` (full Unicode 21-bit codespace).
- **Caveat:** if log is then encoded to UTF-8 for storage/transport, a truncated string may produce up to **4× as many bytes** (256 codepoints × 4 bytes/cp = 1024 bytes worst case for emoji). Audit infrastructure should accept variable-byte rows. Spec doesn't claim fixed-byte rows so this is fine.

### R3.15 — `uuid.uuid4()` for artefact filenames

- **Verified:** PASS. `uuid.uuid4()` produces a 122-bit random value; collision probability after 1 billion artefacts ≈ 5×10⁻²⁰. Negligible.
- **Note:** spec also enforces `_sanitize_filename` precise rule (CRIT-5 closure §2.4) for the `suggested_filename` field that's user-visible; the `<uuid>.md` staging filename is internal-only. Both are sound. `secrets.token_hex(16)` (128 bits, base16) is a marginally tighter choice with no `time-low` exposure but the difference is academic for staging files that live <30 seconds. **Verdict:** keep `uuid.uuid4()`.

---

## Library version pins (final, verified as of 2026-05-02)

| Component | Spec pin | Verified status | Recommendation |
|---|---|---|---|
| `pandoc` (apt) | "current 3.x" (spec text wrong) | **2.17.1.1-2~deb12u1** on Debian bookworm | Update spec text to "as packaged in Debian bookworm (currently pandoc 2.17)". Behaviour identical to 3.x for our usage. |
| `weasyprint` (pip) | `>=63,<70` | Latest stable: 68.1 | **Pin OK** but migrate to `URLFetcher` subclass (R1.2) to survive the 69.0 deprecation removal. Test must use `FatalURLFetchingError` not `URLFetchingError`. |
| `openpyxl` (pip) | `>=3.1.5,<4` (already in pyproject.toml) | API verified for write_only + Font(bold=True) | **OK as-is.** |
| `aiogram` (pip) | `>=3.26,<4` (already in pyproject.toml) | FSInputFile + send_document API verified | **OK as-is.** Spec's `bot.send_document(chat_id, file, caption=...)` works but rename `file → document` to match parameter name. |
| `libpango-1.0-0` + `libpangoft2-1.0-0` (apt) | listed in spec | Required by WeasyPrint | **OK.** |
| `libharfbuzz-subset0` (apt) | **MISSING from spec list** | Required by WeasyPrint 63+ for font subsetting | **Add to apt deps.** |
| `libcairo2` (apt) | listed in spec | NOT required by WeasyPrint 63+ | **Remove from apt deps.** |
| `libgdk-pixbuf2.0-0` (apt) | listed in spec | NOT required by WeasyPrint 63+ | **Remove from apt deps.** |
| `fonts-dejavu-core` (apt) | listed | Recommended for Cyrillic | **OK** (any Unicode font with Cyrillic coverage suffices; `fonts-noto-core` is alternative ~12MB). |

---

## Open questions for owner / coder phase

1. **Q (owner-decision)**: Re-evaluate pandoc dependency in light of R2.11. Pandoc adds ~164 MiB to the runtime image. If owner is OK with `python-docx` direct generation for DOCX (no pandoc), the entire pandoc apt dependency drops out. PDF path can keep pandoc OR use `markdown`/`mistune` Python lib for md→HTML. **Decision needed before Wave A**: keep pandoc, or pivot to all-Python? Current Spec keeps pandoc; researcher position is "owner-call, both viable; all-Python is cleaner but DOCX quality matters".

2. **Q (clarification)**: Wave A spike for pandoc subtraction effectiveness should EMPIRICALLY verify, not just trust the doc. Recipe:
   ```bash
   echo '<script>alert(1)</script>' | pandoc -f 'markdown-raw_html' -t html5
   # Expected output: literal escaped text, not <script> tag
   echo '$E=mc^2$' | pandoc -f 'markdown-tex_math_dollars' -t html5
   # Expected output: literal '$E=mc^2$', not MathML
   ```
   Wave A A2 (smoke test) should include both assertions.

3. **Q (low-priority)**: spec §2.13 in-flight artefact ledger — researcher did not deeply audit for race conditions (out of brief). Reviewer phase should look for TOCTOU between `mark_delivered` and sweeper.

4. **Q (apt Installed-Size accuracy)**: numbers above are direct sizes. Real `apt-get install` adds transitive deps (libcairo2 might still come in via libpangocairo-1.0-0!). Wave A A8 (image-size CI gate) should `dpkg-query -W -f='${Installed-Size}\n'` after install and report the actual delta. Don't trust pre-baked estimates.

---

## Key references

1. Pandoc Extensions source (3.9.0.2): <https://hackage-content.haskell.org/package/pandoc-3.9.0.2/docs/src/Text.Pandoc.Extensions.html> — definitive list of default-enabled extensions in markdown flavour.
2. Pandoc User Manual: <https://pandoc.org/MANUAL.html> — `+EXT`/`-EXT` syntax + extension descriptions.
3. WeasyPrint urls.py source: <https://github.com/Kozea/WeasyPrint/blob/main/weasyprint/urls.py> — definitive `URLFetchingError` (IOError, swallowed) vs `FatalURLFetchingError` (BaseException, abort).
4. WeasyPrint First Steps: <https://doc.courtbouillon.org/weasyprint/stable/first_steps.html> — official apt deps list (libpango + libharfbuzz-subset, NO cairo).
5. WeasyPrint Changelog: <https://doc.courtbouillon.org/weasyprint/stable/changelog.html> — version history; 68.0 deprecates default_url_fetcher.
6. WeasyPrint API Reference: <https://doc.courtbouillon.org/weasyprint/stable/api_reference.html> — URLFetcher class + URLFetcherResponse + FatalURLFetchingError.
7. aiogram FSInputFile: <https://docs.aiogram.dev/en/latest/api/types/input_file.html> — constructor signature.
8. aiogram send_document: <https://docs.aiogram.dev/en/latest/api/methods/send_document.html> — method parameters (`document`, not `file`).
9. openpyxl optimized: <https://openpyxl.readthedocs.io/en/stable/optimized.html> — write_only mode.
10. openpyxl styles: <https://openpyxl.readthedocs.io/en/stable/styles.html> — Font(bold=True).
11. Python 3.12 asyncio.to_thread: <https://docs.python.org/3.12/library/asyncio-task.html#asyncio.to_thread> — silent on cancellation = thread keeps running.
12. Python 3.12 asyncio subprocess: <https://docs.python.org/3.12/library/asyncio-subprocess.html> — caller responsibility for terminate/kill on cancel.
13. Debian bookworm package metadata:
    - pandoc: <https://packages.debian.org/bookworm/pandoc> (2.17.1.1, 168,399 kB amd64)
    - libcairo2: <https://packages.debian.org/bookworm/libcairo2>
    - libpango-1.0-0: <https://packages.debian.org/bookworm/libpango-1.0-0>
    - libharfbuzz-subset0: <https://packages.debian.org/bookworm/libharfbuzz-subset0> (2,577 kB amd64)
    - fonts-dejavu-core: <https://packages.debian.org/bookworm/fonts-dejavu-core> (2,960 kB)
14. Pandoc perf benchmark: <https://github.com/jgm/pandoc/issues/4226> — 2.x cold-start ~250ms.
