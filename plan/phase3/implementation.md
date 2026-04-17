# Phase 3 — Implementation (spike-verified, 2026-04-17)

## Revision history

- **v1** (2026-04-17): initial after spike S1–S5. Empirical answers backed by
  `gh api /repos/anthropics/skills/contents/skills`, `git clone --depth=1
  https://github.com/anthropics/skills`, and a live SDK PostToolUse probe
  (`spikes/sdk_probe_posthook.py`; report in
  `spikes/sdk_probe_posthook_report.json`). Confirmed: (a) all 17 Anthropic
  skills have **no** `allowed-tools` in frontmatter -> B2 patch is mandatory;
  (b) `PostToolUse` hook fires reliably with `tool_input.file_path` set;
  (c) no symlinks and no top-level `tools/` subdir in any of the 17 bundles;
  (d) `sha256_of_tree` idempotent and flip-sensitive on the real
  skill-creator bundle.
- **v2** (2026-04-17): applied devil's advocate review — 6 blockers (B-1..B-6),
  8 strategic fixes (S-1..S-8), 4 security fixes (H-1..H-4). Key semantic
  changes: (1) empty `allowed-tools: []` in phase 3 **does NOT gate** (global
  baseline applies until phase 4) — previously misdocumented as "lockdown";
  (2) installer switched to **stdlib-only** (`urllib.request` + in-repo
  frontmatter parser) — kills the `uv sync` chicken-and-egg during bootstrap
  and removes a 20 MB virtualenv from data_dir; (3) `sha256_of_tree`
  excludes `.git/` + cache dirs (previously gave TOCTOU false-positive on
  every re-clone); (4) phase-2 Bash allowlist permits `python` / `uv run`
  under `skills/<name>/` too — Anthropic `skill-creator` ships `scripts/*.py`
  that would otherwise be denied (H-1 found during grep); (5) canonical URL
  drops query + fragment entirely (avoids token-in-disk exposure).

Companion docs (coder **must** read all four before starting):

- `plan/phase3/description.md` - 71-line summary.
- `plan/phase3/detailed-plan.md` - 800-line canonical spec. All section
  references below point into that file unless prefixed with "phase-2".
- `plan/phase3/spike-findings.md` - empirical answers with raw output.
- `plan/phase2/implementation.md` - phase-2 hook/manifest API (don't
  re-derive; reuse).
- `plan/phase2/summary.md` - state of bridge, hooks, ConversationStore after
  phase 2.

**Auth:** OAuth via `claude` CLI (`~/.claude/`). Do not introduce
`ANTHROPIC_API_KEY` anywhere in phase 3 code, config, or tests.

---

## 1. Verified decisions (S / B / R answers)

| # | Question | Answer | Source |
|---|---|---|---|
| S1.a | Skills lie at repo root? | No - under `/skills/<name>/`. Use `MARKETPLACE_BASE_PATH="skills"`. | `gh api /repos/anthropics/skills/contents/skills` returns 17 dirs. |
| S1.b | SKILL.md has `allowed-tools`? | **None** of 5 sampled (`skill-creator, pdf, docx, mcp-builder, claude-api`). `license:` optional. | `spikes/marketplace_probe_report.json -> samples_any_allowed_tools: false`. |
| S1.c | Any bundle with `tools/` subdir or symlinks? | **No** in all 17 bundles; symlink count = 0; max 83 files, max 5.5 MB (both under phase-3 caps). | `find skills -type l` empty; `find skills -maxdepth 2 -type d -name tools` empty on shallow clone. |
| S2.a | `gh api` query strings? | OK - quote whole endpoint: `gh api "/...?ref=main"`. | `spike-findings section S2.a`. |
| S2.b | Use `--paginate`? | **No** - 17 items fit in one page; extra parsing surface. Skill cap enforced by `MAX_FILES=100`. | `spike-findings section S2.b`. |
| S2.c | `shutil.which("gh")` in dev env? | Present at `/opt/homebrew/bin/gh` on this host. Daemon fallback path remains for hosts without it. | `spike-findings section S2.c`. |
| S2.d | `gh api` rc on HTTP 404? | **rc=0** (body carries `"status":"404"`). Marketplace wrapper must parse stdout, not rely on rc. | `spike-findings section S2.d`. |
| S2.e | Must argv-block `-X`? | Yes - without allowlist, `gh api -X DELETE` actually hits GitHub (403 only by remote authz). | `spike-findings section S2.e`. |
| S3.a | Pre+Post coexist? | Yes - same `hooks` dict; live probe returned `{completed: true}`. | `sdk_probe_posthook_report.json`. |
| S3.b | `PostToolUseHookInput` shape | `{session_id, transcript_path, cwd, permission_mode, agent_id, agent_type, hook_event_name, tool_name, tool_input, tool_response, tool_use_id}`. Same as Pre + `tool_response: Any`. | `typing.get_type_hints(PostToolUseHookInput)`. |
| S3.c | `tool_input.file_path` for Write/Edit | Absolute path. For `Write`: `{file_path, content}`; for `Edit`: `{file_path, old_string, new_string}`. | Live probe, phase-2 file-guard, SDK docs. |
| S3.d | Return shape of PostToolUse | `{}` is a valid no-op; no `hookSpecificOutput` needed. | Live probe. |
| S4.a | `copytree(..., symlinks=True)` vs default | Default follows link -> writes target content at dest. `True` preserves link name; but validator must reject *before* copytree anyway. | `shutil.copytree` docs; fixture test in spike. |
| S5.a | `sha256_of_tree` idempotent | Yes - two passes on unchanged bundle give identical digest. Flip one byte -> digest changes. Must use `not p.is_symlink()` to avoid follow. | `spike-findings section S5.a-d`. |
| B2 / B-6 | allowed-tools missing -> sentinel? empty list -> lockdown? | Missing -> `None` (sentinel) + `skill_permissive_default` warning. Empty `[]` -> parsed `[]` + `skill_lockdown_not_enforced` warning, **but global baseline still applies in phase 3** (per-skill gating is phase-4 work). See §2.1. | `detailed-plan section 1b`. |

**Pinned versions (known-good 2026-04-17), v2 stdlib-only decision:**

| Package | Pin | Note |
|---|---|---|
| `claude-agent-sdk` | `>=0.1.59,<0.2` | Phase 2. PostToolUse probe passed on 0.1.59. |
| `pyyaml` | `>=6.0` | Phase-2 main-venv dep (bridge/skills.py frontmatter parser). **Not** re-exported to installer — see below. |
| `gh` (system) | `>=2.40` | **External**. README documents `brew install gh` / `apt install gh`. |
| `git` (system) | already present | Invoked via Bash allowlist `git clone --depth=1`. |
| `uv` (system) | already present | Invoked via Bash allowlist `uv sync --directory tools/<name>` (installer **no longer** syncs itself — see B-4 below). |

**Installer is stdlib-only (B-4 decision).** `tools/skill-installer/` does
NOT carry a `pyproject.toml` and does NOT require `uv sync`. It runs via
`python tools/skill-installer/main.py` against the main interpreter. It
imports only from the stdlib (`urllib.request`, `urllib.parse`, `hashlib`,
`json`, `re`, `pathlib`, `subprocess`, `shutil`, `tempfile`, `socket`,
`ipaddress`, `os`, `sys`, `argparse`, `ast`, `fcntl`). Rationale in §2.8.

---

## 2. Corrected code snippets

Only the snippets that need a tweak after spike. Everything else - take
as-is from `detailed-plan.md`.

### 2.1 Phase-2 patch B2 - `bridge/skills.py::_normalize_allowed_tools`

**B-1 ripple audit (grep output, 2026-04-17):**

```text
src/assistant/bridge/skills.py:13       def _normalize_allowed_tools(raw: Any) -> list[str]:   <- CHANGE signature
src/assistant/bridge/skills.py:36       "allowed_tools": _normalize_allowed_tools(...)         <- passes through, OK
src/assistant/bridge/claude.py:87       allowed_tools=["Bash", ..., "WebFetch"]                <- hardcoded baseline, NOT touched by B2
tests/test_skills_manifest.py:24        assert meta["allowed_tools"] == ["Bash"]               <- existing case stays green
tests/test_skills_manifest_cache.py:15,82  write frontmatter with "allowed-tools: [Bash]"       <- list form, stays green
tests/test_u3_symlink_skill_discovery.py:21  writes "allowed-tools: [Bash]"                     <- list form, stays green
```

No caller iterates over `meta["allowed_tools"]` in a way that breaks on
`None` — phase-2 `build_manifest` reads only `meta["description"]` and
`meta["name"]` (skills.py:79-83). `_build_options` does NOT read the
manifest's `allowed_tools` at all in phase 2 (claude.py:87 hardcodes the
baseline). Existing tests assert `== ["Bash"]` for list-form frontmatter,
which stays green. **Conclusion: the patch is contained to `skills.py`
plus tests; no `or []` guards needed in src/.**

**Code change:**

```python
# src/assistant/bridge/skills.py

def _normalize_allowed_tools(raw: Any) -> list[str] | None:
    """Three-way result:

      * raw is missing / None   -> None (sentinel, "permissive default")
      * raw is a scalar str      -> [str]
      * raw is a list            -> [str(x) for x in raw]   (empty list kept)
      * anything else            -> None (malformed, warn at call-site)
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return None
```

`parse_skill` passes the value through under key `allowed_tools`; the only
return-type change is `list[str]` → `list[str] | None`.

