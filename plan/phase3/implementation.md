---
version: v2
date: 2026-04-21
supersedes: v1 (2026-04-17, pre-wipe)
phase: 3
owner_decisions:
  - Q-D1 = (c) @tool-decorator pivot
  - BL-1 = (A) dogfood installer as @tool functions
  - BL-2 = gh/git fallback + .0xone-installed marker gate
  - Q1..Q12 = see description.md "Closed architectural decisions"
auth: OAuth via `claude` CLI (~/.claude/) — NO ANTHROPIC_API_KEY
sdk_pin: claude-agent-sdk>=0.1.59,<0.2 (live probe ran 0.1.63)
---

# Phase 3 — Implementation v2 (rewritten from scratch post-wipe)

## Revision history

- **v1** (2026-04-17, pre-wipe) — CLI-framed installer under
  `tools/skill-installer/`; stdlib-only; body-compliance-dependent. Killed
  by wipe commit + Q-D1=c pivot. v1 is preserved on disk only as the
  pre-wipe commit tree; do NOT patch-migrate from it.
- **v2** (2026-04-21) — **full rewrite against the dogfood pivot**. All
  installer logic lives inside the daemon process as
  `claude_agent_sdk.@tool` functions, registered via
  `create_sdk_mcp_server(name="installer", ...)` and passed to
  `ClaudeAgentOptions.mcp_servers={"installer": ...}`. Legacy
  `tools/skill-installer/` is **not created**. Bootstrap is direct Python
  (`_installer_core` helpers called in-process, no subprocess spawn and no
  model). Researcher spike **RQ1 PASSED all 6 criteria live** on SDK
  0.1.63 (2026-04-21) — see `plan/phase3/spike-findings.md` § "RQ1
  (2026-04-21)".

Companion docs (coder **must** read all four before starting):

- `plan/phase3/description.md` — 118-line summary; owner-decisions table.
- `plan/phase3/detailed-plan.md` — 991-line canonical spec (§1-§9
  rewritten for dogfood pivot); §1c is the authoritative acceptance
  contract for RQ1.
- `plan/phase3/spike-findings.md` — S1-S5 (frozen 2026-04-17) + RQ1
  (2026-04-21). Every spec claim in this document traces to one of those
  sections.
- `plan/phase2/implementation.md` + `plan/phase2/summary.md` — don't
  re-derive the phase-2 hook / manifest / ConversationStore API.

Non-negotiables:

- **OAuth only.** Bridge rejects `ANTHROPIC_API_KEY` in `.env`/settings.
  Phase 3 makes no change here.
- **Deploy + owner smoke test after phase 3** before starting phase 4 —
  batch-deploy killed the prior incarnation of this repo.
- **No new secrets.** Phase 3 talks only to GitHub (`gh api` anonymous +
  `git clone --depth=1` + optional `httpx`/`urllib.request`); no new
  stored credentials.

---

## §1 — Verified decisions

Everything in this table is either (a) backed by spike-findings.md, or
(b) a direct owner decision captured in description.md.

| # | Question | Decision | Evidence |
|---|---|---|---|
| Q-D1 | Phase-2 D1 enforcement strategy | **(c)** @tool-decorator pivot — skills stay prompt-expansions, memory/gh/media migrate to `@tool` in phase 4+ | owner 2026-04-21; description.md table |
| BL-1 | Phase-3 installer form | **(A)** dogfood — 7 `@tool` functions in `src/assistant/tools_sdk/installer.py` | owner 2026-04-21; description.md |
| BL-2 | gh/git presence + partial-install safety | `_fetch_tool()` helper in `_installer_core` chooses first-available of (gh, git), raises `FetchToolMissing` otherwise; `.0xone-installed` marker touched only after atomic rename succeeds | description.md §"BL-2 closeout" |
| Q1 | Own skill-creator CLI? | **No** — auto-bootstrap Anthropic's skill-creator bundle via direct-Python fetch+Write at first daemon start | description.md §Q1 |
| Q2 | GitHub fetch tool | `gh api` (read-only) via Bash allowlist + `git clone --depth=1` fallback + `httpx`/`urllib.request` for raw files | spike S2.a-e |
| Q3 | git via subprocess or GitPython? | `subprocess git clone --depth=1` — no GitPython dep | description.md §Q3 |
| Q4 | Preview UX | Plain-text "да/нет" in Telegram; cache-by-URL + re-fetch + SHA compare for TOCTOU | description.md §Q4 |
| Q5 | Limits | `MAX_FILES=100`, `MAX_TOTAL=10 MB`, `MAX_FILE=2 MB`, `FETCH_TIMEOUT=30 s`, `UV_SYNC_TIMEOUT=120 s` | spike S1.c — all 17 Anthropic bundles fit comfortably |
| Q6 | Sanity-run `tools/<X>/main.py --help` pre-install? | **No** — static-validate only (AST + frontmatter schema) | description.md §Q6 |
| Q7 | Tmpdir location | `<data_dir>/run/tmp/` (same FS as `skills/` for atomic rename); sweeper 1h in `Daemon.start()` | description.md §Q7 |
| Q8 | `uv sync` for `tools/<name>/` | Async via `asyncio.create_subprocess_exec`; polled via `@tool("skill_sync_status")` | description.md §Q8 |
| Q9 | Preview cache TTL | 7 days for `installer-cache/`, 1 hour for `tmp/` | description.md §Q12 |
| Q10 | URL detector | Regex `https?://|git@[^:]+:` in `handlers/message.py`; emits one-shot system-note enriching envelope (not DB write) | description.md §Q6 + §8 |
| Q11 | Sentinel scope | PostToolUse(Write/Edit) touches `data/run/skills.dirty` iff `file_path` resolves under `skills/` OR `tools/<X>/` | description.md §Q11 |
| Q12 | Marketplace config | Hardcoded `MARKETPLACE_URL = "https://github.com/anthropics/skills"`, `MARKETPLACE_BASE_PATH = "skills"` | description.md §Q3 + spike S1.a |
| RQ1.C1 | Both `@tool` MCP names visible to model at init | PASS — `mcp__installer__skill_preview`, `mcp__memory__memory_search` in `SystemMessage(init).data["tools"]` | spike-findings §RQ1, probe run 2026-04-21 |
| RQ1.C2 | Model invokes installer `@tool`; marker round-trips via ToolResultBlock | PASS — `tool_use_names=['ToolSearch','mcp__installer__skill_preview']`; marker `PREVIEW-OK: https://example.com/x` present | spike-findings §RQ1 |
| RQ1.C3 | Analogous for memory server placeholder | PASS — `MEMORY-OK: foo` returned | spike-findings §RQ1 |
| RQ1.C4 | `HookMatcher("Bash"|"Write")` do NOT fire on `mcp__` invocations | PASS — `bash_fired=[]`, `write_fired=[]` across 3 queries | spike-findings §RQ1 |
| RQ1.C5 | Regex matcher `mcp__installer__.*` AND exact matcher both fire | PASS — 2 regex fires, 1 exact fire, each tagged with the exact `tool_name` | spike-findings §RQ1 |
| RQ1.C6 | `setting_sources=["project"]` + programmatic `mcp_servers` + on-disk `.claude/settings.local.json` with junk `mcpServers` coexist | PASS — Q3 `stop_reason=end_turn`; SDK silently tolerates invalid disk-declared mcpServers entries | spike-findings §RQ1 |

**Pinned env (2026-04-21 live verification):**

| Package / CLI | Pin | Note |
|---|---|---|
| `claude-agent-sdk` | `>=0.1.59,<0.2` | Probe ran 0.1.63. `@tool`, `create_sdk_mcp_server`, `HookMatcher` all import from top-level package. |
| `claude` CLI | `>=2.1` | OAuth session. |
| `gh` CLI | `>=2.40` (optional) | If missing, marketplace-`@tool`s return `{"error": ..., "code": 9}`. |
| `git` (system) | present | Invoked via `git clone --depth=1` as Bash-allowlisted subprocess AND as Python subprocess inside installer helpers. |
| `httpx` or stdlib `urllib.request` | stdlib OK | Installer uses `urllib.request` for raw/download_url fetches — no new deps. |
| `pyyaml` | `>=6.0` | Phase-2 dep; reused for frontmatter parsing inside `_installer_core.validate_bundle`. |
| `uv` (system) | present | `uv sync --directory tools/<name>` as background task after install. |

No new runtime deps required. Verify at `uv sync` time during §3.1 step 10.

---

## §2 — Code snippets (authoritative)

File tree Phase 3 adds or modifies:

```
src/assistant/tools_sdk/
├── __init__.py                       # NEW — package init
├── _installer_core.py                # NEW — shared fetch/validate/install helpers
└── installer.py                      # NEW — 7 @tool functions + INSTALLER_SERVER export

src/assistant/bridge/
├── claude.py                         # CHANGED — mcp_servers wiring + sentinel check in _render_system_prompt
├── hooks.py                          # CHANGED — Bash allowlist extensions + make_posttool_hooks factory
└── skills.py                         # CHANGED — public invalidate_manifest_cache/touch_skills_dir + _normalize_allowed_tools

src/assistant/handlers/
└── message.py                        # CHANGED — URL detector augments envelope (not DB)

src/assistant/main.py                 # CHANGED — _bootstrap_skill_creator_bg + sweep_run_dirs fire-and-forget in start()

skills/skill-installer/
└── SKILL.md                          # NEW — discoverability aid only, no body-Bash

tests/
├── test_installer_tool_skill_preview.py
├── test_installer_tool_skill_install.py
├── test_installer_tool_unconfirmed_install.py
├── test_installer_tool_missing_fetch_tool.py
├── test_installer_tool_toctou.py
├── test_installer_tool_path_traversal.py
├── test_installer_tool_symlink_rejected.py
├── test_installer_tool_size_limits.py
├── test_installer_tool_ssrf_deny.py
├── test_installer_tool_uninstall.py
├── test_installer_mcp_registration.py
├── test_installer_marketplace_list.py
├── test_installer_marketplace_info.py
├── test_installer_marketplace_install.py
├── test_installer_marketplace_rate_limit.py
├── test_installer_skill_sync_status.py
├── test_bootstrap_direct_python.py
├── test_url_detector.py
├── test_posttool_sentinel.py
├── test_bash_allowlist_gh_api.py
├── test_bash_allowlist_git_clone.py
├── test_bash_allowlist_uv_sync.py
├── test_sweep_run_dirs.py
├── test_skill_permissive_default.py
└── test_skills_sentinel_hot_reload.py
```

No database migrations: phase 3 is filesystem-only. Schema 0002 (phase-2)
remains current.

### §2.1 `src/assistant/tools_sdk/__init__.py`

Phase-3 groundwork — populated with memory server in phase 4 and gh
server in phase 8.

