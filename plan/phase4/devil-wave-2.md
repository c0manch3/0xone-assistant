# Phase 4 Devil's Advocate — Wave 2

> Attack surface on the **patched** description-v2.md (post-researcher).
> Deliberately does not duplicate wave-1; those items are assumed addressed
> by the eight patches. This wave pokes at the fixes themselves, the new
> surface they introduced, and the areas the researcher bypassed.

## Executive summary

Researcher's core claims reproduce on my machine (100% recall on RQ2 corpus;
write-time sentinel rejection; IsoDateLoader round-trips seed notes; FTS5
external-content does NOT double storage — 2.4 MB for 100 × 12 KB notes).

But the patched plan still carries **four BLOCKER-severity defects** that
will bite during phase-4 build or immediately after ship:

1. **C2.1 — Mount parser splits mount points with spaces.** `rest.split(' ', 1)`
   on `/Volumes/Google Chrome (hfs, ...)` gives `mp='/Volumes/Google'`. Any
   vault at a space-containing mount point reports the wrong FS type — or
   worse, `is_relative_to` fails and we silently fall back to `'unknown'`.
   Reproduced live on owner's machine (has `/Volumes/Google Chrome`).
2. **C2.2 — Surrogate-containing bodies crash write path.** `ensure_ascii=False`
   lets `json.dumps` emit lone surrogates, but the subsequent `.encode('utf-8')`
   inside `f.write()` raises `UnicodeEncodeError: surrogates not allowed`.
   Attacker/buggy-model can paste `\ud83c` via Telegram paste-binary and
   crash `memory_write`.
3. **C2.3 — First-boot auto-reindex dereferences `conn`/`log` that don't
   exist in `configure_memory`.** Patched §D calls
   `_maybe_auto_reindex(vault_dir, conn, log)` but `configure_memory`
   signature has no `conn`/`log` — it calls `_ensure_index(...)` which
   *opens and closes* its own connection. Coder will either invent a
   parameter, or the helper will be called with wrong args on first
   attempt. Also: the helper acquires `fcntl.flock` — and it's called
   FROM `configure_memory` on every daemon start — which blocks startup
   if the prior process crashed holding the lock (until kernel reaps fd).
4. **C2.4 — Auto-reindex Policy B misses Obsidian external edits entirely.**
   Researcher's `_count_eligible_on_disk` compares count only. User edits
   an existing note body in Obsidian (mtime changes, path stays) → count
   match → reindex skipped → FTS5 serves stale content forever. Plan's
   R-section notes this is "the common case" but the fix doesn't address
   it. On a real-world Obsidian-edited vault this is the FIRST failure
   mode, not a corner case.

One HIGH-severity finding is worth calling out up top: **H2.5 — researcher's
nonce-sentinel regex still matches any `[0-9a-f]+` suffix**, so attacker
doesn't need to guess the nonce — the regex accepts ANY hex substring as
a valid "nonce" close tag. The nonce protects the model from pre-crafted
payloads, but the SCRUB regex will still correctly neutralize them on
read. Confirmed via spike: `</untrusted-note-body-aaaa>` matches. So the
three-layer defense does hold, but the nonce's claimed benefit (payloads
"non-portable") is overstated.

Distribution: **4 CRITICAL · 8 HIGH · 7 MEDIUM · 4 LOW · 6 assumptions**.
Coder is **BLOCKED** on C2.1, C2.2, C2.3, C2.4 until patched.

---

## CRITICAL (block coder start)