Add to `skills.py::build_manifest` (inside the file-walk loop, after
`parse_skill`):

```python
    tools = meta.get("allowed_tools")
    if tools is None:
        log.warning(
            "skill_permissive_default",
            skill_name=meta["name"],
            reason="allowed-tools missing in SKILL.md",
        )
    elif tools == []:
        # B-6: phase-3 does NOT apply per-skill gating — global baseline is
        # still used. Authors who want lockdown must wait for phase 4.
        log.warning(
            "skill_lockdown_not_enforced",
            skill_name=meta["name"],
            reason="phase 3 uses global baseline; per-skill gating arrives in phase 4",
        )
```

Import `log = get_logger("bridge.skills")` at module top (structlog is
already used in phase 2; reuse).

**Honest semantics table (B-6 fix — previously this doc said `[]` was
lockdown; it is not, in phase 3):**

| Frontmatter | Parsed `allowed_tools` | `_build_options` behaviour | Log event |
|---|---|---|---|
| (field absent) | `None` | baseline (full phase-2 list) | `skill_permissive_default` |
| `allowed-tools: Bash` | `["Bash"]` | baseline (not gated in phase 3) | — |
| `allowed-tools: [Bash, Read]` | `["Bash","Read"]` | baseline | — |
| `allowed-tools: []` | `[]` | baseline (**lockdown NOT enforced**) | `skill_lockdown_not_enforced` |
| malformed (int, dict) | `None` | baseline | `skill_permissive_default` |

**Critical B-6 note for the operator / coder:** in phase 3, none of the
per-skill `allowed_tools` list forms actually restrict what the SDK exposes
to the model. `_build_options` in `bridge/claude.py:87` hardcodes the
global baseline `["Bash","Read","Write","Edit","Glob","Grep","WebFetch"]`
for *every* turn. A SKILL.md author writing `allowed-tools: []` expecting
a sandbox gets the global baseline instead — and the
`skill_lockdown_not_enforced` warning tells them so. Per-skill narrowing
requires merging per-skill sets with the baseline inside `_build_options`,
which is phase-4 work.

**Add to "Explicit non-goals" (detailed-plan §"Явные не-цели"):**

> Per-skill tool-gating (`allowed-tools: [...]` actually narrowing what
> the SDK exposes) — deferred to phase 4. Phase 3 only distinguishes
> "missing" from "list/empty" for the purpose of a permissive-default
> warning; all lists pass through unused.

**Future phase-4 merger (reference only, NOT phase-3 code):**

```python
# phase-4 _build_options:
per_skill = [m["allowed_tools"] for m in manifest if m["allowed_tools"] is not None]
if not per_skill or any(t == [] for t in per_skill):
    # any explicit [] on an active skill -> denies everything for that skill;
    # phase-4 must pass per-skill allowed_tools to SDK (per-session context).
    ...
else:
    allowed = sorted(set().union(*per_skill))
```

### 2.2 PostToolUse hooks - `bridge/hooks.py::make_posttool_hooks`

The detailed-plan snippet in section 5 is 80% correct; two corrections:

```python
# src/assistant/bridge/hooks.py  (add at module bottom, do not touch
# existing PreToolUse code).

def _is_inside_skills_or_tools(raw_path: str, project_root: Path) -> bool:
    """True iff raw_path resolves to a child of <project_root>/skills/
    or <project_root>/tools/. Rejects parent traversal defensively."""
    if not raw_path or ".." in Path(raw_path).parts:
        return False
    try:
        if Path(raw_path).is_absolute():
            abs_path = Path(raw_path).resolve()
        else:
            abs_path = (project_root / raw_path).resolve()
    except (OSError, ValueError):
        return False
    root = project_root.resolve()
    for sub in ("skills", "tools"):
        base = root / sub
        try:
            if abs_path.is_relative_to(base):
                return True
        except ValueError:
            continue
    return False


def make_posttool_hooks(project_root: Path, data_dir: Path) -> list[Any]:
    """HookMatcher list for PostToolUse. Fire-and-forget side effect:
    touch <data_dir>/run/skills.dirty iff the Write/Edit target is inside
    skills/ or tools/. Never deny (PostToolUse can't deny - the tool
    already ran)."""
    from claude_agent_sdk import HookMatcher

    sentinel = data_dir / "run" / "skills.dirty"

    async def on_write_edit(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        file_path = str(tool_input.get("file_path") or "")
        if _is_inside_skills_or_tools(file_path, project_root):
            try:
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.touch()
                log.info(
                    "posttool_sentinel_touched",
                    tool_name=raw.get("tool_name"),
                    file_path=file_path[:200],
                )
            except OSError as exc:
                log.warning("posttool_sentinel_touch_failed", error=repr(exc))
        return {}

    return [
        HookMatcher(matcher="Write", hooks=[on_write_edit]),
        HookMatcher(matcher="Edit", hooks=[on_write_edit]),
    ]
```

Corrections vs detailed-plan section 5:

- **`project_root` arg is load-bearing.** The plan's `"/skills/" in path`
  substring test matches any path containing those literal characters,
  including `/tmp/evil/skills/foo` if `cwd` is elsewhere. Use
  `Path.is_relative_to` against the resolved project root.
- **Wrap `sentinel.touch()` in `try/except OSError`.** The hook must never
  re-raise - PostToolUse errors can propagate to the model as "tool failed
  after-the-fact". We only tested `{}` returns, not exception throws, so
  stay safe.
- **Edit support.** The plan lists Edit; SDK input carries `file_path` for
  Edit too (verified via SDK TypedDict schema).

### 2.3 `ClaudeBridge._build_options` - merge Pre + Post

`bridge/claude.py::_build_options` currently assembles `hooks` as
`{"PreToolUse": make_pretool_hooks(pr)}`. Extend to include Post:

```python
def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
    pr = self._settings.project_root
    dd = self._settings.data_dir
    hooks: dict[Any, Any] = {
        "PreToolUse":  make_pretool_hooks(pr),
        "PostToolUse": make_posttool_hooks(pr, dd),
    }
    ...
```

No other changes to `_build_options`.

### 2.4 `ClaudeBridge._render_system_prompt` - sentinel check

Prepend to the phase-2 method body:

```python
def _render_system_prompt(self) -> str:
    self._check_skills_sentinel()    # <-- NEW first statement
    template_path = ...
    template = template_path.read_text(encoding="utf-8")
    manifest = build_manifest(self._settings.project_root / "skills")
    ...

def _check_skills_sentinel(self) -> None:
    sentinel = self._settings.data_dir / "run" / "skills.dirty"
    if not sentinel.exists():
        return
    from assistant.bridge.skills import (
        invalidate_manifest_cache, touch_skills_dir,
    )
    invalidate_manifest_cache()
    touch_skills_dir(self._settings.project_root / "skills")
    try:
        sentinel.unlink()
    except FileNotFoundError:
        pass  # race between two concurrent turns - harmless
    log.info("skills_cache_invalidated_via_sentinel")
```

**Race note (S-8 formal invariants):** Two concurrent chats each enter
`_render_system_prompt` and see `sentinel.exists() is True`. Both call
`invalidate_manifest_cache` (idempotent) and `touch_skills_dir`
(idempotent). One wins `unlink`; the other sees `FileNotFoundError` —
swallowed. Per-chat handler lock (`ClaudeHandler._chat_locks`, phase 2)
already serialises within a chat; cross-chat races are safe.

**Order of operations inside `_check_skills_sentinel` (critical):**

1. Read `sentinel.exists()`.
2. `invalidate_manifest_cache()` — drops module-level cache dict.
3. `touch_skills_dir(skills_dir)` — bumps parent mtime so next
   `_manifest_mtime` returns a fresh value.
4. `sentinel.unlink()` (missing_ok=True).

**If PostToolUse fires a new Write BETWEEN step 3 and step 4:**

- The new Write's `sentinel.touch()` happens AFTER our step-2
  invalidate; the new file's mtime is higher than the value we'll
  observe on the next turn, so the manifest rebuild picks it up.
- Our step-4 unlink removes the NEW sentinel file — but that's OK:
  manifest is already loaded fresh in step 2, and step 3 guarantees
  the next `_manifest_mtime` comparison will see the `touch_skills_dir`
  bump regardless. The "lost" sentinel touch causes no stale read.
- Race is benign: no stale manifest observable by model; at worst, one
  redundant `invalidate_manifest_cache` happens on the following turn.

### 2.5 `_canonicalize_url` (shared by preview + install + cache key)

**B-2 fix: drop query AND fragment entirely.** Query strings create two
problems: (a) tracking params (`?utm_source=...`) make the same bundle
cache under two different `url_hash`es, forcing two downloads and two
TOCTOU SHA slots; (b) a user-supplied URL with `?token=ABC` would
persist the token into `<data_dir>/run/installer-cache/<hash>/manifest.json`
on disk where it has no business being. GitHub tree URLs in our supported
formats (`/tree/<ref>/<path>`) carry semantics in the **path**, not the
query; there is no legitimate query-param we need today. If a future
extension needs `?ref=SHA`, add it to a `_SAFE_QUERY_KEYS` whitelist then.

```python
from urllib.parse import urlparse

def _canonicalize_url(url: str) -> str:
    s = urlparse(url.strip())
    scheme = s.scheme.lower()
    netloc = s.netloc.lower().removeprefix("www.")
    path = s.path.rstrip("/") or "/"
    # Intentionally drop s.query and s.fragment.
    return f"{scheme}://{netloc}{path}"
```