```python
"""Home for @tool-decorator SDK custom tools (Q-D1=c pivot).

Phase 3 ships ``installer`` (7 tools). Phase 4+ adds ``memory``; phase 8+
adds ``gh``. Each submodule defines its own ``create_sdk_mcp_server(...)``
instance, exported under a descriptive constant (e.g. ``INSTALLER_SERVER``).
``ClaudeBridge._build_options`` imports and merges those constants into
``ClaudeAgentOptions.mcp_servers``.

Rationale: SKILL.md body-instruction compliance on Opus 4.7 is
unreliable (GH issues #39851, #41510). Moving the long tail of tool
logic to first-class SDK tools removes that compliance dependency
entirely. See ``plan/phase2/known-debt.md#D1`` for history.
"""
```

### §2.2 `src/assistant/tools_sdk/_installer_core.py`

Shared, trusted, in-process helpers. NOT decorated with `@tool` — only
callable from other Python code (installer `@tool` bodies + Daemon
bootstrap).

Imports:

```python
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml  # already in phase-2 venv
```

Module-level constants:

```python
MARKETPLACE_URL       = "https://github.com/anthropics/skills"
MARKETPLACE_REPO      = "anthropics/skills"
MARKETPLACE_BASE_PATH = "skills"
MARKETPLACE_REF       = "main"

MAX_TOTAL_BYTES   = 10 * 1024 * 1024
MAX_FILES         = 100
MAX_SINGLE_BYTES  = 2 * 1024 * 1024
FETCH_TIMEOUT_SEC = 30
UV_SYNC_TIMEOUT_SEC = 120
INSTALLER_CACHE_TTL_SEC  = 7 * 86400
INSTALLER_TMP_TTL_SEC    = 3600

_GIT_REPO_RE = re.compile(
    r"^(https://github\.com/[^/\s]+/[^/\s]+(?:\.git)?"
    r"|git@github\.com:[^/\s]+/[^/\s]+\.git)$"
)
_GITHUB_TREE_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$"
)
_RAW_SKILL_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/.+/SKILL\.md$"
)
_GIST_RE = re.compile(r"^https://gist\.github\.com/[^/]+/[0-9a-f]+$")

_SCHEME_WHITELIST = frozenset({"https"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

# @tool error-code constants (surface in tool result dicts):
CODE_URL_BAD         = 1
CODE_NOT_PREVIEWED   = 2
CODE_NOT_CONFIRMED   = 3
CODE_SSRF            = 4
CODE_VALIDATION      = 5
CODE_TOCTOU          = 7
CODE_NO_FETCH_TOOL   = 9
CODE_MARKETPLACE     = 10
CODE_NAME_INVALID    = 11

_HASH_SKIP_PARTS = frozenset({".git", "__pycache__", ".ruff_cache",
                              ".mypy_cache", ".pytest_cache"})
_HASH_SKIP_SUFFIXES = (".pyc", ".DS_Store")
```

Error types:

```python
class FetchToolMissing(RuntimeError):
    """Raised by _fetch_tool() when neither gh nor git is on PATH."""


class URLError(ValueError):
    """Canonical installer URL-rejection error."""


class ValidationError(ValueError):
    """Bundle failed static validation (schema, size, traversal, symlink)."""


class InstallError(RuntimeError):
    """Atomic install pipeline failure (tmp/rename race)."""


class MarketplaceError(RuntimeError):
    """gh api or git operation failed for marketplace queries."""
```

Tool-error helper:

```python
def tool_error(message: str, code: int) -> dict[str, Any]:
    """Return an MCP tool error response.

    S1 fix (wave-2): the SDK's `call_tool` wrapper only forwards
    `content[]` and `is_error` to the model. Extra dict keys like
    `error` and `code` are visible ONLY to Python tests that invoke
    `.handler(...)` directly; the model never sees them.

    The authoritative model-facing surface is the formatted text
    containing `(code=N)`. Do NOT rely on structured keys for
    model-facing error UX — asserting on `.code` in unit tests is fine,
    but any prompt engineering that references "the error code field"
    must instead parse the `(code=N)` suffix from the text.
    """
    return {
        "content": [
            {"type": "text", "text": f"error: {message} (code={code})"}
        ],
        "is_error": True,
        # Visible to Python tests via `.handler(...)` only; the SDK's
        # MCP boundary strips these before the model sees the result.
        "error": message,
        "code": code,
    }
```

Fetch tool dispatch (gh/git fallback):

```python
def _fetch_tool() -> Literal["gh", "git"]:
    """Pick first-available fetch tool. Raises FetchToolMissing if neither."""
    if shutil.which("gh"):
        return "gh"
    if shutil.which("git"):
        return "git"
    raise FetchToolMissing("neither gh nor git is on PATH")
```

Canonical URL (drops query + fragment; preserves case):

```python
def canonicalize_url(url: str) -> str:
    s = urlparse(url.strip())
    if url.startswith("git@"):
        return url.strip()
    if s.scheme.lower() not in _SCHEME_WHITELIST:
        raise URLError(f"unsupported scheme: {s.scheme!r}")
    scheme = s.scheme.lower()
    netloc = (s.netloc or "").lower().removeprefix("www.")
    path = (s.path or "/").rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def cache_key(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:16]
```

SSRF guard — imports `_ip_is_blocked` from phase-2 hooks module:

```python
from assistant.bridge.hooks import _ip_is_blocked  # re-export semantics


def check_host_safety(hostname: str) -> None:
    """Layer-2 SSRF check (resolve + category compare).

    Raises URLError on block. Same category set as WebFetch hook for
    consistency.
    """
    if not hostname:
        raise URLError("empty host")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, 443, 0, socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            return  # unresolvable -> caller's subprocess will also fail
        for _, _, _, _, sockaddr in infos:
            reason = _ip_is_blocked(str(sockaddr[0]))
            if reason:
                raise URLError(
                    f"SSRF: {hostname} -> {sockaddr[0]} ({reason})"
                )
        return
    reason = _ip_is_blocked(str(ip))
    if reason:
        raise URLError(f"SSRF: literal {hostname} ({reason})")
```

Fetch-bundle dispatch:

```python
async def fetch_bundle_async(url: str, dest: Path) -> None:
    """Dispatch to the right fetch backend.

    Raises URLError on bad URL or SSRF; FetchToolMissing when we need gh
    or git and neither is present. ``dest`` must not exist on entry.
    """
    dest.mkdir(parents=True, exist_ok=False)
    parsed = urlparse(url)
    if parsed.hostname:
        check_host_safety(parsed.hostname)

    if _GIT_REPO_RE.match(url):
        await _git_clone_async(url, dest)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        return
    if m := _GITHUB_TREE_RE.match(url):
        owner, repo, ref, path = m.groups()
        await _github_tree_download_async(owner, repo, ref, path, dest)
        return
    if _GIST_RE.match(url):
        await _gist_download_async(url, dest)
        return
    if _RAW_SKILL_RE.match(url):
        await _raw_single_file_async(url, dest)
        return
    raise URLError(f"unsupported URL form: {url!r}")


async def _git_clone_async(url: str, dest: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth=1", url, str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(
            proc.communicate(), timeout=FETCH_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise URLError("git clone timeout") from None
    if proc.returncode != 0:
        raise URLError(
            f"git clone failed rc={proc.returncode}: {err[:300]!r}"
        )


async def _github_tree_download_async(owner: str, repo: str, ref: str,
                                      path: str, dest: Path) -> None:
    """Use gh api if available; else shallow-clone + subtree extract."""
    tool = _fetch_tool()
    if tool == "gh":
        await _gh_recursive_contents_into(owner, repo, ref, path, dest)
        return
    with tempfile.TemporaryDirectory() as td:
        clone_dest = Path(td) / "clone"
        await _git_clone_async(
            f"https://github.com/{owner}/{repo}.git", clone_dest,
        )
        shutil.rmtree(clone_dest / ".git", ignore_errors=True)
        subtree = clone_dest / path
        if not subtree.is_dir():
            raise URLError(f"subtree {path!r} not found in cloned repo")
        for entry in subtree.iterdir():
            target = dest / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target)


async def _gh_api_async(endpoint: str) -> Any:
    """Run ``gh api <endpoint>`` asynchronously and parse JSON stdout.

    Imposes the same endpoint whitelist as the Bash hook — even though
    installer code is trusted, we never let model-supplied @tool args
    construct arbitrary endpoints. Depth-in-defense.
    """
    if not _GH_API_SAFE_ENDPOINT_RE.match(endpoint):
        raise MarketplaceError(
            f"endpoint {endpoint!r} not in installer whitelist"
        )
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", endpoint,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=FETCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MarketplaceError("gh api timeout")
    if proc.returncode != 0:
        # B10 fix (wave-2): rate-limit responses come back as HTTP 403 →
        # real `gh api` exits rc=4 with the JSON body on stdout and a
        # short summary on stderr. Detect rate-limit in BOTH streams and
        # surface a structured remediation message to the model.
        out_text = out.decode("utf-8", "replace")
        err_text = err.decode("utf-8", "replace")
        if (
            "rate limit" in out_text.lower()
            or "rate limit" in err_text.lower()
        ):
            raise MarketplaceError(
                "GitHub API rate-limited. Run `gh auth login` to raise "
                "limit from 60 to 5000 req/hour."
            )
        # Try to surface a structured GitHub message if stdout has JSON.
        try:
            for line in out_text.splitlines():
                s = line.strip()
                if s.startswith("{"):
                    payload_err = json.loads(s)
                    if (
                        isinstance(payload_err, dict)
                        and payload_err.get("message")
                    ):
                        raise MarketplaceError(
                            f"GitHub API: {payload_err['message']}"
                        )
        except json.JSONDecodeError:
            pass
        raise MarketplaceError(
            f"gh api rc={proc.returncode}: {err_text[:300]!r}"
        )
    payload = _parse_gh_json(out)
    if isinstance(payload, dict) and payload.get("message") and "status" in payload:
        status = payload.get("status")
        msg = payload.get("message", "")
        if status in ("403", "429") and "rate limit" in msg.lower():
            raise MarketplaceError(
                "GitHub rate-limited: "
                f"{msg}. Authenticate via `gh auth login` to raise "
                "the limit to 5000 req/hour."
            )
        raise MarketplaceError(f"GitHub API {status}: {msg}")
    return payload


def _parse_gh_json(stdout: bytes) -> Any:
    """Parse JSON from gh api stdout.

    S7 fix (wave-2): accepts raw bytes and handles BOTH common shapes:
    (a) single-line compact JSON (current `gh api` default), and
    (b) multi-line pretty-printed JSON (possible if the model invokes
        `gh api --jq .` or a future gh version switches to pretty
        output). A line-scanning approach alone would return only the
        first `{` line for pretty-printed responses.
    """
    text = stdout.decode("utf-8", "replace")
    # Attempt full-stdout parse first — succeeds for both single-line
    # and multi-line pretty-printed JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: find first line starting with { or [ and parse that
    # (handles stray warning lines emitted ahead of the JSON payload).
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    raise MarketplaceError(
        f"gh api returned unparseable output: {text[:200]!r}"
    )


async def _gh_recursive_contents_into(owner: str, repo: str, ref: str,
                                      path: str, dest: Path) -> None:
    """Walk the GitHub REST contents endpoint into dest/.

    Enforces MAX_FILES + MAX_TOTAL + MAX_SINGLE cumulatively.
    """
    queue: list[tuple[str, Path]] = [(path, dest)]
    total_bytes = 0
    total_files = 0
    while queue:
        sub_path, sub_dest = queue.pop()
        entries = await _gh_api_async(
            f"/repos/{owner}/{repo}/contents/{sub_path}?ref={ref}"
        )
        if not isinstance(entries, list):
            entries = [entries]  # single-file response shape
        sub_dest.mkdir(parents=True, exist_ok=True)
        for e in entries:
            typ = e.get("type")
            name = e.get("name", "")
            if not name or name.startswith(".."):
                raise ValidationError(f"rejected entry name {name!r}")
            if typ == "dir":
                queue.append((f"{sub_path}/{name}", sub_dest / name))
            elif typ == "file":
                size = int(e.get("size") or 0)
                if size > MAX_SINGLE_BYTES:
                    raise ValidationError(
                        f"file too large: {name} ({size} bytes)"
                    )
                total_bytes += size
                total_files += 1
                if total_files > MAX_FILES:
                    raise ValidationError(f"too many files (>{MAX_FILES})")
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValidationError(
                        f"bundle too large (>{MAX_TOTAL_BYTES})"
                    )
                data = await _fetch_file_bytes(e, owner, repo, ref)
                (sub_dest / name).write_bytes(data)
            elif typ in ("submodule", "symlink"):
                raise ValidationError(f"rejected entry type: {typ}")
            else:
                raise ValidationError(f"unknown entry type: {typ!r}")


async def _fetch_file_bytes(entry: dict, owner: str, repo: str, ref: str) -> bytes:
    """Return bytes for a single contents-API entry."""
    if entry.get("encoding") == "base64" and entry.get("content"):
        return base64.b64decode(entry["content"])
    url = entry.get("download_url")
    if not url:
        raise MarketplaceError(f"no download_url for {entry.get('name')}")
    parsed = urlparse(url)
    if parsed.hostname:
        check_host_safety(parsed.hostname)

    def _read() -> bytes:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SEC) as r:
            return r.read(MAX_SINGLE_BYTES + 1)

    data = await asyncio.to_thread(_read)
    if len(data) > MAX_SINGLE_BYTES:
        raise ValidationError(
            f"file too large via download_url: {entry.get('name')}"
        )
    return data


async def _gist_download_async(url: str, dest: Path) -> None:
    """Fetch gist file list via /gists/<id>; write each file into dest/."""
    gist_id = url.rstrip("/").rsplit("/", 1)[-1]
    # gist API is not in _GH_API_SAFE_ENDPOINT_RE; use direct urllib
    api_url = f"https://api.github.com/gists/{gist_id}"
    def _read() -> bytes:
        with urllib.request.urlopen(api_url, timeout=FETCH_TIMEOUT_SEC) as r:
            return r.read(MAX_TOTAL_BYTES + 1)
    payload = json.loads(await asyncio.to_thread(_read))
    files = payload.get("files") or {}
    total_bytes = 0
    for name, entry in files.items():
        if ".." in name or name.startswith("/"):
            raise ValidationError(f"rejected gist file name: {name!r}")
        content = entry.get("content")
        if content is None:
            raw_url = entry.get("raw_url")
            if not raw_url:
                continue
            data = await _fetch_file_bytes(
                {"download_url": raw_url}, "", "", "",
            )
        else:
            data = content.encode("utf-8")
        if len(data) > MAX_SINGLE_BYTES:
            raise ValidationError(f"gist file too large: {name}")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValidationError("gist too large")
        (dest / name).write_bytes(data)


async def _raw_single_file_async(url: str, dest: Path) -> None:
    """One-file fetch into dest/SKILL.md."""
    def _read() -> bytes:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SEC) as r:
            return r.read(MAX_SINGLE_BYTES + 1)
    data = await asyncio.to_thread(_read)
    if len(data) > MAX_SINGLE_BYTES:
        raise ValidationError("raw SKILL.md too large")
    (dest / "SKILL.md").write_bytes(data)
```

Bundle validator:

```python
def validate_bundle(bundle: Path) -> dict[str, Any]:
    """Static checks on a downloaded bundle dir. Returns a report dict.

    Raises ValidationError on fatal.
    """
    import ast

    root = bundle.resolve()
    skill_md = bundle / "SKILL.md"
    if not skill_md.is_file():
        raise ValidationError("SKILL.md missing at bundle root")

    total_bytes = 0
    total_files = 0
    for p in bundle.rglob("*"):
        if p.is_symlink():
            raise ValidationError(
                f"symlink not allowed: {p.relative_to(bundle)} "
                f"-> {os.readlink(p)}"
            )
        try:
            resolved = p.resolve()
        except OSError as exc:
            raise ValidationError(f"unresolvable path {p}: {exc}") from exc
        if not resolved.is_relative_to(root):
            raise ValidationError(f"path escapes bundle: {p}")
        if p.is_file():
            size = p.stat().st_size
            if size > MAX_SINGLE_BYTES:
                raise ValidationError(
                    f"file too large: {p.relative_to(bundle)}"
                )
            total_bytes += size
            total_files += 1
            if total_files > MAX_FILES:
                raise ValidationError("too many files")
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValidationError("bundle too large")

    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        raise ValidationError("SKILL.md lacks frontmatter")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(
            f"frontmatter YAML parse failed: {exc}"
        ) from exc
    if not isinstance(meta, dict):
        raise ValidationError("frontmatter is not a mapping")
    name = str(meta.get("name", "")).strip()
    description = str(meta.get("description", "")).strip()
    if not _NAME_RE.match(name):
        raise ValidationError(f"invalid skill name: {name!r}")
    if not description:
        raise ValidationError(
            "description is required in SKILL.md frontmatter"
        )
    allowed_tools = _normalize_allowed_tools_inline(meta.get("allowed-tools"))

    for py in bundle.rglob("*.py"):
        try:
            ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise ValidationError(
                f"py syntax error: {py.relative_to(bundle)}: {exc}"
            ) from exc

    has_tools_dir = (bundle / "tools").is_dir()
    return {
        "name": name,
        "description": description,
        "allowed_tools": allowed_tools,
        "file_count": total_files,
        "total_size": total_bytes,
        "has_tools_dir": has_tools_dir,
    }


def _normalize_allowed_tools_inline(raw: Any) -> list[str] | None:
    """Duplicate of bridge/skills.py::_normalize_allowed_tools.

    Kept inline to avoid an import cycle (bridge.skills imports
    bridge.hooks; _installer_core imports bridge.hooks; adding
    bridge.skills <- _installer_core would create a cycle at package
    import). Tests assert the two helpers produce identical outputs on
    a shared input matrix.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None
```

Deterministic tree hash:

```python
def sha256_of_tree(root: Path) -> str:
    h = hashlib.sha256()
    files = sorted(
        (p for p in root.rglob("*") if _should_hash(p, root)),
        key=lambda p: p.relative_to(root).as_posix(),
    )
    for p in files:
        rel = p.relative_to(root).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        h.update(b"\x00")
        data = p.read_bytes()
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _should_hash(p: Path, root: Path) -> bool:
    if not p.is_file() or p.is_symlink():
        return False
    if any(part in _HASH_SKIP_PARTS for part in p.relative_to(root).parts):
        return False
    if any(p.name.endswith(sfx) for sfx in _HASH_SKIP_SUFFIXES):
        return False
    return True
```

Atomic install:

```python
def atomic_install(tmp_bundle: Path, report: dict[str, Any],
                   *, project_root: Path) -> None:
    """Install a validated bundle into skills/<name>/ (+ optional tools/<name>/).

    Invariant: ``.0xone-installed`` marker is touched ONLY after every
    rename has succeeded. A partial failure leaves zero or one of the
    target directories present and NO marker, so bootstrap's
    marker-based idempotency guard re-attempts installation on next boot.
    """
    name = report["name"]
    if not _NAME_RE.match(name):
        raise InstallError(f"invalid name in report: {name!r}")
    skills_dst = project_root / "skills" / name
    tools_dst = project_root / "tools" / name
    if skills_dst.exists():
        raise InstallError(f"skill {name} already installed at {skills_dst}")
    if (tools_dst.exists() and (tmp_bundle / "tools").is_dir()):
        raise InstallError(f"tools/{name} already exists")

    stage_skill = project_root / "skills" / f".tmp-{name}-{uuid.uuid4().hex}"
    shutil.copytree(tmp_bundle, stage_skill, symlinks=True)

    inner_tools = stage_skill / "tools"
    stage_tools: Path | None = None
    if inner_tools.is_dir():
        stage_tools = project_root / "tools" / f".tmp-{name}-{uuid.uuid4().hex}"
        shutil.move(str(inner_tools), str(stage_tools))

    # B3 fix (wave-2): on rename failure, rollback ALL partial state
    # (both rename stages, both staging dirs). Sweeper catches leftover
    # `skills/.tmp-*` / `tools/.tmp-*` at next boot as a safety net.
    skills_dst_new_created = False
    try:
        stage_skill.rename(skills_dst)
        skills_dst_new_created = True
        if stage_tools is not None:
            stage_tools.rename(tools_dst)
    except OSError as exc:
        # Revert skills/<name> if the first rename succeeded but the
        # second (tools) rename failed.
        if skills_dst_new_created and skills_dst.exists():
            shutil.rmtree(skills_dst, ignore_errors=True)
        # Clean up any staging dirs still in place (either rename
        # hadn't happened yet, or a failed rename left stage_* behind).
        if stage_skill.exists():
            shutil.rmtree(stage_skill, ignore_errors=True)
        if stage_tools is not None and stage_tools.exists():
            shutil.rmtree(stage_tools, ignore_errors=True)
        raise InstallError(f"atomic rename failed: {exc}") from exc

    (skills_dst / ".0xone-installed").touch()
```

Staging dirs use `.tmp-<name>-<uuid>` prefixes so `_sweep_run_dirs`
catches any leftover `skills/.tmp-*` / `tools/.tmp-*` directories at
next boot (belt-and-suspenders — rollback above is synchronous).

Uv-sync background launcher:

```python
_BG_TASKS: set[asyncio.Task[Any]] = set()


async def spawn_uv_sync_bg(name: str, *, project_root: Path,
                           data_dir: Path) -> None:
    """Launch ``uv sync --directory tools/<name>`` as a background task.

    Writes {pending, ok, failed, timeout} status into
    <data_dir>/run/sync/<name>.status.json.
    """
    status_dir = data_dir / "run" / "sync"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / f"{name}.status.json"
    status_path.write_text(
        json.dumps({"status": "pending", "started_at": time.time()}),
        encoding="utf-8",
    )

    async def _runner() -> None:
        target = project_root / "tools" / name
        proc = await asyncio.create_subprocess_exec(
            "uv", "sync", f"--directory={target}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=UV_SYNC_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            status_path.write_text(
                json.dumps({"status": "timeout",
                            "finished_at": time.time()}),
                encoding="utf-8",
            )
            return
        if proc.returncode == 0:
            status_path.write_text(
                json.dumps({"status": "ok",
                            "finished_at": time.time()}),
                encoding="utf-8",
            )
        else:
            status_path.write_text(
                json.dumps({
                    "status": "failed",
                    "finished_at": time.time(),
                    "stderr": err.decode("utf-8", "replace")[:2000],
                }),
                encoding="utf-8",
            )

    task = asyncio.create_task(_runner(), name=f"uv-sync-{name}")
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
```

Sweeper:

```python
async def sweep_run_dirs(data_dir: Path) -> None:
    """Clean stale tmp/ (>1h) and installer-cache/ (>7d). Best-effort."""
    now = time.time()
    bases: list[tuple[Path, int]] = [
        (data_dir / "run" / "tmp", INSTALLER_TMP_TTL_SEC),
        (data_dir / "run" / "installer-cache", INSTALLER_CACHE_TTL_SEC),
    ]
    for base, ttl in bases:
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age <= ttl:
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass
```

Marketplace helpers:

```python
async def marketplace_list_entries() -> list[dict[str, Any]]:
    entries = await _gh_api_async(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}"
    )
    return [
        {"name": e["name"], "path": e["path"]}
        for e in entries
        if e.get("type") == "dir"
        and not str(e.get("name", "")).startswith(".")
    ]


async def marketplace_fetch_skill_md(name: str) -> str:
    payload = await _gh_api_async(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md"
    )
    assert payload.get("encoding") == "base64"
    return base64.b64decode(payload["content"]).decode("utf-8")


def marketplace_tree_url(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid marketplace name {name!r}")
    return (
        f"{MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/"
        f"{MARKETPLACE_BASE_PATH}/{name}"
    )
```

### §2.3 `src/assistant/tools_sdk/installer.py`

Seven `@tool` functions + server export. Uses a module-level context dict
configured once at daemon boot so the `@tool` handlers can resolve
project_root + data_dir without taking them as model-supplied arguments
(security).

```python
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from assistant.tools_sdk import _installer_core as core


_CTX: dict[str, Path] = {}


def configure_installer(*, project_root: Path, data_dir: Path) -> None:
    """Called once during daemon init before ClaudeBridge is first used."""
    _CTX["project_root"] = project_root
    _CTX["data_dir"] = data_dir


def _need_ctx() -> tuple[Path, Path]:
    try:
        return _CTX["project_root"], _CTX["data_dir"]
    except KeyError as exc:
        raise RuntimeError(
            "installer not configured; call configure_installer() first"
        ) from exc


def _cache_dir_for(canonical: str, data_dir: Path) -> Path:
    return data_dir / "run" / "installer-cache" / core.cache_key(canonical)
```

skill_preview:

```python
@tool(
    "skill_preview",
    "Fetch a skill bundle from URL, validate it, and return a preview. "
    "Must be called before skill_install so the user can confirm.",
    {"url": str},
)
async def skill_preview(args: dict[str, Any]) -> dict[str, Any]:
    project_root, data_dir = _need_ctx()
    url = args["url"]
    try:
        canonical = core.canonicalize_url(url)
    except core.URLError as exc:
        return core.tool_error(str(exc), core.CODE_URL_BAD)

    cdir = _cache_dir_for(canonical, data_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    bundle_dir = cdir / "bundle"
    if bundle_dir.exists():
        await asyncio.to_thread(shutil.rmtree, bundle_dir, True)

    try:
        await core.fetch_bundle_async(url, bundle_dir)
    except core.FetchToolMissing:
        return core.tool_error(
            "marketplace requires gh or git on PATH",
            core.CODE_NO_FETCH_TOOL,
        )
    except core.URLError as exc:
        return core.tool_error(str(exc), core.CODE_SSRF)

    try:
        report = await asyncio.to_thread(core.validate_bundle, bundle_dir)
    except core.ValidationError as exc:
        shutil.rmtree(cdir, ignore_errors=True)
        return core.tool_error(
            f"validation failed: {exc}", core.CODE_VALIDATION,
        )

    bundle_sha = await asyncio.to_thread(core.sha256_of_tree, bundle_dir)
    manifest_path = cdir / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "url": canonical,
            "bundle_sha": bundle_sha,
            "fetched_at": time.time(),
            "file_count": report["file_count"],
            "total_size": report["total_size"],
            "report": report,
        }),
        encoding="utf-8",
    )
    preview_text = (
        f"Skill: {report['name']}\n"
        f"Description: {report['description']}\n"
        f"Files: {report['file_count']}\n"
        f"Total size: {report['total_size']} bytes\n"
        f"Has tools/ subdir: {report['has_tools_dir']}\n"
        f"Source SHA: {bundle_sha[:16]}\n"
        f"To install: ask the user to confirm, then call "
        f"skill_install(url={canonical!r}, confirmed=true)."
    )
    return {
        "content": [{"type": "text", "text": preview_text}],
        "preview": {
            "name": report["name"],
            "description": report["description"],
            "file_count": report["file_count"],
            "total_size": report["total_size"],
            "has_tools_dir": report["has_tools_dir"],
            "source_sha": bundle_sha,
            "cache_key": cdir.name,
        },
        "confirm_hint": (
            f"call skill_install(url={canonical!r}, confirmed=true) "
            "after the user says yes"
        ),
    }
```

skill_install:

```python
@tool(
    "skill_install",
    "Install a previously previewed skill after the user has confirmed.",
    {"url": str, "confirmed": bool},
)
async def skill_install(args: dict[str, Any]) -> dict[str, Any]:
    project_root, data_dir = _need_ctx()
    if not args.get("confirmed"):
        return core.tool_error(
            "install requires confirmed=true; call skill_preview first "
            "and wait for the user to confirm in chat",
            core.CODE_NOT_CONFIRMED,
        )
    url = args["url"]
    try:
        canonical = core.canonicalize_url(url)
    except core.URLError as exc:
        return core.tool_error(str(exc), core.CODE_URL_BAD)
    cdir = _cache_dir_for(canonical, data_dir)
    manifest_path = cdir / "manifest.json"
    if not manifest_path.is_file():
        return core.tool_error(
            "no cached preview for this URL; call skill_preview first",
            core.CODE_NOT_PREVIEWED,
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify = cdir / "verify"
    if verify.exists():
        shutil.rmtree(verify, ignore_errors=True)
    try:
        try:
            await core.fetch_bundle_async(url, verify)
        except core.FetchToolMissing:
            return core.tool_error(
                "marketplace requires gh or git on PATH",
                core.CODE_NO_FETCH_TOOL,
            )
        except core.URLError as exc:
            return core.tool_error(str(exc), core.CODE_SSRF)

        new_sha = await asyncio.to_thread(core.sha256_of_tree, verify)
        if new_sha != manifest["bundle_sha"]:
            shutil.rmtree(cdir, ignore_errors=True)
            return core.tool_error(
                "bundle on source changed since preview; "
                "call skill_preview again",
                core.CODE_TOCTOU,
            )
        try:
            report = await asyncio.to_thread(core.validate_bundle, verify)
        except core.ValidationError as exc:
            return core.tool_error(
                f"validation failed on re-fetch: {exc}",
                core.CODE_VALIDATION,
            )
        await asyncio.to_thread(
            core.atomic_install, verify, report,
            project_root=project_root,
        )
        sentinel = data_dir / "run" / "skills.dirty"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()

        sync_pending = False
        if report["has_tools_dir"]:
            await core.spawn_uv_sync_bg(
                report["name"],
                project_root=project_root,
                data_dir=data_dir,
            )
            sync_pending = True

        return {
            "content": [
                {"type": "text", "text": f"installed {report['name']}"}
            ],
            "installed": True,
            "name": report["name"],
            "sync_pending": sync_pending,
        }
    finally:
        shutil.rmtree(cdir, ignore_errors=True)
```

skill_uninstall:

```python
@tool(
    "skill_uninstall",
    "Remove an installed skill by name. Requires explicit confirmation.",
    {"name": str, "confirmed": bool},
)
async def skill_uninstall(args: dict[str, Any]) -> dict[str, Any]:
    project_root, data_dir = _need_ctx()
    name = args.get("name", "")
    if not core._NAME_RE.match(name):
        return core.tool_error(
            f"invalid name: {name!r}", core.CODE_NAME_INVALID,
        )
    if not args.get("confirmed"):
        return core.tool_error(
            "uninstall requires confirmed=true", core.CODE_NOT_CONFIRMED,
        )
    skill_path = project_root / "skills" / name
    tool_path = project_root / "tools" / name
    existed = skill_path.exists() or tool_path.exists()
    if skill_path.exists():
        shutil.rmtree(skill_path, ignore_errors=True)
    if tool_path.exists():
        shutil.rmtree(tool_path, ignore_errors=True)
    sentinel = data_dir / "run" / "skills.dirty"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    if not existed:
        return {
            "content": [
                {"type": "text",
                 "text": f"skill {name!r} was not installed"},
            ],
            "removed": False,
            "reason": "not installed",
        }
    return {
        "content": [{"type": "text", "text": f"removed {name}"}],
        "removed": True,
        "name": name,
    }
```

Marketplace tools:

```python
@tool("marketplace_list",
      "List the Anthropic skill marketplace entries.",
      {})
async def marketplace_list(args: dict[str, Any]) -> dict[str, Any]:
    try:
        entries = await core.marketplace_list_entries()
    except core.FetchToolMissing:
        return core.tool_error(
            "marketplace requires gh or git on PATH",
            core.CODE_NO_FETCH_TOOL,
        )
    except core.MarketplaceError as exc:
        return core.tool_error(str(exc), core.CODE_MARKETPLACE)
    text = "\n".join(f"- {e['name']}" for e in entries)
    return {
        "content": [{"type": "text", "text": text or "(empty marketplace)"}],
        "entries": entries,
    }


@tool("marketplace_info",
      "Fetch the SKILL.md for a marketplace skill by name.",
      {"name": str})
async def marketplace_info(args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name", "")
    if not core._NAME_RE.match(name):
        return core.tool_error(
            f"invalid name: {name!r}", core.CODE_NAME_INVALID,
        )
    try:
        body = await core.marketplace_fetch_skill_md(name)
    except core.FetchToolMissing:
        return core.tool_error(
            "marketplace requires gh or git on PATH",
            core.CODE_NO_FETCH_TOOL,
        )
    except core.MarketplaceError as exc:
        return core.tool_error(str(exc), core.CODE_MARKETPLACE)
    return {
        "content": [{"type": "text", "text": body}],
        "name": name,
    }


@tool("marketplace_install",
      "Convenience: preview pipeline for a marketplace skill by name. "
      "The user still has to confirm by asking the model to call skill_install.",
      {"name": str})
async def marketplace_install(args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name", "")
    if not core._NAME_RE.match(name):
        return core.tool_error(
            f"invalid name: {name!r}", core.CODE_NAME_INVALID,
        )
    try:
        url = core.marketplace_tree_url(name)
    except ValueError as exc:
        return core.tool_error(str(exc), core.CODE_NAME_INVALID)
    # Delegate to skill_preview's underlying coroutine. The @tool
    # decorator attaches the wrapped async callable at `.handler` on
    # the SdkMcpTool record — see `inspect.signature(tool)`.
    return await skill_preview.handler({"url": url})  # type: ignore[attr-defined]


@tool("skill_sync_status",
      "Check the async `uv sync` status for a recently installed skill.",
      {"name": str})
async def skill_sync_status(args: dict[str, Any]) -> dict[str, Any]:
    _, data_dir = _need_ctx()
    name = args.get("name", "")
    if not core._NAME_RE.match(name):
        return core.tool_error(
            f"invalid name: {name!r}", core.CODE_NAME_INVALID,
        )
    status_path = data_dir / "run" / "sync" / f"{name}.status.json"
    if not status_path.is_file():
        return {
            "content": [
                {"type": "text", "text": f"no sync record for {name}"}
            ],
            "status": "unknown",
        }
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    return {
        "content": [
            {"type": "text",
             "text": json.dumps(payload, ensure_ascii=False)}
        ],
        **payload,
    }


INSTALLER_SERVER = create_sdk_mcp_server(
    name="installer",
    version="0.1.0",
    tools=[
        skill_preview,
        skill_install,
        skill_uninstall,
        marketplace_list,
        marketplace_info,
        marketplace_install,
        skill_sync_status,
    ],
)

# S6 fix (wave-2): export the canonical tool-name tuple so
# `ClaudeBridge._build_options` does not duplicate this list. Any new
# installer tool added to INSTALLER_SERVER above MUST be reflected
# here; the `test_installer_mcp_registration.py` subset-assert keeps
# the two in sync.
INSTALLER_TOOL_NAMES: tuple[str, ...] = (
    "mcp__installer__skill_preview",
    "mcp__installer__skill_install",
    "mcp__installer__skill_uninstall",
    "mcp__installer__marketplace_list",
    "mcp__installer__marketplace_info",
    "mcp__installer__marketplace_install",
    "mcp__installer__skill_sync_status",
)
```

Dogfood properties validated by RQ1:

- Model sees `mcp__installer__<name>` in `SystemMessage(init).data["tools"]`.
- Narrow-scoped `HookMatcher("Bash"|"Write"|"Edit"|...)` does not fire
  on `mcp__installer__*` calls (RQ1 C4).
- `HookMatcher(matcher="mcp__installer__.*")` IS available for phase 4+
  audit logging (RQ1 C5).
- Confirmation model: model must call `skill_preview` first, then
  receive a user "yes" in chat, then call `skill_install(confirmed=true)`
  — enforced by `CODE_NOT_CONFIRMED` when `confirmed=false`.

### §2.4 `src/assistant/bridge/claude.py` changes

Top-of-file imports:

```python
from assistant.tools_sdk.installer import (
    INSTALLER_SERVER,
    INSTALLER_TOOL_NAMES,
)
from assistant.bridge.skills import (
    build_manifest,
    invalidate_manifest_cache,
    touch_skills_dir,
)
from assistant.bridge.hooks import make_posttool_hooks
```

Modify `_build_options`:

```python
def _build_options(self, *, system_prompt: str) -> ClaudeAgentOptions:
    pr = self._settings.project_root
    dd = self._settings.data_dir
    hooks: dict[HookEventName, list[HookMatcher]] = {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[make_bash_hook(pr)]),
            *[HookMatcher(matcher=t, hooks=[make_file_hook(pr)]) for t in FILE_TOOL_NAMES],
            HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
        ],
        "PostToolUse": make_posttool_hooks(pr, dd),
    }
    thinking_kwargs: dict[str, Any] = {}
    if self._settings.claude.thinking_budget > 0:
        thinking_kwargs["max_thinking_tokens"] = self._settings.claude.thinking_budget
        thinking_kwargs["effort"] = self._settings.claude.effort
    system_prompt_preset: SystemPromptPreset = {
        "type": "preset",
        "preset": "claude_code",
        "append": system_prompt,
        "exclude_dynamic_sections": True,
    }
    return ClaudeAgentOptions(
        cwd=str(pr),
        setting_sources=["project"],
        max_turns=self._settings.claude.max_turns,
        # S6 fix (wave-2): derive installer tool names from the
        # installer module's constant instead of duplicating here;
        # adding a new @tool now requires only one edit in installer.py.
        allowed_tools=[
            "Bash", "Read", "Write", "Edit", "Glob", "Grep",
            "WebFetch", "Skill",
            *INSTALLER_TOOL_NAMES,
        ],
        mcp_servers={"installer": INSTALLER_SERVER},
        hooks=hooks,
        system_prompt=system_prompt_preset,
        **thinking_kwargs,
    )
```

Modify `_render_system_prompt`:

```python
def _render_system_prompt(self) -> str:
    self._check_skills_sentinel()
    template = (
        self._settings.project_root
        / "src" / "assistant" / "bridge" / "system_prompt.md"
    ).read_text(encoding="utf-8")
    manifest = build_manifest(self._settings.project_root / "skills")
    return template.format(
        project_root=str(self._settings.project_root),
        skills_manifest=manifest,
    )


def _check_skills_sentinel(self) -> None:
    sentinel = self._settings.data_dir / "run" / "skills.dirty"
    if not sentinel.exists():
        return
    invalidate_manifest_cache()
    touch_skills_dir(self._settings.project_root / "skills")
    try:
        sentinel.unlink()
    except FileNotFoundError:
        pass
    log.info("skills_cache_invalidated_via_sentinel")
```

Two concurrent turns both seeing the sentinel both call
`invalidate_manifest_cache` (dict.clear — idempotent) and
`touch_skills_dir` (os.utime — idempotent); one wins `unlink`, the other
catches `FileNotFoundError`.

### §2.5 `src/assistant/bridge/hooks.py` changes

Extend `BASH_ALLOWLIST_PREFIXES` (each entry MUST end with space or `/`
per existing `BW2` invariant — that import-time assert still holds):

```python
BASH_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "python tools/",
    "python3 tools/",
    "uv run tools/",
    # NEW phase-3 entries — Anthropic skill-creator ships scripts/*.py
    "python skills/",
    "python3 skills/",
    "uv run skills/",
    "git status ",
    "git log ",
    "git diff ",
    "ls ",
    "echo ",
)
```

Argv-level validators for `gh`, `git clone`, `uv sync`:

```python
_GH_ALLOWED_SUBCMDS = frozenset({"api", "auth"})
_GH_AUTH_ALLOWED_SUBSUB = frozenset({"status"})
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

# B11 fix (wave-2): deny-list of flags is NOT safe — `--hostname evil.com`
# was absent from the list, which would redirect `gh api` to an
# attacker-controlled host and exfiltrate the owner's OAuth token.
# We now use a FLAG ALLOW-LIST: only `-H Accept:*` headers are permitted
# alongside the endpoint path. Every other flag (including --hostname,
# --paginate, -X, -F, -f, --input, --method, ...) is denied by default.
_GH_API_ALLOWED_HEADER_PREFIX = "accept:"


def _validate_gh_argv(argv: list[str]) -> str | None:
    """Return error string if argv is not a safe read-only gh invocation.

    Only two invocations pass:
        gh auth status
        gh api [<-H Accept:...> ...] /<read-only endpoint>

    Every other gh subcommand and every non-Accept `-H` header is denied.
    Any unknown flag (`--hostname`, `--paginate`, `-X`, ...) is denied
    by default — the allow-list is authoritative.
    """
    if len(argv) < 2 or argv[0] != "gh":
        return "not gh command"
    sub = argv[1]
    if sub == "auth":
        # `gh auth status` has no endpoint and no flags that matter.
        if len(argv) == 3 and argv[2] in _GH_AUTH_ALLOWED_SUBSUB:
            return None
        return "only `gh auth status` is allowed"
    if sub != "api":
        return (
            f"gh {sub!r}: only `gh api` and `gh auth status` are whitelisted"
        )
    # sub == "api" — walk argv[2:] and accept only endpoint + -H Accept:*.
    i = 2
    saw_endpoint = False
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("/"):
            if saw_endpoint:
                return "gh api: multiple endpoints not allowed"
            if not _GH_API_SAFE_ENDPOINT_RE.match(tok):
                return (
                    f"gh api: endpoint {tok!r} not in read-only whitelist"
                )
            saw_endpoint = True
            i += 1
            continue
        if tok == "-H":
            if i + 1 >= len(argv):
                return "gh api: -H requires header argument"
            hdr = argv[i + 1]
            if not hdr.lower().startswith(_GH_API_ALLOWED_HEADER_PREFIX):
                return (
                    f"gh api: -H {hdr!r}: only Accept headers allowed"
                )
            i += 2
            continue
        return f"gh api: flag {tok!r} not in read-only allow-list"
    if not saw_endpoint:
        return "gh api requires endpoint path"
    return None

# DEPRECATED: _GH_FORBIDDEN_FLAGS is no longer consulted.
# Retained only as a breadcrumb for the pre-B11 deny-list approach.
# The whitelist above is authoritative — do NOT re-introduce deny-list
# checks; `--hostname` escaped the old deny-list and leaked the OAuth
# token. If you need to loosen this, extend the allow-list explicitly.
_GH_FORBIDDEN_FLAGS: frozenset[str] = frozenset()


def _validate_git_clone_argv(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 4:
        return "git clone requires --depth=1 URL DEST"
    if argv[2] != "--depth=1":
        return "only --depth=1 is allowed for git clone"
    url = argv[3]
    if not (url.startswith("https://") or url.startswith("git@github.com:")):
        return "only https:// or git@github.com: URLs are allowed"
    if len(argv) < 5:
        return "git clone requires DEST"
    dest = argv[4]
    try:
        dp = Path(dest)
        resolved = (project_root / dp).resolve() if not dp.is_absolute() else dp.resolve()
    except OSError as exc:
        return f"invalid dest path: {exc}"
    if not resolved.is_relative_to(project_root.resolve()):
        return "git clone dest must be inside project_root"
    return None


def _validate_uv_sync_argv(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "uv requires subcommand"
    if argv[1] != "sync":
        return None  # other uv subcommands covered by existing `uv run` prefix
    dir_arg = next((a for a in argv[2:] if a.startswith("--directory")), None)
    if not dir_arg:
        return "uv sync requires --directory=tools/<name> or skills/<name>"
    if "=" in dir_arg:
        path = dir_arg.split("=", 1)[1]
    else:
        idx = argv.index(dir_arg)
        if idx + 1 >= len(argv):
            return "uv sync --directory requires a value"
        path = argv[idx + 1]
    try:
        full = (project_root / path).resolve()
    except OSError as exc:
        return f"invalid directory path: {exc}"
    pr = project_root.resolve()
    if not (full.is_relative_to(pr / "tools") or full.is_relative_to(pr / "skills")):
        return "uv sync --directory must target tools/ or skills/"
    return None
```

Integrate into `_bash_allowlist_check`:

```python
def _bash_allowlist_check(cmd: str, project_root: Path) -> str | None:
    import shlex
    stripped = cmd.strip()
    if not stripped:
        return "empty command"
    if stripped in BASH_ALLOWLIST_EXACT:
        return None
    if any(stripped.startswith(p) for p in BASH_ALLOWLIST_PREFIXES):
        return None
    try:
        argv = shlex.split(stripped)
    except ValueError:
        return "unparseable Bash command"
    if not argv:
        return "empty argv"
    if argv[0] == "gh":
        return _validate_gh_argv(argv)
    if argv[0] == "git" and len(argv) > 1 and argv[1] == "clone":
        return _validate_git_clone_argv(argv, project_root)
    if argv[0] == "uv" and len(argv) > 1 and argv[1] == "sync":
        return _validate_uv_sync_argv(argv, project_root)
    if stripped.startswith("cat "):
        args = stripped[4:].strip().split()
        ok, reason = _cat_targets_ok(args, project_root)
        if ok:
            return None
        return reason or "cat target outside project_root"
    return (
        "Bash command not in allowlist. Add to BASH_ALLOWLIST_* or file "
        "an RFC. Note: installer @tool calls bypass this allowlist "
        "entirely (enforced at @tool function arg-validation time)."
    )
```

PostToolUse factory:

```python
def _is_inside_skills_or_tools(raw_path: str, project_root: Path) -> bool:
    if not raw_path or ".." in Path(raw_path).parts:
        return False
    try:
        p = Path(raw_path).expanduser()
        resolved = p.resolve() if p.is_absolute() else (project_root / p).resolve()
    except (OSError, ValueError):
        return False
    root = project_root.resolve()
    for sub in ("skills", "tools"):
        try:
            if resolved.is_relative_to(root / sub):
                return True
        except ValueError:
            continue
    return False


def make_posttool_hooks(project_root: Path, data_dir: Path) -> list[HookMatcher]:
    from claude_agent_sdk import HookMatcher
    sentinel = data_dir / "run" / "skills.dirty"

    async def on_write_edit(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, ctx
        raw = cast(dict[str, Any], input_data)
        ti = raw.get("tool_input") or {}
        file_path = str(ti.get("file_path") or "")
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
                log.warning(
                    "posttool_sentinel_touch_failed", error=repr(exc),
                )
        return cast(HookJSONOutput, {})

    return [
        HookMatcher(matcher="Write", hooks=[on_write_edit]),
        HookMatcher(matcher="Edit", hooks=[on_write_edit]),
    ]
```

**RS-3 clarification (re-verified 2026-04-21).** `HookMatcher.matcher`
is a single pattern on `tool_name`. There is no per-file-path matcher
API, so file-path filtering MUST happen inside `on_write_edit`.
Confirmed via `HookMatcher.__dataclass_fields__` + RQ1 C5.

### §2.6 `src/assistant/bridge/skills.py` additions

At module bottom (keep `_FRONT_RE`, `_MANIFEST_CACHE`, `parse_skill`,
`_manifest_cache_key`, `build_manifest` as-is; only add new publics +
refactor `parse_skill` to emit warnings):

```python
import os
from assistant.logger import get_logger

log = get_logger("bridge.skills")


def invalidate_manifest_cache() -> None:
    """Drop the whole manifest cache dict.

    Called from ClaudeBridge._check_skills_sentinel when
    data/run/skills.dirty is detected. dict.clear is atomic under the
    GIL on CPython; the daemon's single-event-loop model gives us even
    stronger guarantees.
    """
    _MANIFEST_CACHE.clear()


def touch_skills_dir(skills_dir: Path) -> None:
    """Bump mtime on skills_dir so the next _manifest_cache_key returns
    a strictly higher max value — forcing a rebuild even if the cache
    dict was NOT cleared."""
    try:
        os.utime(skills_dir, None)
    except OSError:
        pass


def _normalize_allowed_tools(raw: Any) -> list[str] | None:
    """Three-way result:

      missing / None / malformed  -> None  (permissive default sentinel)
      scalar str                  -> [str(raw)]
      list                        -> [str(x) for x in raw]
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None
```

Refactor `parse_skill` (existing code) to use the helper + warn:

```python
def parse_skill(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        return {}
    name = meta.get("name", path.parent.name)
    description = (meta.get("description") or "").strip()
    allowed = _normalize_allowed_tools(meta.get("allowed-tools"))
    if allowed is None:
        log.warning(
            "skill_permissive_default",
            skill_name=name,
            reason="allowed-tools missing in SKILL.md; global baseline applies",
        )
    elif allowed == []:
        log.warning(
            "skill_lockdown_not_enforced",
            skill_name=name,
            reason="phase 3 uses global baseline; per-skill gating is phase 4",
        )
    return {
        "name": name,
        "description": description,
        "allowed_tools": allowed,
    }
```

### §2.7 `src/assistant/handlers/message.py` changes

Add URL detector + envelope enrichment. DB write unchanged.

```python
import re as _re

# B9 fix (wave-2): trailing punctuation was captured into the URL, so
# "see https://github.com/foo/bar." would yield the literal string
# "https://github.com/foo/bar." (trailing dot) — which then fails
# GitHub routing and confuses the system-note hint to the model.
# Approach: keep the broad `\S+` match, then strip trailing punctuation
# characters that are almost never part of a real URL.
_URL_RE = _re.compile(r"https?://\S+|git@[^\s:]+:\S+", _re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)\\]\"'"


def _detect_urls(text: str) -> list[str]:
    urls: list[str] = []
    for m in _URL_RE.finditer(text):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        if u:
            urls.append(u)
    return urls


# Inside ClaudeHandler._run_turn, before calling bridge.ask:
user_text_original = msg.text                   # goes to DB unchanged
urls = _detect_urls(msg.text)
if urls:
    hint = (
        "\n\n[system-note: the user's message contains URL(s) "
        f"{urls[:3]!r}. If one looks like a GitHub skill bundle, "
        "consider calling `mcp__installer__skill_preview(url=...)` to "
        "fetch a preview before asking the user to confirm install. "
        "Otherwise treat the URL as reference content.]"
    )
    user_text_for_sdk = msg.text + hint
else:
    user_text_for_sdk = msg.text
# existing DB write uses user_text_original;
# bridge.ask is invoked with user_text_for_sdk as the envelope content.
```

### §2.8 `src/assistant/main.py` Daemon additions

```python
# new imports
from assistant.tools_sdk import installer as _installer_mod
from assistant.tools_sdk import _installer_core as _core
import shutil as _shutil

class Daemon:
    # ... existing fields ...
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._conn: aiosqlite.Connection | None = None
        self._adapter: TelegramAdapter | None = None
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        await self._preflight_claude_auth()
        assert_no_custom_claude_settings(
            self._settings.project_root,
            logging.getLogger("bridge.bootstrap"),
        )
        ensure_skills_symlink(self._settings.project_root)

        # NEW: configure installer BEFORE ClaudeBridge is constructed.
        _installer_mod.configure_installer(
            project_root=self._settings.project_root,
            data_dir=self._settings.data_dir,
        )
        # NEW: fire-and-forget — sweep stale + bootstrap skill-creator.
        self._spawn_bg(_core.sweep_run_dirs(self._settings.data_dir))
        self._spawn_bg(self._bootstrap_skill_creator_bg())

        # ... existing DB / bridge / adapter wiring ...

    def _spawn_bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _bootstrap_skill_creator_bg(self) -> None:
        skills_dir = self._settings.project_root / "skills"
        marker = skills_dir / "skill-creator" / ".0xone-installed"
        if marker.is_file():
            return
        try:
            tool = _core._fetch_tool()
        except _core.FetchToolMissing:
            log.warning("skill_creator_bootstrap_skipped_no_gh_nor_git")
            return
        log.info("skill_creator_bootstrap_starting", via=tool)
        tmp = self._settings.data_dir / "run" / "tmp" / "skill-creator-boot"
        if tmp.exists():
            _shutil.rmtree(tmp, ignore_errors=True)
        try:
            async def _run() -> None:
                url = _core.marketplace_tree_url("skill-creator")
                await _core.fetch_bundle_async(url, tmp)
                report = await asyncio.to_thread(_core.validate_bundle, tmp)
                await asyncio.to_thread(
                    _core.atomic_install, tmp, report,
                    project_root=self._settings.project_root,
                )
                (self._settings.data_dir / "run" / "skills.dirty").touch()
            await asyncio.wait_for(_run(), timeout=120)
            log.info("skill_creator_bootstrap_ok")
        except asyncio.TimeoutError:
            log.warning("skill_creator_bootstrap_timeout")
        except Exception as exc:  # noqa: BLE001 — surface everything, never re-raise
            log.warning("skill_creator_bootstrap_failed", error=str(exc))
        finally:
            if tmp.exists():
                _shutil.rmtree(tmp, ignore_errors=True)
```

### §2.9 `skills/skill-installer/SKILL.md`

Discoverability aid only — body describes when to use which
`mcp__installer__*` tool. Body **must NOT** instruct "run via Bash"
— tools are first-class MCP.

```markdown
---
name: skill-installer
description: "Manage skill installation from GitHub/marketplace. Use when the user wants to preview, install, uninstall, or browse skills."
---

# skill-installer

Installer exposes **seven** SDK custom tools (no CLI). Invoke them
directly as `mcp__installer__<name>`:

- `skill_preview(url)` — fetch + validate a skill bundle, return a
  preview. Trigger phrases: "поставь скилл <url>", "глянь что за
  скилл", "preview this skill".
- `skill_install(url, confirmed)` — install after the user explicitly
  confirms. NEVER invoke with `confirmed=true` without the user saying
  "да" / "yes" in the immediately previous turn.
- `skill_uninstall(name, confirmed)` — remove an installed skill.
- `marketplace_list()` — list Anthropic's official skills. Trigger
  phrases: "какие скилы есть", "что в marketplace".
- `marketplace_info(name)` — show SKILL.md for one marketplace skill.
- `marketplace_install(name)` — shortcut: build the tree-URL for the
  marketplace skill and run the preview pipeline; still requires an
  explicit `skill_install(..., confirmed=true)` in a follow-up turn.
- `skill_sync_status(name)` — check background `uv sync` progress
  after an install that returned `sync_pending: true`.

## Preview-confirm invariant

Never call `skill_install(url=X, confirmed=true)` in the same turn as
the initial `skill_preview(url=X)`. The owner must see the preview
and respond affirmatively in a subsequent message. If the cache entry
has expired (7 days) between preview and install, re-run `skill_preview`.

## Brand-new skills

Creating a skill from scratch is a different job — Anthropic's own
`skill-creator` skill (auto-installed at first daemon boot) handles
that. When the user says "сделай скилл для X", invoke `skill-creator`
guidance instead, which writes files via the built-in `Write` tool.
```

### §2.10 Migration / schema

**No new schema.** Phase 3 adds no persistent entities — installer
state is either on-disk files (`skills/`, `tools/`, `data/run/`) or
SDK-transient turn state. Schema remains at `0002` (phase 2).

---

## §3 — Step-by-step execution recipe

### §3.1 Bootstrap sequence

1. `mkdir -p src/assistant/tools_sdk && touch src/assistant/tools_sdk/__init__.py`
   then write the docstring from §2.1.
2. Write `src/assistant/tools_sdk/_installer_core.py` per §2.2.
3. Write `src/assistant/tools_sdk/installer.py` per §2.3.
4. Edit `src/assistant/bridge/claude.py` per §2.4 (imports +
   `_build_options` + `_render_system_prompt` + `_check_skills_sentinel`).
5. Edit `src/assistant/bridge/hooks.py` per §2.5 (extend allowlist,
   add validators, add `make_posttool_hooks`).
6. Edit `src/assistant/bridge/skills.py` per §2.6
   (`invalidate_manifest_cache`, `touch_skills_dir`,
   `_normalize_allowed_tools` + warn-on-permissive).
7. Edit `src/assistant/handlers/message.py` per §2.7 (URL detector +
   envelope enrichment; DB write unchanged).
8. Edit `src/assistant/main.py` per §2.8 (`configure_installer`,
   `_bootstrap_skill_creator_bg`, `sweep_run_dirs` fire-and-forget).
9. Write `skills/skill-installer/SKILL.md` per §2.9.
10. `uv sync` (should be no-op — no new deps).
11. `just lint` (ruff + mypy strict) — green.
12. `just test` — new tests in §3.4 pass; existing 136 phase-2 tests
    remain green. We ADDED prefixes (`python skills/`, `python3 skills/`,
    `uv run skills/`) without removing existing ones; gh/git/uv-sync
    validators are net-new and don't regress the existing matrix.

### §3.2 Smoke sequence (owner, post-deploy)

1. "ping" → still works (phase-2 regression check).
2. Wait 5-30s (bootstrap async). `tail` the daemon log for
   `skill_creator_bootstrap_ok` (or `_skipped_no_gh_nor_git`).
3. "какие есть скилы в marketplace" → `mcp__installer__marketplace_list`
   → 17-entry list.
4. "расскажи что умеет pdf" → `mcp__installer__marketplace_info(name="pdf")`
   → pdf SKILL.md body.
5. "поставь weather с https://github.com/foo/weather" →
   a. URL detector adds system-note.
   b. Model calls `mcp__installer__skill_preview`.
   c. Bot asks "Установить? (да/нет)".
   d. Owner: "да".
   e. Next turn: `mcp__installer__skill_install(url=..., confirmed=true)`.
   f. "installed weather" (+ "Зависимости в фоне" if has_tools_dir).
   g. "готово?" → `skill_sync_status(name="weather")` → status JSON.
6. "удали weather" → `skill_uninstall(name="weather", confirmed=true)`
   → files removed.
7. "сделай скилл echo" → model uses `skill-creator` guidance + `Write`
   tool; PostToolUse hook touches sentinel; next turn manifest lists
   `echo`.

### §3.3 Deploy checklist

Before shipping phase 3:

- `just lint && just test` — green.
- Preflight: `gh --version`, `git --version`, `uv --version` all
  present on owner's machine. Documented in README alongside phase-2
  instructions.
- `claude login` session exists. Phase 3 does not change this.
- If upgrading from phase 2: no migration script needed. First boot
  fetches `skills/skill-creator/` asynchronously.

### §3.4 Tests (new)

All tests live under `tests/`. Existing phase-2 tests unchanged.

Installer `@tool` tests use `pytest` + monkeypatch of
`_installer_core.fetch_bundle_async` (or `_gh_api_async`) to avoid
hitting the network. `@tool` handlers are invoked via `.handler`:

```python
@pytest.fixture
def configured_installer(tmp_path):
    project_root = tmp_path / "project"; project_root.mkdir()
    (project_root / "skills").mkdir(); (project_root / "tools").mkdir()
    data_dir = tmp_path / "data"; data_dir.mkdir()
    from assistant.tools_sdk.installer import configure_installer
    configure_installer(project_root=project_root, data_dir=data_dir)
    return project_root, data_dir
```

- `test_installer_tool_skill_preview.py` — monkeypatch
  `core.fetch_bundle_async` to populate `dest` with a minimal valid
  bundle. Invoke `skill_preview.handler({"url": "https://..."})`.
  Assert return dict has `preview.name`, `preview.source_sha`,
  `confirm_hint`; `<data_dir>/run/installer-cache/<key>/manifest.json`
  exists.
- `test_installer_tool_skill_install.py` — run `skill_preview` then
  `skill_install(confirmed=true)` with monkeypatched fetch that
  returns the same bundle both times. Assert
  `skills/<name>/.0xone-installed` exists, `data/run/skills.dirty`
  touched.
- `test_installer_tool_unconfirmed_install.py` —
  `skill_install(confirmed=false)` returns `{"code": 3}`, nothing
  installed under `skills/<name>/` or `tools/<name>/`.
  S13 fix (wave-2): additionally assert the cache entry is PRESERVED
  so the user can retry with `confirmed=true` without re-previewing:
  - `<data_dir>/run/installer-cache/<key>/manifest.json` still exists.
  - `<data_dir>/run/installer-cache/<key>/bundle/` still present.
  - Follow-up call `skill_install(url=..., confirmed=true)` proceeds
    to install without a second fetch (monkeypatched `fetch_bundle_async`
    call-count stays at 1). This pins the contract that
    unconfirmed-install must NOT invalidate the preview cache.
- `test_installer_tool_missing_fetch_tool.py` — monkeypatch
  `shutil.which` to return None for both gh and git; assert
  `marketplace_list` returns `{"code": 9}`.
- `test_installer_tool_toctou.py` — two fetches, second returns a
  mutated file → `skill_install` returns `{"code": 7}`; cache entry
  rmtree'd; no partial install.
- `test_installer_tool_path_traversal.py` — fetch writes `../foo` —
  validator raises → `{"code": 5}`.
- `test_installer_tool_symlink_rejected.py` — fetch creates a symlink
  (absolute AND relative) inside bundle → both rejected → no
  `copytree`.
- `test_installer_tool_size_limits.py` — bundle with 101 files OR one
  3 MB file → ValidationError → `{"code": 5}`.
- `test_installer_tool_ssrf_deny.py` —
  `skill_preview(url="http://169.254.169.254/")` → URLError →
  `{"code": 4}`. Include `[fe80::1]` IPv6 case.
- `test_installer_tool_uninstall.py` — install, then `skill_uninstall`
  with `confirmed=true` removes files and touches sentinel. Second
  call returns `{"removed": false, "reason": "not installed"}`.
- `test_installer_mcp_registration.py` — assert `INSTALLER_SERVER`
  exposes 7 tools with exact names. Do NOT assert equality on init
  tool list; use subset-assert with the canonical seven
  `mcp__installer__*` names. (Reason: RQ1 observed ambient CLI tools.)
  Live-CLI probe variant marked `requires_claude_cli`, CI-skipped.
- `test_installer_marketplace_list.py` — monkeypatch `_gh_api_async`
  to return a synthetic 3-entry list → `marketplace_list` returns
  `{"entries": [...]}`.
- `test_installer_marketplace_info.py` — monkeypatch to return
  base64-encoded SKILL.md → `marketplace_info(name="x")` returns body
  text.
- `test_installer_marketplace_install.py` — monkeypatch to chain
  preview+install end-to-end; final state = installed skill.
- `test_installer_marketplace_rate_limit.py` — monkeypatch
  `_gh_api_async` to raise MarketplaceError with rate-limit text →
  `marketplace_list` returns `{"code": 10}` + message mentions
  `gh auth login` as the remedy (devil NH-2).
- `test_installer_skill_sync_status.py` — write a status.json
  manually, invoke `skill_sync_status(name="x")` → returns parsed.

Bootstrap test:

- `test_bootstrap_direct_python.py` — monkeypatch
  `_core.fetch_bundle_async` and `_core.atomic_install` so the
  bootstrap coroutine completes in <100 ms with a fake skill. Assert:
  `Daemon.start()` returns within 500 ms regardless of bootstrap
  duration (wrap the test in `asyncio.wait_for` with 500 ms); marker
  `.0xone-installed` touched; repeated boot is no-op;
  `_fetch_tool` raising → `skill_creator_bootstrap_skipped_no_gh_nor_git`
  in caplog.

URL detector:

- `test_url_detector.py` — 8 cases: `https://`, `http://`,
  `git@github.com:owner/repo.git`, inline mid-sentence, multiple URLs
  (only first 3 cited), no URL (no system-note), Markdown link
  syntax, URL with query string.

PostToolUse hook:

- `test_posttool_sentinel.py` — synthesize
  `input_data={"tool_name": "Write", "tool_input":
  {"file_path": "<pr>/skills/x/SKILL.md"}}` and invoke `on_write_edit`
  → sentinel exists. For `file_path="<pr>/tools/x/cache.json"`
  → sentinel exists. For `<pr>/foo.py` → sentinel NOT touched.
  For `..`/`../..` → sentinel NOT touched.

Bash allowlist:

- `test_bash_allowlist_gh_api.py` — whitelist coverage (§2.5, post-B11).
  Deny cases:
    - `gh api --hostname evil.com /repos/foo/bar`
      (B11 regression — deny-list previously missed `--hostname`).
    - `gh api --paginate /repos/foo/bar`.
    - `gh api -X DELETE /repos/foo/bar`.
    - `gh api /repos/foo/bar -H Authorization:Bearer X`
      (non-Accept header).
    - `gh api /repos/foo/bar -F field=value` (deny-list holdover).
    - `gh api --input payload.json /repos/foo/bar`.
    - `gh api /repos/../../etc/passwd` (endpoint whitelist).
    - `gh issue list` (only `api` + `auth status` subcommands allowed).
    - `gh auth login` (only `gh auth status` allowed).
  Allow cases:
    - `gh api /repos/anthropics/skills/contents/skills`.
    - `gh api -H Accept:application/vnd.github.v3+json
      /repos/anthropics/skills/contents/skills`.
    - `gh api /repos/anthropics/skills/tarball/main`.
    - `gh auth status`.
- `test_bash_allowlist_git_clone.py` —
  `git clone --depth=1 https://github.com/x/y skills/y` → allow;
  `git clone --depth=1 https://x /tmp/y` → deny (escape);
  `git clone --depth=1 git@evil.com:x/y foo` → deny (not github.com);
  `git clone --depth=0 ...` → deny; `git clone https://...` (no
  depth) → deny.
- `test_bash_allowlist_uv_sync.py` — `uv sync --directory=tools/foo`
  allow; `uv sync --directory=skills/foo` allow;
  `uv sync --directory=../../etc` deny; `uv sync` (no --directory)
  deny.

Sweeper + permissive default + hot-reload:

- `test_sweep_run_dirs.py` — create fake entries with `os.utime` set
  to different mtimes; assert >1h in `tmp/` and >7d in
  `installer-cache/` removed; younger entries survive.
- `test_skill_permissive_default.py` — `parse_skill` returns
  `allowed_tools=None` + log `skill_permissive_default` when missing;
  `[]` + `skill_lockdown_not_enforced`; `[Bash]` passes through
  unchanged.
- `test_skills_sentinel_hot_reload.py` — touch sentinel, call
  `bridge._render_system_prompt()`, assert sentinel gone,
  `_MANIFEST_CACHE` empty, `skills_dir` mtime bumped.

### §3.5 Lint / mypy corrections

- `shutil.copytree(..., symlinks=True)` — mypy strict may flag
  `onerror` positional; add `# type: ignore` if needed. Phase-2
  already uses `copytree` elsewhere.
- `_BG_TASKS` in `_installer_core` — annotate
  `set[asyncio.Task[Any]]` explicitly.
- `installer.py` `@tool` decorators produce `SdkMcpTool[Any]`;
  `.handler({...})` call is supported by SDK 0.1.63 (verified via
  RQ1 probe).

---

## §4 — Known gotchas (folded)

**RS-1 — Model may auto-invoke `ToolSearch` before the target `mcp__`
tool.** RQ1 observed `['ToolSearch', 'mcp__installer__...']` in every
query. Tests MUST NOT assert that the first `ToolUseBlock` is the
installer tool; assert it appears in the list. Source: `spike-findings.md §RQ1` obs 1.

**RS-2 — `setting_sources=["project"]` + `mcp_servers` coexist.** SDK
silently tolerates an invalid-`mcpServers` block in
`.claude/settings.local.json`; it does NOT shadow the programmatic
`mcp_servers=` argument. Do NOT drop `setting_sources=["project"]`.
Source: RQ1 C6.

**RS-3 — `HookMatcher.matcher` is a regex on `tool_name`, not on
`file_path`.** For PostToolUse-on-Write that needs file_path filtering
we must do containment inside the hook body, not rely on a matcher
pattern. Source: `HookMatcher.__dataclass_fields__` inspection + RQ1 C5.

**RS-4 — Ambient CLI tools pollute `SystemMessage(init).data["tools"]`.**
On the RQ1 host the list contained ~60 entries (Task, TodoWrite, Figma/
Gmail/Drive/Calendar/Playwright MCPs) alongside our two. Tests MUST use
subset-assert, never equality.

**NH-1 — `.0xone-installed` marker is the idempotency gate.** A
partial `atomic_install` failure leaves `skills/<name>/` populated
without the marker; next boot re-attempts. Source: description.md BL-2.

**NH-2 — GitHub unauth rate limit (60 req/hour).**
`marketplace_list` + a few `marketplace_info` calls fit, but
"list + info on all" in one session could exhaust quota.
`_gh_api_async` surfaces the remedy (`gh auth login` → 5000/hour).

**NH-3 — `ResultMessage.model` does not exist on 0.1.63.** Capture
model from `AssistantMessage.model` inside the stream loop (phase-2
already does this).

**NH-4 — `ToolResultBlock` comes in `UserMessage`, not `AssistantMessage`.**
RQ1 `_drive()` helper demonstrates the idiom: iterate
`UserMessage.content` for `ToolResultBlock` to extract markers.

**NH-5 — `asyncio.create_task` orphan GC risk.** Anchor via
`self._bg_tasks: set[Task]` + `add_done_callback(set.discard)`.
Done in `Daemon._spawn_bg` and `_BG_TASKS` in `_installer_core`.

**NH-6 — `yaml.safe_load` of malformed frontmatter.** Wrap in
`try/except yaml.YAMLError` (done in `validate_bundle`).

**NH-7 (new from RQ1 obs 1) — `ToolSearch` pre-invoke overhead.** Not
a blocker; documented in `unverified-assumptions.md` for future
phase-4 re-test.

**NH-8 — `allowed-tools` frontmatter is a NO-OP in SDK.** Phase-3
per-skill gating is documentation-only; global baseline applies.
Warning for `[]` case.

**NH-9 — `gh api` rc=0 on HTTP 404.** `_parse_gh_json` + explicit
`MarketplaceError` on `{"message":..., "status":"404"}` shape. Covered
by `test_installer_marketplace_info.py` with synthetic 404.

**NH-10 — Bash allowlist `shlex.split` on unbalanced quotes raises
`ValueError`.** Wrapped and translated to deny-reason in
`_bash_allowlist_check`.

**NH-11 — First-turn cost dominated by init tool list.** RQ1 Q1 cost
$0.25 of $0.39 total. Phase-4 memory + phase-8 gh adding more
`mcp__` tools grows init. `exclude_dynamic_sections=True` already
saves on dynamic bits; ambient tools are from CLI preset, outside our
control.

**NH-12 — Sweeper runs ONCE at boot**, not continuously. A long-running
daemon accumulates cache entries between boots up to the 7-day cap.
Phase 5 (scheduler) could run hourly; not phase-3 concern.

---

## §5 — Citations

- `claude-agent-sdk==0.1.63` — verified via `importlib.metadata.version`.
  `tool`, `create_sdk_mcp_server`, `HookMatcher`, `ClaudeAgentOptions`
  import from top-level package.
- `HookMatcher` fields (`matcher: str | None`, `hooks: list[Callable]`,
  `timeout: float | None`) — `HookMatcher.__dataclass_fields__`
  inspection, 2026-04-21.
- `ClaudeAgentOptions.mcp_servers` accepts `dict[str, McpStdioServerConfig
  | McpSSEServerConfig | McpHttpServerConfig | McpSdkServerConfig] |
  str | Path` — SDK 0.1.63 dataclass type hints.
- `plan/phase3/spike-findings.md` — S1-S5 (2026-04-17) + RQ1 (2026-04-21).
- `plan/phase2/summary.md` — phase-2 hook architecture, manifest cache,
  `assert_no_custom_claude_settings`, preflight.
- `plan/phase2/known-debt.md` — D1 origin; Q-D1 pivot target.
- GitHub `anthropics/claude-code#39851` + `#41510` — imperative body
  compliance anti-patterns on Opus 4.7 (open). Phase-3 pivot bypasses
  entirely.
- GitHub `anthropics/skills` — marketplace structure observed live
  (spike S1.a-c).
- `memory/reference_claude_agent_sdk_gotchas.md` — SKILL.md
  `allowed-tools` SDK no-op; `ToolResultBlock` in `UserMessage`;
  `ResultMessage.model` absent; `_safe_query` wrapper.
- `memory/reference_claude_agent_sdk.md` — hooks API empirical facts on
  0.1.59 (PostToolUse accepts `{}` no-op; `tool_input.file_path`
  absolute).

---

## §6 — Manual smoke test checklist (owner deploy)

1. **Daemon boot.** Log shows:
   - `auth_preflight_ok`
   - `skill_creator_bootstrap_starting via=gh|git`
   - after 5-30s: one of
     `skill_creator_bootstrap_{ok,failed,timeout,skipped_no_gh_nor_git}`.
   - `ls skills/skill-creator/.0xone-installed` exists iff `_ok`.
2. **Telegram: "ping"** — phase-2 regression passes.
3. **Telegram: "какие есть скилы в marketplace"** — list includes at
   least `skill-creator`, `pdf`, `docx`, `mcp-builder`, `claude-api`.
4. **Telegram: "покажи что умеет pdf"** — returns pdf SKILL.md body.
5. **Telegram: "поставь скилл
   https://github.com/anthropics/skills/tree/main/skills/pdf"** —
   - Bot invokes `skill_preview`.
   - Bot asks "Установить? (да/нет)".
   - Owner: "да".
   - Next turn: bot invokes `skill_install(url=..., confirmed=true)`.
   - Bot replies "installed pdf".
   - `ls skills/pdf/.0xone-installed` exists.
6. **Telegram: "сделай скилл echo"** — model writes
   `skills/echo/SKILL.md` + `tools/echo/main.py` via Write; PostToolUse
   touches `data/run/skills.dirty`; next turn manifest lists `echo`.
7. **Telegram: "используй echo"** — bot invokes Skill → Bash
   `python tools/echo/main.py ...` → reply.
8. **Telegram: "удали скилл echo"** — confirms, then
   `skill_uninstall(name="echo", confirmed=true)`. `skills/echo/` gone.
9. **Restart daemon with `gh` uninstalled** (hide via `PATH` or
   `brew uninstall`). Log shows
   `skill_creator_bootstrap_skipped_no_gh_nor_git`. Bot still responds.
   `marketplace_list` → `{"code": 9}`. Ad-hoc URL install still works
   iff `git` is present.
10. **Security spot-check.** URL `http://169.254.169.254/latest/meta-data`
    → `skill_preview` returns `{"code": 4}` (SSRF guard fires).
11. **`./.venv/bin/pytest tests/test_installer_* tests/test_bootstrap_*
    tests/test_posttool_* tests/test_bash_allowlist_* tests/test_sweep_*
    tests/test_url_detector* tests/test_skill_permissive_* tests/test_skills_sentinel_*`**
    — all green.

---

## §7 — Phase-4+ reference (not phase-3 code, planning)

Phase 4 memory tool follows the same dogfood pattern:

- `src/assistant/tools_sdk/memory.py` with `@tool("memory_search", ...)`
  and `@tool("memory_write", ...)`; register via
  `create_sdk_mcp_server(name="memory", ...)`.
- Export `MEMORY_SERVER`; `ClaudeBridge._build_options` merges via
  `mcp_servers={"installer": INSTALLER_SERVER, "memory": MEMORY_SERVER}`.
- No SKILL.md-based memory tool — owner decision Q-D1=c.

Phase 8 gh tool: `src/assistant/tools_sdk/gh.py` with read-only
`@tool("gh_list_repos", ...)` etc. Consider adding a PostToolUse audit
hook with `HookMatcher(matcher="mcp__gh__.*")` — RQ1 C5 confirms this
is workable.

If a future SDK release regresses RQ1 criteria, fall back per
`detailed-plan §1c`: (a) exact `HookMatcher` list instead of regex;
(b) drop `setting_sources=["project"]` if `mcp_servers` / settings
conflict.

---

*End of implementation v2.*