### ID-C2.1. `mount` output parser breaks on mount points with spaces
- **Location:** §J R8 patched code sketch; spike `rq3_fs_type.txt` L63-L87.
- **Claim:** `rest.split(' ', 1)` extracts mount point; `re.search(r'\(([a-z0-9]+)`, tail)` extracts FS type.
- **Attack (reproduced live on owner's Darwin 24.6):**
  ```
  /dev/disk4s2 on /Volumes/Google Chrome (hfs, local, ...)
  rest.split(' ', 1) → mp='/Volumes/Google', tail='Chrome (hfs, ...)'
  ```
  `mp` is truncated. The researcher's longest-prefix `target.relative_to(mp)`
  check will **succeed** against `/Volumes/Google` for any `/Volumes/Google/*`
  path — but will **miss** `/Volumes/Google Chrome/vault/*` entirely (since
  `Chrome` is in tail, not mp). Result: legitimate HFS vault at the real
  mount reports `fs='unknown'` and the warning path never fires.
  Worse: if user has an external exFAT drive mounted at `/Volumes/My Drive`
  and points `MEMORY_VAULT_DIR` there, the UNSAFE detection fails open —
  owner gets silent flock no-ops, corrupt index.
- **Proposed fix:** Replace the two-step split with a single regex:
  ```python
  m = re.match(r"^(.+?) on (.+?) \(([a-z0-9]+)", line)
  if m:
      mp, fs = m.group(2), m.group(3)
  ```
  `.+?` is non-greedy so it stops at the *last* ` on ` + space-before-paren —
  though mount points with ` on ` in their name is pathological. Safer still:
  `r"^.+? on (.+) \(([a-z0-9]+)"` with greedy match that consumes the whole
  mount-point including spaces, then paren marks the fs-type boundary.
  Write explicit test: `/Volumes/Google Chrome`, `/Volumes/My Book`,
  `/Users/agent2/Obsidian Vaults/Personal`.

### ID-C2.2. Body containing lone surrogate crashes `memory_write` at `.encode('utf-8')`
- **Location:** §K "All JSON serialization uses `ensure_ascii=False`"; §B.3 step 7 atomic write (writes encoded body to file); M6 patch.
- **Claim:** `ensure_ascii=False` fixes Cyrillic FTS; no surrogate handling mentioned.
- **Attack (reproduced):**
  ```python
  body = 'hello \ud83c'  # unpaired high surrogate
  json.dumps({'body': body}, ensure_ascii=False)
  # → '{"body": "hello \ud83c"}'   (OK)
  ...encode('utf-8')
  # → UnicodeEncodeError: 'utf-8' codec can't encode character '\ud83c' in position 16: surrogates not allowed
  ```
  A Telegram message with a broken emoji (client-side keyboard glitch, or
  an attacker sending raw `\uD83C` escape via a markdown paste) reaches the
  model; model writes it verbatim; `memory_write` crashes **after** the
  collision+validation checks pass — error surfaces as `(code=4)` vault IO,
  owner thinks it's a disk issue.
- **Proposed fix:** In `sanitize_body` (after size check), also enforce UTF-8
  encodability:
  ```python
  try:
      body.encode('utf-8')
  except UnicodeEncodeError as e:
      raise ValueError(f"body contains un-encodable surrogate at offset {e.start}")  # (code=3)
  ```
  Add test `test_memory_write_rejects_surrogate_body` with `\ud83c` payload.
  Also add to §B.3 step 3 description.

### ID-C2.3. `_maybe_auto_reindex` signature mismatch with `configure_memory`
- **Location:** §D patched snippet (`_maybe_auto_reindex(vault_dir, conn, log)`); §L RQ6 helper stub.
- **Claim:** Helper called at end of `configure_memory`.
- **Attack:** `configure_memory(*, vault_dir, index_db_path, max_body_bytes)`
  has NO `conn` param. `_ensure_index` (per installer precedent) opens a
  short-lived connection internally. RQ6's `_maybe_auto_reindex(vault, db, log)`
  takes a live connection. Where does `conn` come from? Either:
  - (a) `configure_memory` opens a persistent module-level `_CONN` — adds
    state, breaks WAL-readers-during-writers assumption, complicates
    `reset_memory_for_tests`.
  - (b) Helper opens its own connection each call — then we lose the
    "idempotent" property (re-opening races with other writes).
  - (c) Every memory tool call checks "has vault been auto-reindexed?"
    — adds a per-call overhead.
  The researcher didn't pin this. Coder will pick whichever is easiest and
  silently diverge from the plan.
- **Follow-on problem:** The helper takes `fcntl.flock` (it calls
  `_reindex_vault` which must lock for DELETE+INSERT). Daemon startup now
  blocks on flock acquire. If prior daemon crashed mid-reindex without
  unwinding the flock (fd leaked into a zombie), startup hangs. No timeout
  on `fcntl.LOCK_EX` by default.
- **Proposed fix:**
  - Pin signature: `_maybe_auto_reindex(vault_dir: Path, index_db_path: Path, log) -> None`.
    Helper opens its own short-lived connection, does the count compare
    read-only (no lock needed), and only acquires flock IF it's going to
    reindex.
  - Use `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on startup auto-reindex —
    fail open with a warning if the lock is held. Subsequent
    `memory_reindex()` calls can block normally.
  - Explicit AC in §G: "daemon start with a held `.lock` file logs
    warning, does not block".

### ID-C2.4. Auto-reindex Policy B silently serves stale content on external edits
- **Location:** §D `_maybe_auto_reindex`; RQ6 logic ("if `disk == idx`: return").
- **Claim:** "Staleness-gated on count mismatch ... covers accidental index delete."
- **Attack:** The primary use case for MEMORY_VAULT_DIR is pointing at an
  Obsidian vault the user edits externally. User edits
  `projects/flowgent.md` body in Obsidian (count unchanged, mtime changed).
  Daemon restarts. `_count_eligible_on_disk == idx_count` → reindex SKIPPED
  → `memory_search` hits the old body forever. Owner cannot distinguish
  "stale" from "correct" hit. **This is the normal edit path**, not a
  corner case.
  Current plan says "manual `memory_reindex` only" as non-goal but that
  conflicts with the seed-vault-works-out-of-box UX promise: owner opens
  Obsidian, fixes a note, restarts daemon, asks bot → wrong answer.
- **Proposed fix:** Track max `st_mtime_ns` of `.md` files in the vault at
  the DB level:
  ```
  CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
  -- on reindex: INSERT OR REPLACE INTO meta VALUES ('max_mtime_ns', '1745...');
  ```
  `_maybe_auto_reindex` compares `max(st_mtime_ns)` of eligible FS files
  against stored value. If stored missing OR FS max > stored → reindex.
  Cheap (one `rglob+stat` pass, no open), addresses the real failure mode.
  Add AC #9: "edit a seed note externally, restart daemon, search returns
  NEW body not old."

---

## HIGH

### ID-H2.1. Single-char query tokens pass through unfiltered, matching huge result sets
- **Location:** §B.1 `_build_fts_query`; RQ2 spike does not exercise short queries.
- **Claim:** Recall 100% on researcher's 22-case corpus.
- **Attack (reproduced live):**
  ```python
  stemmer.stemWord('я') → 'я'   # 1 char stem
  → FTS5 query: 'я*'
  ```
  On a real vault, `я*` matches every token starting with `я` (я, яблоко,
  январь, яндекс, ясно, ...). On a 500-note personal vault, this returns
  hundreds of hits; `limit=10` crops but ORDER BY rank is unpredictable at
  that density. Same for `а*`, `и*`, `о*`. One-character queries from user
  are rare but the model is *trained* to try short queries as a first pass
  ("я люблю..." → тoken `я`).
  Tested against corpus of 6 notes: `я*` returned 3 unrelated hits.
- **Proposed fix:** In `_build_fts_query`, skip stemming for Cyrillic tokens
  of length < 3; drop them entirely (don't add to query); if ALL tokens
  drop, raise the empty-query `(code=5)`. Researcher acknowledged this as
  "Mitigation" in RQ2 downsides but never committed it to the patch.
  Pin in §B.1: "tokens with `len(stem) < 3` are skipped".

### ID-H2.2. PyStemmer over-stems imperative verb → matches nouns with shared prefix
- **Location:** §B.1 stem-and-wildcard transform.
- **Claim:** 100% recall, 100% precision on RQ2 corpus.
- **Attack:** `stemmer.stemWord('запомни')` → `'запомн'` → query `запомн*`
  matches `запомнил`, `запомнить`, `запомнишь` — good. But also matches
  `запомнённый`, `запомнив`, AND any hypothetical noun with stem `запомн`.
  More critically: `stemmer.stemWord('работа')` → `'работ'` → `работ*`
  matches `работать`, `работаю`, `работает` — but also `работник`,
  `работодатель`, `работоспособность`. False-positive density depends on
  vault content; RQ2 tested on 25 cases, not 500.
- **Proposed fix:** Acknowledged as a known trade-off in §H debt. Add test
  `test_memory_search_false_positive_density` that builds a 100-note synthetic
  corpus with known noise; assert precision doesn't drop below 80%.

### ID-H2.3. Nonce regex accepts any hex substring → nonce does not actually prevent injection
- **Location:** §J R1 patched; RQ5 `_SENTINEL_RE = r"</?\s*untrusted-note-(?:body|snippet)(?:-[0-9a-f]+)?\s*>"`.
- **Claim:** "Nonce makes prompt-injection payloads non-portable (attacker
  can't pre-craft a close tag for a nonce they haven't seen)."
- **Attack (reproduced):** The scrub regex catches ANY `-[0-9a-f]+` suffix.
  So `</untrusted-note-body-aaaa>` is matched and ZWSP-scrubbed on READ.
  The scrub does its job — but the NONCE provides no additional security,
  because the actual cage-break defense is **the scrub**, not **the nonce**.
  The claimed "payloads non-portable" property is illusory: attacker
  writes `</untrusted-note-body-aaaa>` → scrub ZWSPs it → they can't break
  cage. But the same is true without the nonce. The nonce only helps if
  the scrub fails (regex miss); in that case the ATTACK succeeds too
  (because the regex miss means the match test itself missed).
- **Proposed fix:** Not a security blocker because the scrub does work.
  But the researcher's framing in RQ5 overclaims value. Recommend:
  - Keep the nonce (it's cheap, defense-in-depth).
  - Drop the `?` in `-[0-9a-f]+)?` making nonce-suffix REQUIRED to match —
    then legacy bodies with plain `</untrusted-note-body>` aren't scrubbed,
    but they're also no longer tag-shaped for the current call's cage.
    Actually this is safer: after write-time reject (layer 1), no NEW
    legacy tag can enter; legacy-in-scrub is belt-and-suspenders.
  - Update §J R1 wording: "Nonce prevents **forward-portable payloads
    encoded in transit tooling (logs, mirrors)**, not in stored notes."

### ID-H2.4. `datetime.time` and malformed dates crash IsoDateLoader
- **Location:** §K `parse_frontmatter`; RQ4 only tests `datetime.date`.
- **Claim:** Timestamp constructor returns datetime.date/datetime; coerced to ISO.
- **Attack (reproduced):**
  ```python
  yaml.load('time: 12:34:56', Loader=IsoDateLoader)
  # → {'time': 45296}   (INT, not str — YAML parses bare time as seconds)
  ```
  The `time: 12:34:56` YAML scalar resolves to `int` (seconds since midnight)
  because `tag:yaml.org,2002:timestamp` only fires on timestamps that
  *include* a date. Malformed date `2026-13-99`:
  ```
  ValueError: month must be in 1..12
  ```
  propagates up from `construct_yaml_timestamp` — `memory_read` crashes.
- **Proposed fix:** Wrap `construct_yaml_timestamp` in try/except; on
  ValueError, fall back to `str(node.value)`:
  ```python
  def _timestamp_as_iso(loader, node):
      try:
          val = yaml.SafeLoader.construct_yaml_timestamp(loader, node)
      except ValueError:
          return node.value  # raw YAML string
      return val.isoformat() if isinstance(val, (dt.date, dt.datetime)) else str(val)
  ```
  Also: add a `post_walk` step that coerces any remaining non-JSON-serializable
  values (int → int is fine; sets, bytes → str). Test matrix:
  `'time: 12:34'`, `'created: 2026-13-99'`, `'created: 2026-04-16T12:34:56+03:00'`,
  `'value: !!binary abc'`.

### ID-H2.5. Reordering: `memory_delete(confirmed=false)` returns code=10 BEFORE path validation
- **Location:** §B.5 `memory_delete(path, confirmed)`.
- **Claim:** "Require `confirmed=true` ... reduces accidental delete risk."
- **Attack:** Model calls `memory_delete(path='../../etc/passwd', confirmed=false)`.
  Plan returns `(code=10)` "not confirmed" — tells the attacker "set
  confirmed=true and I'll try it". Better: validate path FIRST, so even
  `confirmed=false` with bad path returns `(code=1)` and reveals nothing
  about the confirmation check.
  Security-through-obscurity, low severity, but cheap to fix.
- **Proposed fix:** In §B.5, pin order: validate path → if `not confirmed`
  return (code=10) → lookup → delete. Applies equally to `confirmed`-gated
  ops in other tools.

### ID-H2.6. `configure_memory` idempotent with different `max_body_bytes` — grandfathered notes
- **Location:** §D `"""Idempotent with same args, raises on re-config with different args."""`; §I Q8.
- **Claim:** Idempotence check; env override changes affect future writes.
- **Attack:** `MEMORY_MAX_BODY_BYTES=2097152` (2 MB) set in env. Daemon
  writes a 1.5 MB note. User lowers env to `1048576`. Daemon restart:
  `configure_memory` raises "re-config with different args" → daemon fails
  to start. OR: if researcher means "new cap applies only to future writes",
  old 1.5 MB note still sits in DB; `memory_read` returns full body; no
  problem. But `memory_write(overwrite=true)` on that path with same body
  → passes size check as `<= 1 MB`? NO — body is 1.5 MB → rejected →
  user cannot re-save their own note.
- **Proposed fix:**
  - Make `configure_memory` idempotent with `max_body_bytes=newer-value`
    (warn if changed, don't raise).
  - Document in §H: "lowering `MEMORY_MAX_BODY_BYTES` may make pre-existing
    oversized notes read-only; run `memory_reindex` does not help. Manual
    fix: truncate body in Obsidian."

### ID-H2.7. `ensure_ascii=False` + FTS `tags` column — field-scoped MATCH still quote-aware
- **Location:** §B.1 area filter (`AND area=:area`); M6 patch.
- **Claim:** Cyrillic tag matching works.
- **Attack:** Researcher verified `MATCH 'встреча'` against JSON-encoded
  list — I re-tested: yes, works because unicode61 strips brackets/quotes.
  **But:** model uses column-scoped form `tags:встреча` to disambiguate.
  FTS5 MATCH parses column-filter prefix before the tokenizer; if query
  token starts with `"` or `[`, parse fails. If the model constructs a
  column filter naively from user input containing Cyrillic → parse
  ambiguity.
  Secondary: the FTS `area` column is also tokenized (no phrase marker).
  Query `area:inbox` works but `area="inbox"` (user misformat) → parse fail.
- **Proposed fix:** Expose `area` ONLY through the separate `notes` mirror
  table (non-FTS) — the current plan already does that for `memory_list`
  but `memory_search`'s `AND area=:area` clause joins `notes` implicitly?
  Not pinned. Check: if `memory_search` does `WHERE notes_fts MATCH ... AND
  area=:area` — that requires `notes_fts` to have `area` col (it does per
  schema). Prefer: join on notes table for area filter:
  ```sql
  SELECT n.path, n.title, ... FROM notes n
  JOIN notes_fts f ON n.rowid = f.rowid
  WHERE f.notes_fts MATCH :q AND (:area IS NULL OR n.area = :area)
  ```

### ID-H2.8. FTS5 `snippet()` function called on external-content table — may not work
- **Location:** §B.1 `snippet(notes_fts, 4, '<b>', '</b>', '...', 32)`.
- **Claim:** Snippet returns matched-text context for the model.
- **Attack:** FTS5's `snippet()` on external-content tables requires the
  ORIGINAL TEXT to be reconstructible. Our external content is in `notes`
  mirror — which is fine as long as content-rowid mapping is intact. But
  if a write inserts into `notes` and fails trigger mid-way, `notes_fts`
  and `notes` can divergence (researcher's H2 patch addresses write ordering
  but doesn't verify `snippet()` still works after rollback).
  Also: `snippet()` column index is 4 (body). After schema add (e.g. phase-5
  adds `body_sha`), the column index shifts silently.
- **Proposed fix:**
  - Add regression test: after a forced rollback (mock `os.rename`), assert
    `snippet()` still returns sane output for other rows.
  - Replace positional `snippet(notes_fts, 4, ...)` with named column index
    resolved at startup from `PRAGMA table_info(notes_fts)`, or hardcode a
    `_BODY_COL_INDEX = 4` constant with a schema-drift assertion.

---

## MEDIUM

### ID-M2.1. `.obsidian/templates/*.md` slips through exclusion filter
- **Location:** §B.6 `_VAULT_SCAN_EXCLUDES = {".obsidian", ...}`.
- **Claim:** `any(part in excludes for part in rel.parts)` — any depth.
- **Attack:** Correct if coder implements "any depth". But if coder writes
  `rel.parts[0] in excludes` (common first attempt), `.obsidian/templates/meeting-template.md`
  slips through → indexed as a note → owner sees "Meeting template" when
  searching "встреча".
- **Proposed fix:** Pin the algorithm verbatim in §B.6 body:
  ```python
  parts = rel.parts
  if any(p in _VAULT_SCAN_EXCLUDES for p in parts):
      skip(...)
  ```
  Add test: `.obsidian/templates/foo.md`, `nested/.obsidian/foo.md`,
  `.trash/bar.md`, `projects/.git/refs/head.md`.

### ID-M2.2. `vault/.tmp/` staging directory creation unspecified
- **Location:** §C atomic-write section; §D `configure_memory` flow.
- **Claim:** `tmp = NamedTemporaryFile(dir=vault/.tmp/, ...)`.
- **Attack:** On first `memory_write` to a fresh vault, `vault/.tmp/` may
  not exist → `NamedTemporaryFile(dir=...)` raises `FileNotFoundError` →
  user sees `(code=4)` vault IO for their first write.
- **Proposed fix:** In `configure_memory`, after `vault_dir.mkdir(parents=True, exist_ok=True)`:
  ```python
  (vault_dir / ".tmp").mkdir(exist_ok=True)
  ```
  Add to §D explicitly.

### ID-M2.3. Global `Stemmer.Stemmer("russian")` vs module import order
- **Location:** §B.1 PyStemmer; researcher's code sketch uses module-level global.
- **Claim:** Cheap initialization, thread-safe (verified).
- **Attack:** My thread test confirmed `Stemmer.Stemmer` is thread-safe across
  8 threads × 400 calls. But: a naive coder might `Stemmer.Stemmer("russian")`
  **inside `_build_fts_query`** (per-call construction). At 500 searches,
  that's 500 C-extension constructor calls. Each takes ~0.1 ms — 50 ms
  wasted. Not a blocker, but performance smell.
- **Proposed fix:** Pin in §K `_memory_core.py` module docstring:
  ```
  `_STEMMER = Stemmer.Stemmer("russian")` at module scope. DO NOT
  instantiate per-call.
  ```

### ID-M2.4. Wikilinks heading-variant stripping incomplete
- **Location:** §B.2 fix ID-H6 `link.split("|",1)[0].split("#",1)[0]`.
- **Claim:** Handles alias + heading forms.
- **Attack:** Obsidian wikilinks have THREE special forms:
  - `[[note|alias]]` — alias
  - `[[note#heading]]` — heading ref
  - `[[note#heading|alias]]` — combined
  - `[[note^block-id]]` — block reference
  Researcher's fix handles `|` and `#` but not `^`. Block refs in seed
  notes don't exist yet but common in Obsidian.
- **Proposed fix:** `re.split(r"[#^|]", link, 1)[0]` — strips any of the
  three suffix markers. Add test with `[[projects/flowgent^intro|foo]]`.

### ID-M2.5. Integration test auth detection scope
- **Location:** §F `test_memory_integration_ask.py`; §H NH-20.
- **Claim:** "Skips gracefully if `claude` CLI not authenticated" via
  "pytest fixture probing trivial `query(max_turns=0)`".
- **Attack:** The test's auth probe fixture (per H10 fix) uses a live
  subprocess `query()`. That subprocess starts a child `claude` process,
  which uses the user's OAuth session — in CI this returns "auth required"
  fast, BUT on owner's workstation the REAL vault at
  `~/.local/share/0xone-assistant/vault/` is picked up by the test.
  Test writes notes with names like `integration-test-2026-04-21-nonce.md`
  — 30 runs later, vault has 30 pollution notes.
- **Proposed fix:** Integration test MUST use `tmp_path` fixture + override
  `MEMORY_VAULT_DIR` env var for the subprocess. Pin in §F:
  ```python
  def test_memory_integration_ask(tmp_path, claude_authed, monkeypatch):
      monkeypatch.setenv("MEMORY_VAULT_DIR", str(tmp_path / "vault"))
      monkeypatch.setenv("MEMORY_INDEX_DB_PATH", str(tmp_path / "idx.db"))
      # assert vault root != ~/.local/share/...
      ...
  ```
  Add fixture guard: `assert not str(tmp_path).startswith(os.path.expanduser("~/.local"))`.

### ID-M2.6. Seed-notes read path untested
- **Location:** §F test list; spike confirmed 9/12 seed notes have `datetime.date`.
- **Claim:** `test_memory_parse_frontmatter_seed_roundtrip` covers.
- **Attack:** Roundtrip test asserts parse doesn't crash. But:
  - Does `memory_read(path='projects/flowgent.md')` actually return
    `structured.frontmatter = {...str-coerced dates...}`?
  - Does `memory_list()` show the 9 seed notes with dates as strings?
  - Does `memory_search("flowgent")` return the seed note with snippet
    containing seed body text?
  Parse-only roundtrip doesn't verify the top-level tool outputs.
- **Proposed fix:** Add to §F:
  - `test_memory_read_seed_note_structured_output` — asserts all keys are
    JSON-serializable.
  - `test_memory_list_after_seed` — calls after `configure_memory` with
    seed vault, asserts count == 12 (or 9 after MOC exclusion).
  - `test_memory_search_seed_flowgent` — query "flowgent", assert at least
    one hit with path `projects/flowgent.md`.

### ID-M2.7. Tests don't cover `memory_search("")` / `"*"` / `"?"` queries
- **Location:** §F.
- **Claim:** Covered by "FTS5 parse error → code=5" surface.
- **Attack:** The `_build_fts_query` transform: empty query → empty tokens
  → raise ValueError(code=5). Good. But `memory_search("*")` → tokens=
  `[]` (re findall strips `*`) → (code=5). `memory_search("?")` → same.
  Model retries with diff punctuation; sees 5 errors in a row; gives up.
  Silent UX degradation.
- **Proposed fix:** Add test matrix `test_memory_search_degenerate_queries`:
  `""`, `"*"`, `"?"`, `"   "`, `"!!!"`, `"\n\t"`. All return `(code=5)`
  with helpful text "no searchable tokens".

### ID-M2.8. Area resolution precedence on nested paths
- **Location:** §B.3 step 5 "area inferred from path parent if omitted";
  wave-1 M4 → "pin `area = path.parts[0]`".
- **Claim:** Top-level directory only.
- **Attack:** The fix addresses the path-side but not the frontmatter-side
  conflict. Seed note `projects/studio44-workload-platform.md` may have
  `area: studio44` in frontmatter (nested subarea intent). Plan R9 says
  "path parent wins with warning". But log isn't visible to owner. After
  3 notes like this, vault has a cluster with `area='projects'` that owner
  mentally grouped as `area='studio44'`. `memory_list(area='studio44')`
  returns empty — surprise.
- **Proposed fix:** Pin: if frontmatter `area` is set AND doesn't match
  `path.parts[0]`, return `(code=7)` "area conflict; use overwrite=true
  to rewrite frontmatter OR move the file to `<area>/`". Forces the model
  to make the choice explicit.

---

## LOW

### ID-L2.1. `mount` command timeout is 3s — too tight on slow disks
- **Location:** RQ3 code sketch `subprocess.run(['mount'], ..., timeout=3)`.
- **Claim:** 3s enough.
- **Attack:** On a machine with network-mounted volumes, `mount` stats all
  mounts and can take >3s. Researcher's spike tested on a fresh APFS-only
  box. Timeout → `return 'unknown'` → no warning fires (fail-open for
  unknown types).
- **Proposed fix:** Bump to 5s and log.info on timeout so owner can
  diagnose a silent no-op warning later.

### ID-L2.2. `.gitignore` template not specified in phase-4 deliverables
- **Location:** §K files-to-edit list; wave-1 M5.
- **Claim:** Add `memory-index.db*` to `.gitignore`.
- **Attack:** Future phase-7 `git commit` of vault will include `.lock`,
  `.tmp/staging-*.md`, `.obsidian/` (if user imported). Phase 4 can ship
  a repo-level `.gitignore` but vault-level `.gitignore` (inside the vault
  dir) also matters once vault becomes a git repo in phase 7.
- **Proposed fix:** Add to §K: at `configure_memory` time, if vault root
  has no `.gitignore`, write one with:
  ```
  .tmp/
  .obsidian/
  *.sqlite*
  ```
  Commit in phase 7 picks up this file.

### ID-L2.3. Large reindex: `DELETE FROM notes` in single txn inflates WAL
- **Location:** §B.6; wave-1 H5 addressed by double-buffer but researcher
  disposition column leaves "accept-fix" with no visible patch.
- **Claim:** Double-buffer via swap-table.
- **Attack:** `CREATE TABLE notes_new AS SELECT * FROM notes WHERE 0` is
  structurally correct but doesn't carry FTS5 content rows. Then
  `DROP TABLE notes` + `ALTER TABLE notes_new RENAME TO notes` leaves
  `notes_fts` pointing at a stale rowid space. FTS5 external-content
  tables require `INSERT INTO notes_fts(notes_fts) VALUES('rebuild')`
  after swap.
- **Proposed fix:** Update §B.6 algorithm to explicitly call `'rebuild'`
  command post-swap. Add test measuring FTS5 hits after swap.

### ID-L2.4. Phase-8 vault-push to remote contains secrets
- **Location:** wave-1 unknown-unknowns "phase 8 OAuth gh push of vault".
- **Claim:** Out of phase-4 scope.
- **Attack:** Researcher didn't flag — but the plan's silent-save UX means
  model can and will save OAuth tokens it sees in conversation ("запомни
  что мой api key is sk-..."). Vault then contains those. Phase 8 pushes
  to remote. Re-flag now so phase 4 documents the expectation.
- **Proposed fix:** Add to §H:
  ```
  D7 | Vault may contain sensitive strings the model saved. Phase 4
       writes UNENCRYPTED. Phase 8 must (a) add secret-redaction hook
       to memory_write, OR (b) ship encrypted-at-rest vault, OR (c)
       push to private remote only. Defer decision but call out now.
  ```

---

## Unspoken assumptions (new)

- **Nonce collision probability is negligible.** True (2^-48 per call) —
  but the retry loop inside `wrap_untrusted_body` could run forever if a
  body contains many hex substrings (adversarial). Should bound retries
  to, say, 3 attempts then fall back to a large-random base64 nonce.
- **`mount` output format is stable across macOS versions.** Researcher
  tested on Darwin 24.6 only. Monterey (21.x), Sonoma (23.x), Sequoia
  (24.x), Tahoe (25.x) differ in spacing around `(`. Not verified.
- **SQLite `PRAGMA journal_mode = WAL` persists across reopen.** It does
  per docs, but if the DB file is opened read-only (e.g. search path),
  the pragma is ignored. Mixed-open-mode tests not in §F.
- **`.encode('utf-8')` of `ensure_ascii=False` JSON is the only byte
  serialization path.** If coder uses `open(..., 'w')` (default utf-8
  encoding with strict errors), surrogates fail there too. Need an
  earlier guard.
- **Structured output field `wikilinks` is consumed by the model, not the
  user.** Plan returns raw wikilinks. Model may echo them back into its
  text response to user, including MOC-file wikilinks — user sees Obsidian
  markup in chat. Cosmetic.
- **`fcntl.flock` is only used on the `.lock` file, not the DB itself.**
  True per plan. But WAL-mode DB itself creates sidecars `db-wal`,
  `db-shm`; if an editor or backup tool holds those, SQLite opens block.
  Not plan's bug but worth noting — Time Machine backup windows can hold
  the sidecar.

---

## Scope creep (new)

- **Staleness detection via mtime (C2.4 fix) tempts coder into "why not
  incremental reindex by mtime?".** Pre-flag: phase 4 does FULL reindex on
  mtime change. Incremental per-path reindex is phase-8 M2 deferral.
- **Surrogate rejection (C2.2 fix) tempts coder into "add full validation —
  reject C0 control chars too, CR/LF normalize, etc.".** Pre-flag: ONLY
  reject surrogates. Everything else is fair content.
- **Path-level `.obsidian` allowlist tempts coder into adding "read
  templates/ for the model to discover note schema".** Pre-flag: never.
- **Config file for allowlists/blocklists.** Coder may want a YAML config
  for `_VAULT_SCAN_EXCLUDES`. Pre-flag: module constant only.

---

## Unknown unknowns (wave-2 only)

- **Telegram keyboard state on Russian QWERTY / Cyrillic input.** User may
  type `жeны` (Latin `e` where Cyrillic `е` expected) — looks identical on
  screen but tokenizer + stem treat as two tokens `ж`/`еы` or similar.
  Not tested anywhere.
- **PyStemmer Russian Snowball algorithm version stability across
  PyStemmer releases.** `>=2.2,<4` spans major version; stem output may
  drift. Pin to `~=2.2` or snapshot the stemmer rules for AC corpus.
- **Obsidian plugin `Better Word Count` writes `words` key to frontmatter
  as INT.** Roundtrip-safe per IsoDateLoader but model sees `words: 4523`
  noise. Flag only.
- **Git commit on vault when `.md` file has `~/Library/Application
  Support/...` absolute path in body (common model paste).** Phase 7
  concern, but phase 4 can redact on write.
- **macOS Sandbox / SIP + `mount` subprocess.** If the daemon runs under
  launchd with tight sandbox, `mount` may fail. Owner runs unsandboxed now
  but phase 9 hardening may bite.
- **`Stemmer.Stemmer("russian")` return value when called with
  non-lowercase input.** Researcher pre-lowercases in the sketch. If coder
  skips that step, `stemmer.stemWord('Жены')` returns... uncertain —
  Snowball expects lowercase.