Canonical form used for:

- Cache directory name: `sha256(canonical_url).hexdigest()[:16]`.
- Equality check between preview-time URL and install-time URL argument.

Path case is **preserved** — FS is case-sensitive on Linux; aliasing
`Anthropics` vs `anthropics` would collapse two distinct repos into one
cache entry.

**Test `test_canonicalize_url_drops_query_and_fragment` (added in §5):**

```python
def test_canonicalize_url_drops_query_and_fragment():
    assert (
        _canonicalize_url("https://gh.com/x/y?utm=a#readme")
        == _canonicalize_url("https://gh.com/x/y?utm=b")
        == _canonicalize_url("https://gh.com/x/y/")
        == "https://gh.com/x/y"
    )

def test_canonicalize_url_strips_www():
    assert (_canonicalize_url("https://www.github.com/x/y")
            == _canonicalize_url("https://github.com/x/y"))

def test_canonicalize_url_preserves_path_case():
    # Anthropics/skills vs anthropics/skills must NOT collapse.
    assert (_canonicalize_url("https://github.com/Anthropics/skills")
            != _canonicalize_url("https://github.com/anthropics/skills"))
```

### 2.6 Marketplace `_gh_api` - rc=0 on 404 guard + H-4 warning-line skip

`gh api` returns rc=0 for any valid HTTP response, including 4xx. Also,
some `gh` versions prefix stdout with deprecation or update-notice warning
lines before the JSON payload. `marketplace.py::_gh_api` MUST parse
line-by-line and detect the `{message, status}` 4xx shape:

```python
def _parse_gh_json(stdout: str) -> Any:
    """`gh api` sometimes prints warning lines ahead of the JSON body.
    Scan line-by-line for the first one starting with `{` or `[`."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            return json.loads(stripped)
    # Fallback: maybe the whole stdout is JSON with leading/trailing whitespace.
    if stdout.strip():
        return json.loads(stdout)
    raise MarketplaceError(f"no JSON in gh stdout: {stdout[:200]!r}")


def _gh_api(endpoint: str) -> Any:
    gh = shutil.which("gh")
    if gh is None:
        raise MarketplaceError("gh CLI not found; marketplace disabled")
    proc = subprocess.run(
        [gh, "api", endpoint],
        check=False, capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if proc.returncode != 0:
        raise MarketplaceError(
            f"gh api failed rc={proc.returncode}: {proc.stderr[:300]}"
        )
    try:
        payload = _parse_gh_json(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MarketplaceError(f"gh api returned non-JSON: {exc}")
    if isinstance(payload, dict) and payload.get("message") and "status" in payload:
        raise MarketplaceError(
            f"GitHub API error {payload['status']}: {payload['message']}"
        )
    return payload
```

**Added test `test_parse_gh_json_skips_leading_warnings` (§5):** stdout
`"warning: gh out of date\n[{...}]"` → parser returns the list, not raises.

### 2.7 `make_bash_hook` registration site

Phase-2 `_BASH_PROGRAMS` is a dict at `hooks.py:77-85`. Phase 3 adds
`"gh"` and extends the `uv` + `git` validators. Detailed-plan sections 1
and 1a snippets are correct; registration is:

```python
_BASH_PROGRAMS = {
    "python": "python",
    "uv":     "uv",
    "git":    "git",
    "ls":     "ls",
    "pwd":    "pwd",
    "cat":    "cat",
    "echo":   "echo",
    "gh":     "gh",     # NEW
}
```

Extend the `argv[1]` switch inside the existing Bash `validate_*`
routines:

- `git`: add `argv[1] == "clone"` branch -> new `_validate_git_clone`
  (detailed-plan section 1). Keep `{"status","log","diff"}` untouched.
- `uv`: add `argv[1] == "sync"` branch -> new `_validate_uv_sync`
  (detailed-plan section 1). `argv[1] == "run"` branch stays.
- `gh`: new validator `_validate_gh_invocation` (detailed-plan section
  1a). Accept only `api` + `auth status`. Reject `-X/--method/-F/-f/
  --field/--raw-field/--input` before matching endpoint.

**Do NOT** relax `_SHELL_METACHARS` - `gh api "/repos/.../?ref=main"`
must pass through `shlex.split` as a single quoted token. Verified:
`shlex.split('gh api "/repos/x/y/contents/skills?ref=main"')` yields
three tokens; `?` is not a shell metachar and is not flagged.

### 2.8 B-4 decision: installer is stdlib-only (no `uv sync` bootstrap)

**Devil's advocate offered two options:** (a) pre-bootstrap `uv sync
--directory tools/skill-installer` before the first run, or (b) rewrite
installer to be stdlib-only. **Chosen: (b) stdlib-only.** Rationale:

- The installer's external deps in v1 were only two: `httpx` and `pyyaml`.
  `httpx` replaces `urllib.request` only for convenience; we do not need
  connection pools, HTTP/2, or streaming — the single largest bundle is
  5.5 MB (spike S1.c), well within `urllib.urlopen` comfort zone.
- `pyyaml` is used for **one** purpose: parse SKILL.md frontmatter. Phase 2
  already ships a frontmatter parser (`src/assistant/bridge/skills.py:10-37`)
  that uses `yaml.safe_load` against the main-venv `pyyaml`. Installer
  needs an in-file equivalent ~15 lines (regex for `^---\n(.*?)\n---`,
  then `yaml.safe_load` IF available, else a minimal line-parser). Since
  SKILL.md frontmatter only uses scalar strings + a list (`allowed-tools`),
  a tiny fallback parser is enough.
- **Chicken-and-egg killed.** Option (a) requires: main daemon boots →
  bootstrap task calls `uv sync --directory tools/skill-installer` → waits
  up to 120 s for the sync → only then runs the installer. Two subprocess
  layers, two failure modes (sync fail, install fail), extra virtualenv
  on disk (`tools/skill-installer/.venv/` ≈ 20 MB).
- **Audit surface smaller.** Zero external deps means zero CVEs in the
  installer's dependency chain. Security guide: "the code you don't ship
  can't bite you."
- **Trade-off accepted:** installer can't use `httpx.Client` features
  (retries, H2, streaming). For our 5 URL-shapes (`git clone`, `gh api`,
  gist tarball, raw SKILL.md, GitHub tree recursive) `urllib.request` is
  sufficient with a 30 s timeout.

**Consequence:** §3.3 / §3.5 recipe no longer runs
`uv sync --directory tools/skill-installer`. Running the installer is
just `python tools/skill-installer/main.py <cmd>`, reusing the
main-project interpreter.

**Minimal frontmatter parser for installer (in `tools/skill-installer/_lib/validate.py`):**

```python
# Stdlib frontmatter parser — mirrors src/assistant/bridge/skills.py:10-37
# but uses only stdlib (no pyyaml import). SKILL.md uses a tiny YAML
# subset; we accept `key: value` and `key: [a, b]` only.
import re
from typing import Any

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_SCALAR_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$")
_LIST_RE   = re.compile(r"^\[(.*)\]$")

def parse_frontmatter(text: str) -> dict[str, Any]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    out: dict[str, Any] = {}
    for raw in m.group(1).splitlines():
        sm = _SCALAR_RE.match(raw.strip())
        if not sm:
            continue
        key, value = sm.group(1), sm.group(2).strip()
        # Strip surrounding quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        lm = _LIST_RE.match(value)
        if lm:
            items = [x.strip().strip('"').strip("'") for x in lm.group(1).split(",")]
            out[key] = [x for x in items if x]
        else:
            out[key] = value
    return out
```

(The main-venv `bridge/skills.py` continues to use `yaml.safe_load` —
richer, handles multiline strings, block-style lists. The installer's
version only has to validate incoming bundles; multi-line YAML in a
SKILL.md is rare and would fail the validator's schema check downstream,
which is fine.)

### 2.9 H-1 Bash allowlist — permit `python skills/<name>/...`

**Found during B-1 grep audit.** Phase-2
`hooks.py::_validate_python_invocation` (lines 175-176) requires
`script.startswith("tools/")`. Anthropic's `skill-creator` bundle ships
`scripts/*.py` (e.g. `scripts/init_skill.py`, `scripts/run_eval.py`)
that the model is expected to invoke as `python skills/skill-creator/scripts/run_eval.py`
— which the current allowlist **denies**. This breaks the very bootstrap
path phase 3 is trying to enable.

**Fix (part of §3.2 phase-3.2 commit):**

```python
# src/assistant/bridge/hooks.py

_PYTHON_ALLOWED_PREFIXES: tuple[str, ...] = ("tools/", "skills/")

def _validate_python_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "python requires a script argument"
    script = argv[1]
    script_path = Path(script)
    if _has_dotdot(script_path.parts):
        return "script path must not contain '..'"
    if not script_path.is_absolute() and not any(
        script.startswith(p) for p in _PYTHON_ALLOWED_PREFIXES
    ):
        return (
            f"python script must live under one of {_PYTHON_ALLOWED_PREFIXES!r}; "
            f"got {script!r}"
        )
    if not _path_safely_inside(project_root / script_path, project_root):
        return "python script escapes project_root"
    return None
```

Same treatment for `_validate_uv_invocation`:
`_UV_RUN_ALLOWED_PREFIXES = ("tools/", "skills/")`.

**Acceptable baseline note (add to Risks):** permitting `python skills/<X>/...`
means any installed skill can execute arbitrary Python with the baseline
tool-access the daemon already gives it. This is intentional — a skill IS
user-trusted code (preview+confirm on install is the gate). The file-path
guard still sandboxes Read/Write to `project_root`, and the Bash argv
allowlist still forbids everything outside `_BASH_PROGRAMS`. Marketplace
(`anthropics/skills`) is trusted at the `gh` endpoint-whitelist level
(read-only `contents/`, `tarball/`); arbitrary user URLs require the
preview+confirm TOCTOU flow.

**Tests (add to §5 `test_bash_allowlist_skills_scripts.py` new file):**

```python
def test_python_skills_scripts_allowed(project_root):
    assert check_bash_command(
        "python skills/skill-creator/scripts/init_skill.py --name foo",
        project_root,
    ) is None

def test_python_skills_dotdot_still_denied(project_root):
    assert check_bash_command(
        "python skills/../../../etc/passwd", project_root
    ) is not None

def test_python_outside_tools_and_skills_denied(project_root):
    assert check_bash_command(
        "python src/assistant/main.py", project_root
    ) is not None
```

### 2.10 H-2 SSRF helper — mirror file, kept byte-identical

Installer must SSRF-check hostnames for `urllib.request` fetches. Phase 2
already has `src/assistant/bridge/hooks.py::classify_url` + helpers. We
pick the **pragmatic mirror** approach (devil's option 2):

- `src/assistant/bridge/hooks.py` keeps its SSRF functions as-is.
- `tools/skill-installer/_lib/net.py` contains a **byte-identical copy**
  of the ≤60-LOC block: `_is_private_address`, `_resolve_hostname`,
  `classify_url`. Header of the installer file:

  ```python
  # MIRROR OF src/assistant/bridge/hooks.py ssrf helpers.
  # Keep byte-for-byte identical with the block between the
  # `SSRF_MIRROR_START` and `SSRF_MIRROR_END` sentinels in the source.
  # A CI-enforced unit test asserts filecmp.cmp() equality.
  ```

- Bracket the source block with:

  ```python
  # --- SSRF_MIRROR_START (mirrored to tools/skill-installer/_lib/net.py) ---
  def _is_private_address(...): ...
  async def _resolve_hostname(...): ...
  async def classify_url(...): ...
  # --- SSRF_MIRROR_END ---
  ```

- Test `tests/test_ssrf_mirror_in_sync.py`:

  ```python
  def _extract_block(path: Path, start: str, end: str) -> str:
      text = path.read_text()
      a = text.index(start) + len(start)
      b = text.index(end)
      return text[a:b]

  def test_ssrf_mirror_byte_identical():
      src = ROOT / "src/assistant/bridge/hooks.py"
      dst = ROOT / "tools/skill-installer/_lib/net.py"
      src_block = _extract_block(src, "SSRF_MIRROR_START", "SSRF_MIRROR_END")
      dst_text  = dst.read_text()
      assert src_block.strip() in dst_text, (
          "tools/skill-installer/_lib/net.py must contain an exact copy of the "
          "phase-2 SSRF block; re-copy from src/assistant/bridge/hooks.py."
      )
  ```

**Rationale for mirror over shared import:** the installer is a separate
entrypoint invoked via the Bash allowlist with `cwd=project_root`; Python
import from `src/assistant/bridge/...` would require adding `src/` to
`sys.path`, which couples the installer to the main package layout and
complicates future deletion of the installer from the repo. Mirror is
ugly but local.

### 2.11 H-3 + B-3 `sha256_of_tree` — stable order + `.git/` exclusion

Combined fix for two issues:

- **B-3:** after `git clone`, `dest/.git/` contains clone-time metadata
  (`HEAD`, `packed-refs`, index dates) that differ between runs for the
  same commit. Hashing them yields a non-deterministic digest → every
  TOCTOU check fails. Must exclude `.git/` from the hash. Also exclude
  `__pycache__/`, `.DS_Store`, `*.pyc`, `.ruff_cache/`, `.mypy_cache/`,
  `.pytest_cache/` (appear in user-URL bundles).
- **H-3:** `sorted(Path)` sorts by repr; `as_posix()` yields different
  strings on Windows (backslash) vs POSIX. Explicit
  `key=lambda p: p.relative_to(root).as_posix()` makes the order
  portable.

```python
# tools/skill-installer/_lib/validate.py

_HASH_SKIP_PART_NAMES: frozenset[str] = frozenset({
    ".git", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache",
})
_HASH_SKIP_NAME_SUFFIXES: tuple[str, ...] = (".pyc", ".DS_Store")

def _should_hash(p: Path, root: Path) -> bool:
    if not p.is_file() or p.is_symlink():
        return False
    if any(part in _HASH_SKIP_PART_NAMES for part in p.relative_to(root).parts):
        return False
    if any(p.name.endswith(sfx) for sfx in _HASH_SKIP_NAME_SUFFIXES):
        return False
    return True

def sha256_of_tree(root: Path) -> str:
    h = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if _should_hash(p, root)),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for p in files:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big")); h.update(rel); h.update(b"\x00")
        data = p.read_bytes()
        h.update(len(data).to_bytes(8, "big")); h.update(data)
    return h.hexdigest()
```

**Double-safety in `_lib/fetch.py::_git_clone`:**

```python
def _git_clone(url: str, dest: Path) -> None:
    subprocess.run(
        ["git", "clone", "--depth=1", url, str(dest)],
        check=True, timeout=TIMEOUT,
    )
    # Strip .git metadata after clone so the bundle hash is stable
    # across re-clones of the same commit. `_should_hash` also skips
    # .git/, but nuking the directory is zero-cost and makes the bundle
    # bit-for-bit reproducible across environments.
    shutil.rmtree(dest / ".git", ignore_errors=True)
```

**Tests (§5 additions):**

```python
def test_sha256_of_tree_idempotent_after_git_clone(tmp_path):
    """Two shallow clones of the same URL/commit must hash identically."""
    # Use a local bare repo as source — fully offline & deterministic.
    import subprocess
    bare = tmp_path / "src.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True)
    # ... populate bare with a known commit via temp worktree ...
    a = tmp_path / "a"; b = tmp_path / "b"
    subprocess.run(["git", "clone", "--depth=1", str(bare), str(a)], check=True)
    subprocess.run(["git", "clone", "--depth=1", str(bare), str(b)], check=True)
    # Strip .git (as _git_clone does):
    shutil.rmtree(a / ".git"); shutil.rmtree(b / ".git")
    assert sha256_of_tree(a) == sha256_of_tree(b)

def test_sha256_of_tree_skips_dot_git_and_pycache(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    h1 = sha256_of_tree(tmp_path)
    # Pollute with .git/ and __pycache__
    (tmp_path / ".git").mkdir(); (tmp_path / ".git" / "HEAD").write_text("ref: x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00")
    assert sha256_of_tree(tmp_path) == h1
```

### 2.12 S-2 Installer mutex + S-5 copytree inline comment + S-6 TOCTOU diff

**S-2 (bootstrap mutex).** Two processes racing on the same `url_hash`
(old daemon during restart + new daemon) corrupt the cache entry. Wrap
the whole preview+install flow in an `fcntl.flock` on a lockfile inside
the cache dir:

```python
# tools/skill-installer/main.py

import fcntl, os
from contextlib import contextmanager

@contextmanager
def _cache_lock(cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / ".lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

def cmd_preview(args):
    cdir = _cache_dir(args.url); cdir.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cdir):
        # ... existing preview body ...

def cmd_install(args):
    cdir = _cache_dir(args.url)
    with _cache_lock(cdir):
        # ... existing install body ...
```

(POSIX-only — fine for phase 3, macOS + Linux dev boxes. Windows support
is not a stated goal.)

**S-5 (inline comment on copytree, `_lib/install.py`):**

```python
# DO NOT change to symlinks=False — validator already rejected all symlinks;
# symlinks=True is defence-in-depth against a TOCTOU swap between validate
# and copy. See gotcha #4 in plan/phase3/implementation.md.
shutil.copytree(bundle, stage, symlinks=True)
```

**S-6 (TOCTOU diff in stderr).** When SHA mismatch, print a sorted file
diff so the user can decide whether to re-preview:

```python
# tools/skill-installer/_lib/install.py

def diff_trees(old: Path, new: Path) -> list[str]:
    """Return sorted human-readable diff lines: REMOVED/ADDED/CHANGED."""
    import hashlib
    def _digest(p: Path) -> str:
        h = hashlib.sha256(p.read_bytes())
        return h.hexdigest()
    def _tree_map(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix(): _digest(p)
            for p in root.rglob("*") if _should_hash(p, root)
        }
    a, b = _tree_map(old), _tree_map(new)
    lines: list[str] = []
    for rel in sorted(set(a) | set(b)):
        if rel in a and rel not in b:
            lines.append(f"REMOVED: {rel}")
        elif rel in b and rel not in a:
            lines.append(f"ADDED: {rel}")
        elif a[rel] != b[rel]:
            lines.append(f"CHANGED: {rel}")
    return lines
```

Used from `cmd_install`:

```python
if new_sha != manifest["bundle_sha"]:
    diff = diff_trees(cdir / "bundle", cdir / "verify")
    shutil.rmtree(cdir, ignore_errors=True)
    sys.stderr.write(
        "bundle on source changed since preview; re-run `preview <URL>` "
        "to see new content\n"
    )
    for line in diff[:50]:      # cap at 50 lines
        sys.stderr.write(line + "\n")
    if len(diff) > 50:
        sys.stderr.write(f"... and {len(diff) - 50} more lines\n")
    sys.exit(EXIT_TOCTOU)
```

**Why keep the preview bundle?** The `cmd_preview` in v1 deleted
`bundle/` after writing `manifest.json`. For S-6 diff we need both old
and new trees present at install-time. **v2 change to `cmd_preview`:**
leave `bundle/` on disk (sweeper cleans after 7 d anyway). `cmd_install`
re-fetches into `verify/`, diffs against `bundle/`, then `rmtree` the
whole cache entry in `finally`.

---

## 3. Step-by-step recipe for coder

Execute in this order. Each step maps to detailed-plan sections and ends
with a small, reviewable commit.

### Phase 3.1 - phase-2 prep patch (B2 + B-6)

1. **Edit** `src/assistant/bridge/skills.py`:
   - Return-type of `_normalize_allowed_tools` -> `list[str] | None`.
   - `build_manifest` logs `skill_permissive_default` warning when
     `meta.get("allowed_tools") is None`.
   - `build_manifest` also logs `skill_lockdown_not_enforced` when
     `meta["allowed_tools"] == []` (B-6: phase 3 does not gate).
2. **Update** `tests/test_skills_manifest.py` (phase-2 existing):
   - Keep: `Bash scalar -> ["Bash"]`, `list -> kept`.
   - Add: `missing field -> None` (sentinel).
   - **Do NOT** add a "empty list = lockdown" test — B-6 says `[]` is
     a no-op in phase 3. Instead add
     `test_empty_list_logs_warning_but_does_not_gate` (§5).
3. **Add** `tests/test_skill_permissive_default.py` (§5 for invariants).
4. Bump "Explicit non-goals" in detailed-plan (but that's the devil's
   advocate's call on a separate doc — leave a note here only if the
   user asks).
5. Run `just lint && just test` — all phase-2 tests must stay green.
6. Commit: `phase 3.1: permissive allowed-tools sentinel + lockdown warning (B-6)`.

### Phase 3.2 - hooks extension (+ H-1 skills/ prefix)

1. **Edit** `src/assistant/bridge/hooks.py`:
   - **H-1:** `_PYTHON_ALLOWED_PREFIXES = ("tools/", "skills/")`; update
     `_validate_python_invocation` and `_validate_uv_invocation` to
     accept both (§2.9).
   - Add `_validate_gh_invocation` + `_GH_API_SAFE_ENDPOINT_RE` +
     `_GH_FORBIDDEN_FLAGS`.
   - Add `_validate_git_clone` (SSRF host-safety inline, same
     `ipaddress` pattern as phase-2 `classify_url`).
   - Add `_validate_uv_sync` (`--directory=tools/<name>` path-guard).
   - Extend `_BASH_PROGRAMS` with `"gh": "gh"`.
   - Bracket the SSRF block with `# --- SSRF_MIRROR_START ---` /
     `# --- SSRF_MIRROR_END ---` sentinels (H-2).
   - Append `make_posttool_hooks(project_root, data_dir)` at file bottom.
2. **Edit** `src/assistant/bridge/claude.py`:
   - `_build_options`: `hooks` now has both PreToolUse + PostToolUse.
   - `_render_system_prompt`: first call `self._check_skills_sentinel()`.
   - Add `_check_skills_sentinel` method (section 2.4 above).
3. **Add tests** (one file each):
   - `tests/test_posttool_hook_touches_sentinel.py`
   - `tests/test_skills_sentinel_hot_reload.py`
   - `tests/test_bash_allowlist_gh_cli.py` (5 allow + 13 deny matrix)
   - `tests/test_bash_allowlist_git_clone.py` (allow inside-project /
     deny `/tmp`).
4. Run `just lint && just test`.
5. Commit: `phase 3.2: PostToolUse sentinel + gh/git-clone/uv-sync allowlist`.

### Phase 3.3 - installer skeleton (stdlib-only, B-4 decision)

1. **Create** `tools/skill-installer/` — **NO `pyproject.toml`**, **NO
   `uv sync`** (B-4 decision, §2.8). Layout:
   ```
   tools/skill-installer/
   |-- main.py                 # argparse CLI; stdlib only
   `-- _lib/
       |-- __init__.py
       |-- net.py              # MIRROR of bridge/hooks.py SSRF helpers (H-2)
       |-- fetch.py            # git clone / gh api / urllib.request; calls net.classify_url
       |-- marketplace.py      # _gh_api wrapper + _parse_gh_json + list/info/install
       |-- validate.py         # parse_frontmatter + AST + symlink reject + path-traversal + sha256_of_tree
       |-- preview.py          # render_preview(url, bundle, sha, report) -> str
       `-- install.py          # atomic_install + optional tools-split + diff_trees
   ```
2. Run nothing to "populate" — the installer lives in the main interpreter
   space. Smoke: `python tools/skill-installer/main.py --help` must exit
   0 without `ModuleNotFoundError`.
3. **Add** `tests/test_skill_installer_fetch_mock.py`,
   `tests/test_skill_installer_ssrf_deny.py`,
   `tests/test_skill_installer_path_escape.py`,
   `tests/test_skill_installer_size_limits.py`,
   `tests/test_skill_installer_symlink_rejected.py`,
   `tests/test_ssrf_mirror_in_sync.py` (H-2),
   `tests/test_canonicalize_url.py` (B-2).
4. Commit: `phase 3.3: skill-installer core, stdlib-only (B-4)`.

### Phase 3.4 - marketplace + TOCTOU cache

1. **Edit** `tools/skill-installer/_lib/marketplace.py`:
   - `MARKETPLACE_URL = "https://github.com/anthropics/skills"`.
   - `MARKETPLACE_REPO = "anthropics/skills"`.
   - `MARKETPLACE_BASE_PATH = "skills"` (**verified** in spike).
   - `_gh_api` with rc=0-on-404 parsing (section 2.6).
   - `list_skills`, `fetch_skill_md`, `install_from_marketplace`
     (convenience shortcut returning tree URL).
2. **Edit** `tools/skill-installer/main.py`:
   - `cmd_preview` / `cmd_install` with cache-by-URL + re-fetch + SHA
     compare (detailed-plan section 4).
   - `EXIT_TOCTOU = 7` constant.
   - `cmd_marketplace_list / info / install` subcommands.
   - `cmd_status` (section 8 uv sync progress - stub returns
     `{"status": "unknown"}` if skill isn't running; real polling in
     phase-3.5).
3. **Add** `tests/test_skill_installer_marketplace_list.py`,
   `tests/test_skill_installer_marketplace_install.py`,
   `tests/test_skill_installer_toctou_detection.py`.
4. Commit: `phase 3.4: marketplace subcommands + TOCTOU re-fetch`.

### Phase 3.5 - bootstrap + sweeper + SKILL.md + Daemon.stop (S-1, S-3)

1. **Edit** `src/assistant/main.py::Daemon.start`:
   - After `ensure_skills_symlink` call:
     ```python
     self._bg_tasks: set[asyncio.Task] = set()
     t1 = asyncio.create_task(self._bootstrap_skill_creator_bg())
     t2 = asyncio.create_task(self._sweep_run_dirs())
     for t in (t1, t2):
         self._bg_tasks.add(t)
         t.add_done_callback(self._bg_tasks.discard)
     ```
   - Add coroutines from detailed-plan section 6 / 6a.
   - Do **not** add any `_ensure_installer_venv` / `uv sync` step — B-4
     chose stdlib-only, the installer runs under the main interpreter
     directly.
   - On startup, log once if `shutil.which("gh") is None`.
2. **S-1** `Daemon.stop` additions:
   ```python
   async def stop(self) -> None:
       # existing stop logic (close telegram adapter, close DB) ...
       if self._bg_tasks:
           log.info("daemon_waiting_bg_tasks", count=len(self._bg_tasks))
           # Best-effort drain; do not propagate exceptions.
           await asyncio.gather(*self._bg_tasks, return_exceptions=True)
   ```
   Prevents SIGTERM mid-bootstrap from leaving a half-downloaded
   `<data_dir>/run/installer-cache/<hash>/verify/` on disk.
3. **S-3** owner UX on bootstrap failure. At the end of
   `_bootstrap_skill_creator_bg` on any failure branch (`rc != 0`,
   `TimeoutError`, `Exception`):
   ```python
   notified_marker = self._settings.data_dir / "run" / ".bootstrap_notified"
   if not notified_marker.exists() and self._telegram_adapter is not None:
       msg = ("Автобутстрап skill-creator не удался — "
              "marketplace-установка временно недоступна. "
              "Проверь `gh auth status` и логи.")
       try:
           await self._telegram_adapter.send_text(
               self._settings.owner_chat_id, msg,
           )
           notified_marker.parent.mkdir(parents=True, exist_ok=True)
           notified_marker.touch()
       except Exception as exc:
           log.warning("bootstrap_notify_failed", error=str(exc))
   ```
   One-shot marker avoids spam across restarts. **Do not** notify on
   `skipped_no_gh` — that's a predictable state, not a surprise.
4. **Create** `skills/skill-installer/SKILL.md` (detailed-plan section 7
   content). Do NOT create `skills/skill-creator/` — it appears at runtime
   via the bootstrap task. Add `.gitignore` entries: explicit allowlist
   `skills/ping/`, `skills/skill-installer/`; everything else under
   `skills/` is ignored.
5. **Add** `tests/test_bootstrap_skill_creator.py` (§5 for the
   fire-and-forget timing invariant).
6. **Add** `tests/test_sweep_run_dirs.py` (ttl for tmp + installer-cache).
7. **Add** `tests/test_daemon_stop_drains_bg_tasks.py` (S-1).
8. Commit: `phase 3.5: auto-bootstrap + sweeper + Daemon.stop drain (S-1/S-3)`.

### Phase 3.6 - URL detector + polish (S-4 bridge signature delta)

1. **S-4 phase-2 API delta.** `ClaudeBridge.ask` acquires a new keyword-
   only argument:
   ```python
   async def ask(
       self,
       chat_id: int,
       user_text: str,
       history: list[dict[str, Any]],
       *,
       system_notes: list[str] | None = None,
   ) -> AsyncIterator[Any]:
   ```
   `system_notes` is appended to the final user envelope's `content` as
   a list-of-blocks mix: `[{"type":"text","text": user_text}, {"type":
   "text","text": f"[system-note: {n}]"} for n in system_notes]`. If
   `system_notes` is `None` or `[]`, content stays as the plain `str`
   (backwards-compatible with phase-2 callers).
2. **Edit** `src/assistant/handlers/message.py::_run_turn`:
   - Compile `_URL_RE` at module level (S-7 regex — see §5).
   - Strip trailing punctuation via post-processing, not in regex.
   - Collect up to 3 matches → `system_notes=["URL detected: ...", ...]`.
   - Pass `system_notes=system_notes` as kwarg to `ClaudeBridge.ask`.
   - `ConversationStore.append` stores **original** `msg.text`, not
     enriched. System-notes are ephemeral per envelope.
3. **Add** `tests/test_url_detector.py` (§5 — with S-7 edge cases).
4. Run full `just lint && just test`.
5. **Manual smoke** (no CI) — see §4 below.
6. Commit: `phase 3.6: URL detector + system_notes bridge kwarg (S-4)`.

### Commands cheat-sheet

```bash
# B-4: installer is stdlib-only, no uv sync required.
python tools/skill-installer/main.py --help   # smoke

# Lint + test (whole repo):
just lint
just test

# Run a probe:
uv run python spikes/sdk_probe_posthook.py
uv run python spikes/marketplace_probe.py

# Bootstrap the marketplace skill-creator manually (first real run):
uv run python tools/skill-installer/main.py marketplace install skill-creator --confirm
```

### Manual smoke checks (after phase 3.6)

1. **Start daemon** (`just run`) with `gh` installed and `~/.claude` OAuth
   valid. Observe `log.info skill_creator_bootstrap_starting` -> either
   `skill_creator_bootstrap_ok` or `*_failed/*_timeout` within 120 s.
   `Daemon.start()` should return in <2 s regardless.
2. **Tell the bot:** "создай скилл echo, который отвечает {"pong":true}".
   Expect: model reads `skills/skill-creator/SKILL.md`, writes
   `skills/echo/SKILL.md` + `tools/echo/main.py` via Write. PostToolUse
   touches `<data_dir>/run/skills.dirty`. Next turn manifest shows `echo`.
3. **Tell the bot:** "какие скиллы есть в marketplace?" Expect: model
   calls `python tools/skill-installer/main.py marketplace list`, gets
   the 17-entry JSON. User: "поставь pdf". Preview -> "да" -> install ->
   SHA compare OK (single-fetch scenario) -> sentinel touched.
4. **TOCTOU smoke:** preview a local fake URL (toy test server) with
   bundle v1, swap to v2, run install -> `exit 7` with expected stderr.

---

## 4. Known gotchas (found in spike)

1. **`gh api` rc=0 on HTTP 404.** Marketplace wrapper must parse stdout
   and look for `{"message":..., "status":"..."}`. Trusting `returncode
   == 0` yields silent "empty list" with no error surfaced. See section
   2.6.
2. **`gh api` fires the request regardless of our allowlist.** A missing
   allowlist rule means the CLI actually hits GitHub; remote authz (403)
   is not our defence. Block `-X/--method/-F/-f/--field/--raw-field/
   --input` at argv level **before** `gh` ever runs. Tests
   `test_bash_allowlist_gh_cli` cover 13 deny cases precisely for this.
3. **`allowed-tools: []` vs missing (B-6).** Phase-2 parser conflates
   them; phase-3 B2 patch distinguishes, but phase 3 does **NOT** act on
   either — global baseline applies to all skills either way. What's
   load-bearing is the *log event*: `skill_permissive_default` (missing)
   vs `skill_lockdown_not_enforced` (empty list). The 3-value return type
   `list[str] | None` is kept so phase 4 can start enforcing without
   another migration. See §2.1.
4. **`shutil.copytree(..., symlinks=False)` follows links.** Phase 3 must
   use `symlinks=True` **and** the validator must reject any symlink
   before `copytree` is reached. Both layers independently block the
   attack. Don't remove either.
5. **`pathlib.Path.is_file()` follows symlinks by default.** The hasher
   MUST use `p.is_file() and not p.is_symlink()`. Otherwise the SHA
   hashes the *target* content of any symlink that survived the
   validator, and a symlink swap between `sha256_of_tree(bundle)` and
   `atomic_install` bypasses TOCTOU detection. Validator-level reject
   remains primary.
6. **`Daemon.start()` fire-and-forget vs test mocks.** The Daemon's two
   new tasks (`_bootstrap_skill_creator_bg`, `_sweep_run_dirs`) are
   started with `asyncio.create_task(...)` **without** `await`. In tests,
   `start()` returning within <500 ms is the invariant - assert by making
   the mock bootstrap sleep 60 s and then `asyncio.wait_for(daemon.start(),
   timeout=0.5)` must succeed.
7. **`create_task` refs can be GC'd.** Keep handles on `self`
   (`self._bg_tasks: set[asyncio.Task] = set()` and
   `task.add_done_callback(self._bg_tasks.discard)`). Python can GC a
   floating task mid-run and silently drop the subprocess; this is a
   `RuntimeWarning` in Python 3.12+, not a runtime fail, so CI won't
   catch it - add the hold explicitly.
8. **Anthropic bundles have no `tools/` subdir (verified 2026-04-17).**
   The optional `atomic_install` split for `bundle/tools/` is dead code
   against every public skill today. Keep it (user-URLs may use it) but
   don't let tests depend on it being hit by the bootstrap path.
9. **`canvas-design` is 5.5 MB / 83 files.** Under `MAX_TOTAL=10 MB` but
   a large diff over Wi-Fi -> `gh api` recursive walk could hit the
   60 req/hour anonymous cap if a user previews it many times. Skill-
   installer should prefer `git clone --depth=1` over per-file
   `gh api /contents/...` for bundles over ~10 files. The detailed-plan
   section 4a already goes through `git clone` for repo-rooted URLs; the
   `GITHUB_TREE_RE` branch uses `gh api` - fine for `skill-creator` (18
   files) but worth a comment in the code.
10. **`gh` 2.89 installed on dev host (this machine).** CI runner may not
    have it. All marketplace tests must mock `subprocess.run` for the
    `gh` code-path. No live `gh api` calls in CI.
11. **B-3 `.git/` leaks into `sha256_of_tree`.** After `git clone`,
    `dest/.git/packed-refs` and `dest/.git/HEAD` differ between re-clones
    of the same commit. Hashing them gives TOCTOU false-positive *every*
    install. Fix: skip `.git/`, `__pycache__/`, `.DS_Store`, `*.pyc`,
    `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/` in the hasher AND
    `shutil.rmtree(dest/.git)` inside `_git_clone`. See §2.11.
12. **H-3 cross-platform `Path` sorting.** `sorted(Path_obj)` sorts by
    repr — backslash on Windows, forward-slash on POSIX. Explicit
    `key=lambda p: p.relative_to(root).as_posix()` gives portable order.
    Matters if CI ever runs on Windows or a Linux user later mounts a
    Windows FS.
13. **B-4 stdlib-only installer.** No `pyproject.toml` in
    `tools/skill-installer/` — running under the main interpreter with
    `python tools/skill-installer/main.py`. If someone reintroduces
    `httpx` or `pyyaml` they re-add the chicken-and-egg problem during
    bootstrap. See §2.8.
14. **H-1 phase-2 Bash allowlist forbids `python skills/<X>/scripts/...`.**
    Phase 3 MUST relax `_validate_python_invocation` (+ `uv run`) to accept
    `skills/` prefix too, otherwise Anthropic `skill-creator`'s own scripts
    are denied and the bootstrap artifact can't run. See §2.9.
15. **B-2 URL query strings leak tokens into cache.** `?token=ABC` would
    hit disk in `manifest.json`. `_canonicalize_url` drops query and
    fragment entirely. Whitelist param by param only when a new URL-shape
    requires it. See §2.5.
16. **H-4 `gh` stdout warning-line prefix.** Some `gh` versions print
    a warning line before the JSON payload. `_parse_gh_json` must scan
    line-by-line for the first `{`/`[` start. See §2.6.
17. **S-1 SIGTERM mid-bootstrap.** Without `Daemon.stop` draining
    `self._bg_tasks`, a restart leaves partial `verify/` dir and the
    next preview/install collides. `_cache_lock` (S-2) partially mitigates
    but the drain is still needed to finish the `finally: rmtree`.

---

## 5. Tests - invariants + assertions

Exact invariants for every new/updated test file. Each test file under
~60 LOC; reuses phase-2 fixtures (`tmp_path`, `monkeypatch` for
`subprocess.run`, `structlog.testing.capture_logs`).

### `test_skill_permissive_default.py` (B-6 honest)

Invariants:
- SKILL.md without `allowed-tools` → `parse_skill(path)["allowed_tools"] is None`.
- SKILL.md with `allowed-tools: []` → returns `[]` (parsed), but
  `build_manifest` emits `skill_lockdown_not_enforced` warning AND
  `_build_options` STILL passes the global baseline to the SDK. (Phase 3
  does not gate.)
- SKILL.md with `allowed-tools: Bash` → `["Bash"]`; no warning.
- Missing field → `skill_permissive_default` warning fires.

```python
def test_missing_allowed_tools_is_sentinel(tmp_path):
    (tmp_path / "echo").mkdir()
    (tmp_path / "echo" / "SKILL.md").write_text(
        "---\nname: echo\ndescription: test\n---\n"
    )
    meta = parse_skill(tmp_path / "echo" / "SKILL.md")
    assert meta["allowed_tools"] is None

def test_empty_list_parses_but_does_not_gate(tmp_path, caplog):
    (tmp_path / "echo").mkdir()
    (tmp_path / "echo" / "SKILL.md").write_text(
        "---\nname: echo\ndescription: test\nallowed-tools: []\n---\n"
    )
    meta = parse_skill(tmp_path / "echo" / "SKILL.md")
    assert meta["allowed_tools"] == []
    # Now trigger manifest build so the warning fires:
    from structlog.testing import capture_logs
    with capture_logs() as cap:
        build_manifest(tmp_path)
    assert any(e["event"] == "skill_lockdown_not_enforced" for e in cap)

def test_empty_list_does_not_narrow_sdk_allowed_tools(fake_settings):
    # B-6: in phase 3, _build_options hardcodes the baseline regardless.
    bridge = ClaudeBridge(fake_settings)
    opts = bridge._build_options(system_prompt="x")
    assert set(opts.allowed_tools) == {
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"
    }
```

### `test_canonicalize_url.py` (B-2)

```python
def test_canonicalize_url_drops_query_and_fragment():
    assert (
        _canonicalize_url("https://gh.com/x/y?utm=a#readme")
        == _canonicalize_url("https://gh.com/x/y?utm=b")
        == _canonicalize_url("https://gh.com/x/y/")
        == "https://gh.com/x/y"
    )

def test_canonicalize_url_strips_www():
    assert (_canonicalize_url("https://www.github.com/x/y")
            == _canonicalize_url("https://github.com/x/y"))

def test_canonicalize_url_preserves_path_case():
    assert (_canonicalize_url("https://github.com/Anthropics/skills")
            != _canonicalize_url("https://github.com/anthropics/skills"))
```

### `test_sha256_of_tree_git_and_caches.py` (B-3 + H-3)

```python
def test_sha256_of_tree_skips_dot_git_and_pycache(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    h1 = sha256_of_tree(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: x")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00")
    (tmp_path / ".DS_Store").write_bytes(b"metadata")
    assert sha256_of_tree(tmp_path) == h1

def test_sha256_of_tree_idempotent_after_git_clone(tmp_path):
    """Two shallow clones of the same local bare repo must hash identically
    (after _git_clone strips .git/)."""
    # Set up a local bare source; clone twice; strip .git/; assert equality.
    ...
```

### `test_ssrf_mirror_in_sync.py` (H-2)

```python
def test_ssrf_mirror_byte_identical():
    src = ROOT / "src/assistant/bridge/hooks.py"
    dst = ROOT / "tools/skill-installer/_lib/net.py"
    src_text = src.read_text()
    dst_text = dst.read_text()
    s_start = src_text.index("SSRF_MIRROR_START") + len("SSRF_MIRROR_START")
    s_end   = src_text.index("SSRF_MIRROR_END")
    block = src_text[s_start:s_end].strip()
    assert block in dst_text, (
        "tools/skill-installer/_lib/net.py must contain an exact copy of "
        "the SSRF block from src/assistant/bridge/hooks.py — re-copy it."
    )
```

### `test_parse_gh_json.py` (H-4)

```python
def test_parse_gh_json_skips_leading_warnings():
    out = 'warning: gh is out of date\n[{"name":"pdf","type":"dir"}]\n'
    assert _parse_gh_json(out) == [{"name": "pdf", "type": "dir"}]

def test_parse_gh_json_raises_on_no_json():
    with pytest.raises(MarketplaceError):
        _parse_gh_json("just warnings\nand no JSON\n")
```

### `test_posttool_hook_touches_sentinel.py`

Invariants:
- `Write` inside `<project_root>/skills/foo/SKILL.md` -> sentinel exists.
- `Write` to `<project_root>/foo.py` (outside skills/tools) -> sentinel absent.
- `Edit` inside `<project_root>/tools/bar/main.py` -> sentinel exists.
- Hook must return `{}` (dict) in both cases (PostToolUse no-op).
- Traversal attempt (`../../../etc/passwd`) -> sentinel absent.

```python
@pytest.mark.asyncio
async def test_write_inside_skills_touches_sentinel(tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    (tmp_path / "skills" / "echo").mkdir(parents=True)
    matchers = make_posttool_hooks(tmp_path, data_dir)
    hook = matchers[0].hooks[0]                     # Write matcher
    out = await hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": str(tmp_path / "skills" / "echo" / "SKILL.md"),
                        "content": "..."}},
        "tu-1", {}
    )
    assert out == {}
    assert (data_dir / "run" / "skills.dirty").exists()
```

### `test_skills_sentinel_hot_reload.py`

Invariants:
- Touch `<data_dir>/run/skills.dirty` -> next `ClaudeBridge._render_system_prompt`
  invalidates the module cache AND unlinks the sentinel.
- Run `_render_system_prompt` twice back-to-back WITHOUT the sentinel ->
  cache stable (no invalidate log event).

### `test_bash_allowlist_gh_cli.py`

Allow (5 asserts - validator returns `None`):
```python
assert validate_bash(shlex.split("gh api /repos/anthropics/skills/contents/skills"), ROOT) is None
assert validate_bash(shlex.split("gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md"), ROOT) is None
assert validate_bash(shlex.split('gh api "/repos/x/y/contents/skills?ref=main"'), ROOT) is None
assert validate_bash(shlex.split("gh api /repos/x/y/tarball/main"), ROOT) is None
assert validate_bash(shlex.split("gh auth status"), ROOT) is None
```

Deny (13 asserts - validator returns a non-None reason containing the
expected substring):
```python
deny_matrix = [
    ("gh api /graphql",                     "not in read-only whitelist"),
    ("gh api /user",                        "not in read-only whitelist"),
    ("gh api -X POST /repos/x/y/issues",    "flag -X"),
    ("gh api --method PATCH /repos/x/y",    "flag --method"),
    ("gh api -F title=X /repos/x/y/issues", "flag -F"),
    ("gh api -f title=X /repos/x/y/issues", "flag -f"),
    ("gh api --field title=X /repos/x/y",   "flag --field"),
    ("gh api --input foo.json /repos/x/y",  "flag --input"),
    ("gh pr create",                        "subcommand 'pr'"),
    ("gh issue create",                     "subcommand 'issue'"),
    ("gh workflow run x",                   "subcommand 'workflow'"),
    ("gh gist create",                      "subcommand 'gist'"),
    ("gh auth login",                       "only `gh auth status`"),
]
```

### `test_bash_allowlist_git_clone.py`

Allow:
- `git clone --depth=1 https://github.com/x/y skills/y`
- `git clone --depth=1 https://github.com/x/y tools/y`

Deny:
- `git clone --depth=1 https://github.com/x/y /tmp/x` (dest outside root).
- `git clone --depth=1 file:///etc/passwd dest` (scheme reject).
- `git clone https://github.com/x/y dest` (missing `--depth=1`).
- `git clone --depth=1 https://169.254.169.254/foo dest` (SSRF IP literal).

### `test_skill_installer_marketplace_list.py`

Mock `subprocess.run` for `gh api /repos/anthropics/skills/contents/skills`
to return a fixture JSON (17 dir entries + 1 file entry
`.gitattributes`):

```python
def test_list_filters_to_dirs_only(fake_gh_api):
    fake_gh_api.stdout = json.dumps([
        {"name": "skill-creator", "type": "dir",  "path": "skills/skill-creator"},
        {"name": "pdf",           "type": "dir",  "path": "skills/pdf"},
        {"name": ".gitattributes","type": "file", "path": "skills/.gitattributes"},
    ])
    out = list_skills()
    assert [e["name"] for e in out] == ["skill-creator", "pdf"]

def test_list_raises_on_404(fake_gh_api):
    fake_gh_api.returncode = 0  # gh rc=0 even on 404
    fake_gh_api.stdout = json.dumps({"message": "Not Found", "status": "404"})
    with pytest.raises(MarketplaceError, match="404"):
        list_skills()
```

### `test_skill_installer_marketplace_install.py`

End-to-end with three mocks:
- `gh api /repos/anthropics/skills/contents/skills/skill-creator/SKILL.md`
  returns base64 SKILL.md of a minimal fixture.
- `git clone --depth=1 ...` mocked to populate a pre-made bundle fixture.
- `atomic_install` target = `tmp_path / "skills" / "skill-creator"`.

Invariants:
- `install_from_marketplace("skill-creator")` returns the tree URL
  `https://github.com/anthropics/skills/tree/main/skills/skill-creator`.
- After driver runs preview -> install (same URL in both calls):
  `(skills_dir / "skill-creator" / "SKILL.md").exists()`; cache entry
  removed; `skills.dirty` sentinel touched.

### `test_skill_installer_toctou_detection.py`

Invariants:
- Mock fetch returns bundle v1 on first call, v2 on second (one byte
  differs in `SKILL.md`).
- Preview -> install in sequence -> exit code 7, stderr contains
  `bundle on source changed since preview`, cache entry deleted,
  `skills/<name>/` not created.

```python
def test_toctou_mismatch_exits_7(tmp_path, monkeypatch, capsys):
    urls = iter([_fake_fetch_v1, _fake_fetch_v2])
    monkeypatch.setattr("_lib.fetch.fetch_bundle", lambda url, dst: next(urls)(dst))
    rc = run_main(["preview", "https://github.com/example/repo/tree/main/foo"])
    assert rc == 0
    rc = run_main(["install", "--confirm", "--url",
                   "https://github.com/example/repo/tree/main/foo"])
    assert rc == 7
    assert "bundle on source changed" in capsys.readouterr().err
```

### `test_skill_installer_symlink_rejected.py`

Invariants:
- Fixture bundle with `scripts/evil -> /etc/passwd` (absolute) ->
  `ValidationError`.
- Fixture bundle with `scripts/loop -> ./SKILL.md` (relative-inside) ->
  `ValidationError`.
- `skills/<name>/` not created in either case.

### `test_bootstrap_skill_creator.py`

Core invariant: **`Daemon.start()` returns in <500 ms regardless of the
mocked bootstrap subprocess's behaviour.**

```python
@pytest.mark.asyncio
async def test_start_does_not_wait_for_bootstrap(fake_settings, monkeypatch):
    async def fake_create_subprocess_exec(*args, **kw):
        class _Proc:
            async def wait(self): await asyncio.sleep(60)   # never hit
            @property
            def stderr(self): return _FakeStderr()
        return _Proc()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(fake_settings)
    t0 = time.monotonic()
    await asyncio.wait_for(daemon.start(), timeout=0.5)
    assert time.monotonic() - t0 < 0.5
    # Background task must be alive (not GC'd) - verify via held set:
    assert any("_bootstrap_skill_creator_bg" in repr(t) for t in daemon._bg_tasks)
```

Other invariants:
- `shutil.which("gh") is None` -> `log.warning
  skill_creator_bootstrap_skipped_no_gh` and no subprocess spawn.
- `skills/skill-creator/` already exists -> no-op (no subprocess spawn).

### `test_sweep_run_dirs.py`

Invariants:
- Create fixtures with controlled mtimes via `os.utime`:
  `tmp/old` at `now - 3600*2`, `tmp/new` at `now - 600`,
  `installer-cache/stale` at `now - 86400*8`,
  `installer-cache/fresh` at `now - 86400`.
- Call `await daemon._sweep_run_dirs()`.
- Assert `tmp/old` and `installer-cache/stale` gone;
  `tmp/new` and `installer-cache/fresh` survive.
- `Daemon.start()` does not raise when `<data_dir>/run/` does not exist.

### `test_url_detector.py` (S-7 edge cases)

Regex: `r"https?://[^\s<>\[\]()]+|git@[^\s:]+:\S+"` — brackets excluded
from the URL body; trailing punctuation `.,;:` stripped in
post-processing; assertions on both match count and the stripped URL
value.

```python
_STRIP_TRAILING = ".,;:!?)"

def _detect_urls(text: str) -> list[str]:
    raw = _URL_RE.findall(text)
    return [u.rstrip(_STRIP_TRAILING) for u in raw]

def test_url_detector_plain_https():
    assert _detect_urls("посмотри https://example.com/x") == ["https://example.com/x"]

def test_url_detector_strips_trailing_punctuation():
    # S-7 case: "вот URL: https://github.com/x/y., дальше"
    assert _detect_urls("тут https://github.com/x/y., дальше") == [
        "https://github.com/x/y"
    ]

def test_url_detector_handles_parens_without_eating_url():
    # S-7 case: "см. (https://github.com/x/y)."
    assert _detect_urls("см. (https://github.com/x/y).") == [
        "https://github.com/x/y"
    ]

def test_url_detector_markdown_link():
    # S-7 case: "[link](https://github.com/x/y)"
    assert _detect_urls("[link](https://github.com/x/y)") == [
        "https://github.com/x/y"
    ]

def test_url_detector_preserves_encoded_chars():
    # URL-encoded spaces must stay.
    assert _detect_urls("file https://github.com/x/y%20z done") == [
        "https://github.com/x/y%20z"
    ]

def test_url_detector_two_urls():
    urls = _detect_urls("два: http://a.com и git@github.com:x/y")
    assert len(urls) == 2 and "http://a.com" in urls and "git@github.com:x/y" in urls

def test_url_detector_no_match():
    assert _detect_urls("без урла") == []

def test_conversation_store_gets_original_text(fake_bridge, fake_store):
    # Handler-level: DB sees original; bridge sees enriched.
    await handler.handle(IncomingMessage(text="поставь https://x/y"), emit=...)
    assert fake_store.last_append_text == "поставь https://x/y"
    assert "[system-note:" in fake_bridge.last_system_notes[0]
```

### `test_bash_allowlist_skills_scripts.py` (H-1)

```python
def test_python_skills_scripts_allowed(project_root):
    assert check_bash_command(
        "python skills/skill-creator/scripts/init_skill.py --name foo",
        project_root,
    ) is None

def test_uv_run_skills_scripts_allowed(project_root):
    assert check_bash_command(
        "uv run skills/skill-creator/scripts/init_skill.py",
        project_root,
    ) is None

def test_python_skills_dotdot_still_denied(project_root):
    assert check_bash_command(
        "python skills/../../../etc/passwd", project_root
    ) is not None

def test_python_outside_tools_and_skills_denied(project_root):
    # src/ and arbitrary paths still blocked.
    assert check_bash_command("python src/assistant/main.py", project_root) is not None
    assert check_bash_command("python /tmp/evil.py", project_root) is not None
```

### `test_daemon_stop_drains_bg_tasks.py` (S-1)

```python
@pytest.mark.asyncio
async def test_stop_awaits_bg_tasks_before_returning(fake_settings, monkeypatch):
    # Bootstrap coroutine that flips a flag after 100 ms.
    flag = {"done": False}
    async def fake_bootstrap(self):
        await asyncio.sleep(0.1)
        flag["done"] = True
    monkeypatch.setattr(Daemon, "_bootstrap_skill_creator_bg", fake_bootstrap)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/gh")

    daemon = Daemon(fake_settings)
    await daemon.start()
    assert flag["done"] is False
    await daemon.stop()
    assert flag["done"] is True
    assert not daemon._bg_tasks  # all discarded via done_callback
```

### `test_toctou_diff_in_stderr.py` (S-6)

```python
def test_toctou_stderr_contains_diff_lines(tmp_path, monkeypatch, capsys):
    # Preview populates cache/bundle/ with v1; install re-fetches v2;
    # stderr must carry "bundle on source changed ... REMOVED: ... ADDED: ... CHANGED: ..."
    urls = iter([_fake_fetch_v1_3files, _fake_fetch_v2_3files_swapped])
    monkeypatch.setattr("_lib.fetch.fetch_bundle", lambda u, d: next(urls)(d))
    assert run_main(["preview", URL]) == 0
    assert run_main(["install", "--confirm", "--url", URL]) == 7
    err = capsys.readouterr().err
    assert "bundle on source changed" in err
    assert "REMOVED:" in err or "ADDED:" in err or "CHANGED:" in err
```

---

## 6. Citations

- `plan/phase3/detailed-plan.md` - canonical spec.
- `plan/phase3/spike-findings.md` - S1-S5 raw empirical answers.
- `plan/phase2/implementation.md` - phase-2 hook API, manifest cache.
- `plan/phase2/summary.md` - phase-2 finished state.
- Live SDK artefact: `spikes/sdk_probe_posthook_report.json`
  (`completed: true`, PostToolUse fired with `tool_input.file_path` set).
- Marketplace artefact: `spikes/marketplace_probe_report.json`
  (`list_count: 17`, `samples_any_allowed_tools: false`).
- `github.com/anthropics/skills` - cloned 2026-04-17, depth=1. Audit
  output cited inline in `spike-findings section S1.c`.
- `docs.python.org/3/library/shutil.html#shutil.copytree` - symlinks param.
- `docs.python.org/3/library/pathlib.html#pathlib.Path.is_file` - follows
  symlinks by default.
- `cli.github.com/manual/gh_api` - `gh api` flags (`-X`, `-F`, `-f`,
  `--field`, `--raw-field`, `--input`, `--method`).
- `anthropic-ai/claude-agent-sdk-python` - `HookEvent` literal set,
  `PostToolUseHookInput` TypedDict (verified via
  `typing.get_type_hints(PostToolUseHookInput)` on 0.1.59).

---

## Open items (not blocking coder start)

None. All S-questions closed empirically on this host; the two probes
executed successfully; B2 semantics confirmed against current phase-2
`skills.py` source.
