"""PreToolUse + PostToolUse hook builders for ClaudeBridge.

Three PreToolUse guards register against
`ClaudeAgentOptions.hooks["PreToolUse"]`:

* `make_bash_hook(project_root, data_dir=None)` — strict argv-based
  allowlist with shell-metacharacter rejection. Slip-guard regex is kept
  ONLY as a last-ditch defence-in-depth barrier (see `_BASH_SLIP_GUARD_RE`).
  When `data_dir` is provided the phase-7 validators (`render_doc`,
  `genimage`) gain additional path guards bound to
  `<data_dir>/run/render-stage/`, `<data_dir>/media/outbox/`, etc.
* `make_file_hook(project_root, data_dir=None)` — sandboxes
  `Read/Write/Edit/Glob/Grep` to paths inside `project_root` via
  `Path.is_relative_to`. Refuses any pattern containing `..` even when
  relative. `data_dir` is reserved for phase-7 extensions (stage-dir
  allowance) and currently unused.
* `make_webfetch_hook()` — full SSRF defence: hostname is parsed via
  `urllib.parse`, classified through `ipaddress`, and DNS-resolved (with a
  3-second timeout) so that any A/AAAA pointing at a private/loopback/
  link-local/reserved range is denied. Delegates to `bridge/net.py`.

Phase 3 adds a PostToolUse matcher (`make_posttool_hooks`) that touches
`<data_dir>/run/skills.dirty` whenever Write/Edit lands inside `skills/` or
`tools/` — the sentinel drives hot-reload of the manifest cache.

Phase 7 (commit 11) extends the Bash allowlist with four CLI validators
(`tools/transcribe`, `tools/genimage`, `tools/extract_doc`, `tools/render_doc`).
The factory surface gains an optional `data_dir: Path | None = None` kwarg
so the 9 existing phase-3/5/6 test call sites that construct factories
without a data dir stay green. When `data_dir is None` and the argv
targets `tools/render_doc/main.py`, the hook denies with
"render-doc requires data_dir-bound hooks" — render_doc's path guards
depend on the `<data_dir>/run/render-stage/` and `<data_dir>/media/outbox/`
roots, which cannot be validated without the bound directory.

All return the canonical SDK hook reply shape — `{}` (allow / no-op) or
`_deny(reason)` (PreToolUse deny). Reasoning is logged via the structured
logger so operators can audit decisions.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from claude_agent_sdk.types import (
    AsyncHookJSONOutput,
    HookCallback,
    HookContext,
    HookInput,
    SyncHookJSONOutput,
)

from assistant.bridge.net import (
    classify_url as _classify_url_via_net,
)
from assistant.bridge.net import (
    is_private_address as _net_is_private_address,
)
from assistant.logger import get_logger

log = get_logger("bridge.hooks")

# Re-export the SDK's hook signature so call sites in `bridge/claude.py` can
# bind matchers without importing `claude_agent_sdk.types` directly.
HookFn = HookCallback

# `HookCallback` accepts the full union of every hook event's input. Our hooks
# only fire under PreToolUse, but mypy strict requires the inner closure to be
# at least as permissive as the SDK signature -- so we accept the union (the
# `HookInput` alias re-exported by the SDK) and downcast at runtime to the
# `dict[str, Any]` shape the CLI actually ships.

FILE_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

# -----------------------------------------------------------------------------
# Bash: strict argv-based allowlist.
# -----------------------------------------------------------------------------

# Any of these in the raw command -> hard reject before tokenizing. We never
# want to give the model the ability to chain, redirect, or substitute.
#
# Fix-pack HIGH #2: bare `$` (any dollar) is denied outright. Claude Code's
# Bash tool runs the command via `/bin/sh -c`, so `echo $HOME $SSH_AUTH_SOCK`
# expands envvars BEFORE the argv-allowlist ever sees them. `$(...)` and
# `${...}` were already caught, but `$FOO` slipped through. Treating any `$`
# as a deny is overkill for the single legitimate case (echo "$5" would not
# have been allowed anyway), but it closes the env-var leak cleanly.
_SHELL_METACHARS: tuple[str, ...] = (
    ";",
    "&",
    "|",
    "`",
    "$",  # catches $FOO / $(cmd) / ${VAR}
    ">",
    "<",
    "\n",
    "\r",
    "\x00",
)

# argv[0] -> validator is routed via the match statement in
# `_validate_bash_argv`. The dict is kept as the allowlist membership gate.
_BASH_PROGRAMS: dict[str, str] = {
    "python": "python",
    "uv": "uv",
    "git": "git",
    "ls": "ls",
    "pwd": "pwd",
    "cat": "cat",
    "echo": "echo",
    "gh": "gh",  # phase 3: read-only `gh api` / `gh auth status` only
}

# H-1 (phase 3): both `tools/` and `skills/` are valid roots for `python` /
# `uv run` scripts. Anthropic's `skill-creator` bundle ships `scripts/*.py`
# under `skills/skill-creator/scripts/` that the model is expected to invoke
# directly. Without this prefix, the bootstrap path fails with "python
# script must live under tools/". See plan/phase3/implementation.md §2.9.
_PYTHON_ALLOWED_PREFIXES: tuple[str, ...] = ("tools/", "skills/")

# Phase 5: `python tools/schedule/main.py <sub>` additionally goes through a
# structural validator (`_validate_schedule_argv`) so the model can't
# accidentally run write-requests against sibling tables or pass unsafe flags.
# The generic `_validate_python_invocation` still runs BEFORE this check —
# the schedule-specific rules stack on top of the path allowlist.
_SCHEDULE_SUBCMDS: frozenset[str] = frozenset({"add", "list", "rm", "enable", "disable", "history"})
# Upper bounds for free-form string values. Structural validation of `--tz`
# (IANA shape) is the CLI's job — we only enforce length here, per wave-2
# B-W2-4 (regex would lose legitimate names like `Etc/GMT+3`).
_SCHEDULE_PROMPT_MAX_BYTES = 2048
_SCHEDULE_TZ_MAX_CHARS = 64
_UV_RUN_ALLOWED_PREFIXES: tuple[str, ...] = ("tools/", "skills/")

# Phase 7: argv gates for media CLIs (transcribe / genimage / extract_doc /
# render_doc). Each mirrors the phase-5/6 shape: enum subcommands where
# relevant, flag whitelist, dup-flag deny, integer/range caps on free-form
# values. `render_doc` additionally enforces path roots bound to the
# daemon's `data_dir` — without it, render invocations are refused entirely
# (the path guards cannot be satisfied).
_TRANSCRIBE_LANG_VALUES: frozenset[str] = frozenset({"auto", "en", "ru"})
_TRANSCRIBE_FORMAT_VALUES: frozenset[str] = frozenset({"segments", "text"})
_TRANSCRIBE_TIMEOUT_MIN_S = 10
_TRANSCRIBE_TIMEOUT_MAX_S = 300

_GENIMAGE_PROMPT_MAX_BYTES = 1024
_GENIMAGE_SIZE_VALUES: frozenset[int] = frozenset({256, 512, 768, 1024})
_GENIMAGE_STEPS_MIN = 1
_GENIMAGE_STEPS_MAX = 20
_GENIMAGE_SEED_MIN = 0
_GENIMAGE_SEED_MAX = 2**31 - 1
_GENIMAGE_TIMEOUT_MIN_S = 10
_GENIMAGE_TIMEOUT_MAX_S = 600
_GENIMAGE_DAILY_CAP_MIN = 0
_GENIMAGE_DAILY_CAP_MAX = 1_000

_EXTRACT_DOC_MAX_CHARS_CAP = 2_000_000
_EXTRACT_DOC_PAGES_RE = re.compile(r"^\d+(-\d+)?$")

_RENDER_DOC_TITLE_MAX_BYTES = 256
_RENDER_DOC_FONT_MAX_CHARS = 64
_RENDER_DOC_ALLOWED_OUT_SUFFIXES: tuple[str, ...] = (".pdf", ".docx")

# Phase 6: argv gate for `python tools/task/main.py <sub>`.
# Mirrors the phase-5 schedule validator — enum subcommands, dup-flag
# deny (wave-2 B-W2-5), per-sub flag whitelist, size/range caps.
_TASK_SUBCMDS: frozenset[str] = frozenset({"spawn", "list", "status", "cancel", "wait"})
_TASK_KINDS: frozenset[str] = frozenset({"general", "worker", "researcher"})
_TASK_TASK_MAX_BYTES = 4096
_TASK_TIMEOUT_MIN_S = 1
_TASK_TIMEOUT_MAX_S = 600
_TASK_LIMIT_MAX = 100
_TASK_STATUS_VALUES: frozenset[str] = frozenset(
    {
        "requested",
        "started",
        "completed",
        "failed",
        "stopped",
        "interrupted",
        "error",
        "dropped",
    }
)

# Strictly safe `git` subcommands. We refuse arbitrary args (no `-c`,
# `--upload-pack`, custom `--format=`, etc.) -- only known harmless flags.
_GIT_ALLOWED_SUBCMDS: frozenset[str] = frozenset({"status", "log", "diff", "clone"})

# Dangerous git flags. Several of these allow arbitrary command spawning via
# git's plumbing options (`-c core.sshCommand=...`, `--upload-pack=...`).
_GIT_FORBIDDEN_FLAGS: tuple[str, ...] = (
    "-c",
    "--config-env",
    "--upload-pack",
    "--receive-pack",
    "--ext-program",  # synthetic name; covered defensively below
)

# phase 3: `gh` CLI read-only. Only two subcommands, tightly framed.
_GH_ALLOWED_SUBCMDS: frozenset[str] = frozenset({"api", "auth"})
_GH_AUTH_ALLOWED_SUBSUB: frozenset[str] = frozenset({"status"})

# Phase 8 (C6): argv gate for `python tools/gh/main.py ...`. Covers the
# five first-level subcommands we ship (auth-status / issue / pr / repo /
# vault-commit-push). Every sub-subcommand outside the explicit allow-list
# below is denied; ditto for any write-flag (`--force`, `-X POST`, ...).
_GH_CLI_SUBCMDS: frozenset[str] = frozenset({
    "auth-status",
    "issue",
    "pr",
    "repo",
    "vault-commit-push",
})
# Issue sub-subs that mutate remote state — blocked until phase-9
# keyboard-confirm surface exists. SF-C6 extended the list with
# `develop`/`pin`/`unpin`/`status` to match gh 2.89's verb set.
_GH_CLI_ISSUE_FORBIDDEN: frozenset[str] = frozenset({
    "close",
    "comment",
    "edit",
    "delete",
    "reopen",
    "transfer",
    "unlock",
    "lock",
    "develop",
    "pin",
    "unpin",
    "status",
})
# PR sub-subs that mutate or leak diff content — same deferral.
_GH_CLI_PR_FORBIDDEN: frozenset[str] = frozenset({
    "create",
    "merge",
    "close",
    "comment",
    "edit",
    "delete",
    "review",
    "ready",
    "checkout",
    "checks",
    "develop",
    "update-branch",
    "lock",
    "unlock",
    "diff",
})
# SF-C6: `repo` sub-sub allow-list. `view` only; `clone`/`create`/`delete`/
# `edit`/`archive`/`rename`/`sync`/... all touch local filesystem or mutate
# remote state and are rejected even when they LOOK read-only.
_GH_CLI_REPO_ALLOWED_SUBSUB: frozenset[str] = frozenset({"view"})
# Flags that turn a read-only invocation into a write, leak state into
# an attacker-controlled file, or bypass the numeric --limit cap.
_GH_CLI_FORBIDDEN_FLAGS: frozenset[str] = frozenset({
    "--force",
    "--force-with-lease",
    "--no-verify",
    "--amend",
    "-X",
    "--method",
    "--body-file",  # SF-C6: file-based body reads bypass --body size caps
})
_GH_CLI_LIMIT_MIN = 1
_GH_CLI_LIMIT_MAX = 100

# Endpoints the model is allowed to hit via `gh api`. Must stay read-only —
# `/repos/<owner>/<repo>/contents[...]` and `/repos/<owner>/<repo>/tarball[...]`
# are the only shapes the skill_installer actually needs for marketplace flow.
# Query strings are tolerated (`?ref=main` etc.) but must not contain
# whitespace. Any other endpoint (`/graphql`, `/user`, `/search/...`) — deny.
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

# Flags that turn `gh api` into a write request, or that let the model
# redirect the call to an attacker host / override the method via a header.
# Blocked at argv level — we do NOT rely on remote-side authz (spike S2.e:
# the CLI still fires the request and only the server returns 403, which
# leaks intent).
#
# Phase-3 fix-pack additions (review #2):
#   -H / --header    : an attacker can bypass the -X block with
#                      `-H "X-HTTP-Method-Override: DELETE"`, and can also
#                      smuggle Authorization / custom auth headers.
#   --hostname       : points the CLI at an attacker-controlled GHES
#                      instance (`--hostname evil.example.com /repos/x/y`).
#   --cache          : persists responses on disk. Not a SSRF/method-bypass
#                      vector on its own, but it muddies TOCTOU detection
#                      (stale cache could hide an upstream swap between
#                      preview and install).
#   -p / --preview   : opts into GitHub preview APIs that are not in our
#                      endpoint whitelist (dependency-graph, packages, ...).
_GH_FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "-X",
        "--method",
        "--method-override",
        "-F",
        "--field",
        "-f",
        "--raw-field",
        "--input",
        "-H",
        "--header",
        "--hostname",
        "--cache",
        "-p",
        "--preview",
    }
)

# Files that must NEVER be readable via `cat`, even if technically inside
# project_root. Defence in depth: project_root won't contain these in prod
# (XDG separation), but a dev machine might still have a `./.env`.
_CAT_DENYLIST_BASENAMES: frozenset[str] = frozenset(
    {".env", ".envrc", "credentials", "credentials.json"}
)
_CAT_DENYLIST_SUBSTRINGS: tuple[str, ...] = (
    ".ssh/",
    ".aws/",
    ".gnupg/",
    ".docker/config",
    ".kube/",
    "id_rsa",
    "id_ed25519",
)
_CAT_DENYLIST_SUFFIXES: tuple[str, ...] = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
)

# Last-ditch slip-guard. Should be redundant with the argv-based allowlist,
# but kept as defence-in-depth for unexpected vectors.
_BASH_SLIP_GUARD_RE = re.compile(
    r"(\benv\b|\bprintenv\b|\bset\b\s*$|"
    r"\.env|\.ssh|\.aws|secrets|\.db\b|token|password|ANTHROPIC_API_KEY|"
    r"\$'\\[0-7]|"
    r"base64\s+-d|openssl\s+enc|xxd\s+-r|"
    r"[A-Za-z0-9+/]{48,}={0,2}"
    r")",
    re.IGNORECASE,
)


def _deny(reason: str) -> SyncHookJSONOutput:
    """Return the canonical SDK reply that denies the tool call."""
    return cast(
        SyncHookJSONOutput,
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
    )


def _allow() -> SyncHookJSONOutput:
    """Return the canonical SDK reply that allows the tool call."""
    return cast(SyncHookJSONOutput, {})


def _path_safely_inside(candidate: Path, root: Path) -> bool:
    """True iff `candidate.resolve()` is `root.resolve()` or below it."""
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _has_dotdot(parts: tuple[str, ...]) -> bool:
    return any(part == ".." for part in parts)


def _validate_python_invocation(
    argv: list[str],
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> str | None:
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
            f"python script must live under one of {list(_PYTHON_ALLOWED_PREFIXES)}; got {script!r}"
        )
    if not _path_safely_inside(project_root / script_path, project_root):
        return "python script escapes project_root"
    # Phase 5: additional structural validator for the schedule CLI. Any
    # other `python tools/<foo>/main.py` call passes through unchanged.
    if script == "tools/schedule/main.py":
        return _validate_schedule_argv(argv[2:])
    # Phase 6: argv gate for the task CLI.
    if script == "tools/task/main.py":
        return _validate_task_argv(argv[2:])
    # Phase 7: argv gates for the four media CLIs. `render_doc` needs
    # `data_dir` to resolve its two mandatory path roots; denied outright
    # without it so the refusal carries a precise operator-facing reason
    # rather than an opaque path-guard failure.
    if script == "tools/transcribe/main.py":
        return _validate_transcribe_argv(argv[2:])
    if script == "tools/genimage/main.py":
        return _validate_genimage_argv(argv[2:], data_dir=data_dir)
    if script == "tools/extract_doc/main.py":
        return _validate_extract_doc_argv(argv[2:], project_root=project_root)
    if script == "tools/render_doc/main.py":
        if data_dir is None:
            return "render-doc requires data_dir-bound hooks"
        return _validate_render_doc_argv(argv[2:], data_dir=data_dir)
    # Phase 8 (C6): argv gate for the gh CLI wrapper. SF-C6 locks `repo`
    # to `view`, blocks write flags, and caps `--limit` to 1..100.
    if script == "tools/gh/main.py":
        return _validate_gh_argv(argv[2:])
    return None


def _validate_schedule_argv(args: list[str]) -> str | None:
    """Phase 5 bash hook gate for `python tools/schedule/main.py ...`.

    Runs on the arguments AFTER the script path, i.e. `[<subcmd>, *rest]`.
    Wave-2 additions: reject duplicate flags (B-W2-5), enforce 1-positional
    on `rm/enable/disable`, only length-check `--tz` (stdlib is sole
    authority on IANA shape; see wave-2 B-W2-4).
    """
    if not args:
        return "schedule CLI requires a subcommand"
    sub = args[0]
    if sub not in _SCHEDULE_SUBCMDS:
        return f"schedule subcommand {sub!r} not allowed"

    remaining = args[1:]

    if sub == "add":
        allowed_flags = {"--cron", "--prompt", "--tz"}
        seen: set[str] = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed_flags:
                return f"schedule add: flag {tok!r} not allowed"
            if tok in seen:
                return f"schedule add: duplicate flag {tok!r}"
            seen.add(tok)
            if i + 1 >= len(remaining):
                return f"schedule add: flag {tok} requires a value"
            val = remaining[i + 1]
            if tok == "--prompt" and len(val.encode("utf-8")) > _SCHEDULE_PROMPT_MAX_BYTES:
                return f"schedule add: --prompt exceeds {_SCHEDULE_PROMPT_MAX_BYTES} bytes"
            if tok == "--tz" and len(val) > _SCHEDULE_TZ_MAX_CHARS:
                return f"schedule add: --tz exceeds {_SCHEDULE_TZ_MAX_CHARS} chars"
            i += 2
        return None

    if sub == "list":
        seen = set()
        for tok in remaining:
            if tok != "--enabled-only":
                return f"schedule list: unknown flag {tok!r}"
            if tok in seen:
                return f"schedule list: duplicate flag {tok!r}"
            seen.add(tok)
        return None

    if sub in ("rm", "enable", "disable"):
        if len(remaining) != 1:
            return f"schedule {sub}: exactly one positional ID required"
        try:
            int(remaining[0])
        except ValueError:
            return f"schedule {sub}: ID must be integer"
        return None

    if sub == "history":
        allowed = {"--schedule-id", "--limit"}
        seen = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"schedule history: flag {tok!r} not allowed"
            if tok in seen:
                return f"schedule history: duplicate flag {tok!r}"
            seen.add(tok)
            if i + 1 >= len(remaining):
                return f"schedule history: flag {tok} needs a value"
            try:
                int(remaining[i + 1])
            except ValueError:
                return f"schedule history: {tok} requires integer"
            i += 2
        return None

    return f"schedule subcommand {sub!r} missing validator"


def _validate_task_argv(args: list[str]) -> str | None:
    """Phase 6 bash-hook gate for `python tools/task/main.py ...`.

    Runs on the arguments AFTER the script path (i.e. `[<subcmd>, *rest]`).
    Mirrors `_validate_schedule_argv`:
      * enum subcommands;
      * dup-flag deny (wave-2 B-W2-5 lesson);
      * per-sub flag whitelist;
      * size / range caps on free-form values.

    Returns a deny-reason string, or None to allow.
    """
    if not args:
        return "task CLI requires a subcommand"
    sub = args[0]
    if sub not in _TASK_SUBCMDS:
        return f"task subcommand {sub!r} not allowed"
    remaining = args[1:]

    if sub == "spawn":
        allowed = {"--kind", "--task", "--callback-chat-id"}
        required = {"--kind", "--task"}
        seen: dict[str, str] = {}
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"task spawn: flag {tok!r} not allowed"
            if tok in seen:
                return f"task spawn: duplicate flag {tok!r}"
            if i + 1 >= len(remaining):
                return f"task spawn: flag {tok} requires a value"
            val = remaining[i + 1]
            seen[tok] = val
            if tok == "--kind" and val not in _TASK_KINDS:
                return f"task spawn: --kind must be one of {sorted(_TASK_KINDS)}"
            if tok == "--task" and len(val.encode("utf-8")) > _TASK_TASK_MAX_BYTES:
                return f"task spawn: --task exceeds {_TASK_TASK_MAX_BYTES} bytes"
            if tok == "--callback-chat-id":
                try:
                    int(val)
                except ValueError:
                    return "task spawn: --callback-chat-id must be integer"
            i += 2
        missing = required - seen.keys()
        if missing:
            return f"task spawn: missing required flag(s) {sorted(missing)}"
        return None

    if sub == "list":
        allowed = {"--status", "--kind", "--limit"}
        seen_flags: set[str] = set()
        i = 0
        while i < len(remaining):
            tok = remaining[i]
            if tok not in allowed:
                return f"task list: flag {tok!r} not allowed"
            if tok in seen_flags:
                return f"task list: duplicate flag {tok!r}"
            seen_flags.add(tok)
            if i + 1 >= len(remaining):
                return f"task list: flag {tok} requires a value"
            val = remaining[i + 1]
            if tok == "--status" and val not in _TASK_STATUS_VALUES:
                return f"task list: --status must be one of {sorted(_TASK_STATUS_VALUES)}"
            if tok == "--kind" and val not in _TASK_KINDS:
                return f"task list: --kind must be one of {sorted(_TASK_KINDS)}"
            if tok == "--limit":
                try:
                    n = int(val)
                except ValueError:
                    return "task list: --limit must be integer"
                if n < 1 or n > _TASK_LIMIT_MAX:
                    return f"task list: --limit must be 1..{_TASK_LIMIT_MAX}"
            i += 2
        return None

    if sub in ("status", "cancel"):
        if len(remaining) != 1:
            return f"task {sub}: exactly one positional job_id required"
        try:
            int(remaining[0])
        except ValueError:
            return f"task {sub}: job_id must be integer"
        return None

    if sub == "wait":
        if not remaining:
            return "task wait: positional job_id required"
        try:
            int(remaining[0])
        except ValueError:
            return "task wait: job_id must be integer"
        rest = remaining[1:]
        seen_flags = set()
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok != "--timeout-s":
                return f"task wait: flag {tok!r} not allowed"
            if tok in seen_flags:
                return f"task wait: duplicate flag {tok!r}"
            seen_flags.add(tok)
            if i + 1 >= len(rest):
                return f"task wait: flag {tok} requires a value"
            try:
                t = int(rest[i + 1])
            except ValueError:
                return "task wait: --timeout-s must be integer"
            if t < _TASK_TIMEOUT_MIN_S or t > _TASK_TIMEOUT_MAX_S:
                return (
                    f"task wait: --timeout-s must be {_TASK_TIMEOUT_MIN_S}..{_TASK_TIMEOUT_MAX_S}"
                )
            i += 2
        return None

    return f"task subcommand {sub!r} missing validator"


# -----------------------------------------------------------------------------
# Phase 7 media CLIs — transcribe / genimage / extract_doc / render_doc.
# -----------------------------------------------------------------------------


def _is_loopback_only_endpoint(url: str) -> bool:
    """Quick argv-side sanity check: endpoint must be an http/https URL with a
    loopback literal as hostname. DNS is NOT resolved here — the CLI itself
    runs the authoritative `is_loopback_only(url)` check (S-1 spike), so this
    hook-side rule only needs to block the obvious bypass shapes
    (schemes other than http(s), missing host, public IP literal, hostname
    that is neither `localhost` nor a loopback IP literal).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Non-literal hostname that is not `localhost`. The CLI will run
        # the full DNS-resolving `is_loopback_only` check at runtime; we
        # refuse here to keep argv-side behaviour predictable.
        return False
    return bool(ip.is_loopback)


def _validate_path_argument(
    arg: str,
    *,
    must_be_absolute: bool,
    label: str,
) -> tuple[Path | None, str | None]:
    """Shared path-argv primitive — reject `..`, enforce absolute-ness.

    Returns (resolved_path | None, deny_reason | None). Resolution is
    lexical (`Path(...)`) — we do NOT touch the filesystem here because
    the validator runs before the CLI launches.
    """
    if not arg:
        return None, f"{label} requires a non-empty value"
    if arg.startswith("-"):
        return None, f"{label} value {arg!r} looks like a flag"
    path = Path(arg)
    if _has_dotdot(path.parts):
        return None, f"{label} must not contain '..'"
    if must_be_absolute and not path.is_absolute():
        return None, f"{label} must be an absolute path; got {arg!r}"
    return path, None


def _consume_flag_value(flag: str, remaining: list[str], i: int) -> tuple[str | None, str | None]:
    """`--flag value` -> (value, None) or (None, deny-reason)."""
    if i + 1 >= len(remaining):
        return None, f"flag {flag} requires a value"
    return remaining[i + 1], None


def _validate_transcribe_argv(args: list[str]) -> str | None:
    """Phase 7 bash-hook gate for `python tools/transcribe/main.py ...`.

    Shape (per tools/transcribe/main.py --help):
      ``<path> [--language {auto,en,ru}] [--timeout-s N] [--format {segments,text}]
       [--endpoint URL]``

    * The positional `<path>` must be an absolute file path without `..`.
    * `--endpoint` must be loopback (http(s) scheme, localhost or a
      loopback IP literal). DNS resolution is left to the CLI itself.
    * Flags must not repeat (wave-2 B-W2-5 lesson).
    """
    allowed_flags = {"--language", "--timeout-s", "--format", "--endpoint"}
    positional: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in allowed_flags:
            if tok in seen:
                return f"transcribe: duplicate flag {tok!r}"
            seen.add(tok)
            val, reason = _consume_flag_value(tok, args, i)
            if reason is not None:
                return f"transcribe: {reason}"
            assert val is not None  # for mypy — _consume_flag_value guarantees pair
            if tok == "--language" and val not in _TRANSCRIBE_LANG_VALUES:
                return f"transcribe: --language must be one of {sorted(_TRANSCRIBE_LANG_VALUES)}"
            if tok == "--format" and val not in _TRANSCRIBE_FORMAT_VALUES:
                return f"transcribe: --format must be one of {sorted(_TRANSCRIBE_FORMAT_VALUES)}"
            if tok == "--timeout-s":
                try:
                    n = int(val)
                except ValueError:
                    return "transcribe: --timeout-s must be integer"
                if n < _TRANSCRIBE_TIMEOUT_MIN_S or n > _TRANSCRIBE_TIMEOUT_MAX_S:
                    return (
                        f"transcribe: --timeout-s must be "
                        f"{_TRANSCRIBE_TIMEOUT_MIN_S}..{_TRANSCRIBE_TIMEOUT_MAX_S}"
                    )
            if tok == "--endpoint" and not _is_loopback_only_endpoint(val):
                return "transcribe: --endpoint must be http(s) loopback-only"
            i += 2
            continue
        if tok.startswith("-"):
            return f"transcribe: flag {tok!r} not allowed"
        positional.append(tok)
        i += 1
    if len(positional) != 1:
        return "transcribe: exactly one positional <path> required"
    _path, reason = _validate_path_argument(
        positional[0], must_be_absolute=True, label="transcribe: <path>"
    )
    if reason is not None:
        return reason
    return None


def _validate_genimage_argv(args: list[str], *, data_dir: Path | None) -> str | None:
    """Phase 7 bash-hook gate for `python tools/genimage/main.py ...`.

    Required flags: ``--prompt TEXT --out PATH``.
    Optional: ``--width/--height`` ∈ {256,512,768,1024}; ``--steps`` 1..20;
    ``--seed`` 0..2^31-1; ``--timeout-s`` 10..600; ``--endpoint`` loopback;
    ``--daily-cap`` 0..1000; ``--quota-file`` absolute.

    When `data_dir` is provided the `--out` path is further constrained to
    live under ``<data_dir>/media/outbox/`` and the optional
    ``--quota-file`` under ``<data_dir>/run/``. Without `data_dir` we only
    require `--out` to be an absolute path (phase-7 daemon always wires
    `data_dir` in; call sites that lack it stay permissive so phase-3..6
    tests keep passing).
    """
    allowed_flags = {
        "--prompt",
        "--out",
        "--width",
        "--height",
        "--steps",
        "--seed",
        "--timeout-s",
        "--endpoint",
        "--daily-cap",
        "--quota-file",
    }
    required = {"--prompt", "--out"}
    seen: dict[str, str] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok not in allowed_flags:
            return f"genimage: flag {tok!r} not allowed"
        if tok in seen:
            return f"genimage: duplicate flag {tok!r}"
        val, reason = _consume_flag_value(tok, args, i)
        if reason is not None:
            return f"genimage: {reason}"
        assert val is not None
        seen[tok] = val
        if tok == "--prompt":
            if "\n" in val or "\r" in val:
                return "genimage: --prompt must not contain newlines"
            if len(val.encode("utf-8")) > _GENIMAGE_PROMPT_MAX_BYTES:
                return f"genimage: --prompt exceeds {_GENIMAGE_PROMPT_MAX_BYTES} bytes"
        elif tok == "--out":
            out_path, path_reason = _validate_path_argument(
                val, must_be_absolute=True, label="genimage: --out"
            )
            if path_reason is not None:
                return path_reason
            assert out_path is not None
            if out_path.suffix.lower() != ".png":
                return "genimage: --out must end in .png"
            if data_dir is not None:
                outbox = data_dir / "media" / "outbox"
                if not _path_safely_inside(out_path, outbox):
                    return f"genimage: --out must live under {outbox}"
        elif tok in ("--width", "--height"):
            try:
                n = int(val)
            except ValueError:
                return f"genimage: {tok} must be integer"
            if n not in _GENIMAGE_SIZE_VALUES:
                return f"genimage: {tok} must be one of {sorted(_GENIMAGE_SIZE_VALUES)}"
        elif tok == "--steps":
            try:
                n = int(val)
            except ValueError:
                return "genimage: --steps must be integer"
            if n < _GENIMAGE_STEPS_MIN or n > _GENIMAGE_STEPS_MAX:
                return f"genimage: --steps must be {_GENIMAGE_STEPS_MIN}..{_GENIMAGE_STEPS_MAX}"
        elif tok == "--seed":
            try:
                n = int(val)
            except ValueError:
                return "genimage: --seed must be integer"
            if n < _GENIMAGE_SEED_MIN or n > _GENIMAGE_SEED_MAX:
                return f"genimage: --seed must be {_GENIMAGE_SEED_MIN}..{_GENIMAGE_SEED_MAX}"
        elif tok == "--timeout-s":
            try:
                n = int(val)
            except ValueError:
                return "genimage: --timeout-s must be integer"
            if n < _GENIMAGE_TIMEOUT_MIN_S or n > _GENIMAGE_TIMEOUT_MAX_S:
                return (
                    f"genimage: --timeout-s must be "
                    f"{_GENIMAGE_TIMEOUT_MIN_S}..{_GENIMAGE_TIMEOUT_MAX_S}"
                )
        elif tok == "--endpoint":
            if not _is_loopback_only_endpoint(val):
                return "genimage: --endpoint must be http(s) loopback-only"
        elif tok == "--daily-cap":
            try:
                n = int(val)
            except ValueError:
                return "genimage: --daily-cap must be integer"
            if n < _GENIMAGE_DAILY_CAP_MIN or n > _GENIMAGE_DAILY_CAP_MAX:
                return (
                    f"genimage: --daily-cap must be "
                    f"{_GENIMAGE_DAILY_CAP_MIN}..{_GENIMAGE_DAILY_CAP_MAX}"
                )
        elif tok == "--quota-file":
            quota_path, path_reason = _validate_path_argument(
                val, must_be_absolute=True, label="genimage: --quota-file"
            )
            if path_reason is not None:
                return path_reason
            assert quota_path is not None
            if data_dir is not None:
                run_dir = data_dir / "run"
                if not _path_safely_inside(quota_path, run_dir):
                    return f"genimage: --quota-file must live under {run_dir}"
        i += 2
    missing = required - seen.keys()
    if missing:
        return f"genimage: missing required flag(s) {sorted(missing)}"
    return None


def _validate_extract_doc_argv(args: list[str], *, project_root: Path) -> str | None:
    """Phase 7 bash-hook gate for `python tools/extract_doc/main.py ...`.

    Shape: ``<path> [--max-chars N] [--pages N[-M]]``.

    * Positional `<path>` must not contain `..`; absolute OR relative-to-
      project_root is acceptable (the CLI resolves + `is_file()` checks).
    * `--max-chars` ∈ 1..2_000_000 (CLI hard cap).
    * `--pages` must match ``^\\d+(-\\d+)?$`` (1-based inclusive range).
    """
    allowed_flags = {"--max-chars", "--pages"}
    positional: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in allowed_flags:
            if tok in seen:
                return f"extract_doc: duplicate flag {tok!r}"
            seen.add(tok)
            val, reason = _consume_flag_value(tok, args, i)
            if reason is not None:
                return f"extract_doc: {reason}"
            assert val is not None
            if tok == "--max-chars":
                try:
                    n = int(val)
                except ValueError:
                    return "extract_doc: --max-chars must be integer"
                if n < 1 or n > _EXTRACT_DOC_MAX_CHARS_CAP:
                    return f"extract_doc: --max-chars must be 1..{_EXTRACT_DOC_MAX_CHARS_CAP}"
            elif tok == "--pages":
                if not _EXTRACT_DOC_PAGES_RE.match(val):
                    return "extract_doc: --pages must match 'N' or 'N-M'"
                parts = val.split("-")
                if len(parts) == 2:
                    lo, hi = int(parts[0]), int(parts[1])
                    if lo < 1 or hi < lo:
                        return "extract_doc: --pages range must be 1-based and ascending"
                else:
                    if int(parts[0]) < 1:
                        return "extract_doc: --pages must be 1-based"
            i += 2
            continue
        if tok.startswith("-"):
            return f"extract_doc: flag {tok!r} not allowed"
        positional.append(tok)
        i += 1
    if len(positional) != 1:
        return "extract_doc: exactly one positional <path> required"
    path_str = positional[0]
    path, reason = _validate_path_argument(
        path_str, must_be_absolute=False, label="extract_doc: <path>"
    )
    if reason is not None:
        return reason
    assert path is not None
    # Relative paths must still stay inside project_root (defence-in-depth;
    # absolute paths are left to the CLI's own `is_file()` guard since
    # media inboxes typically live outside project_root).
    if not path.is_absolute() and not _path_safely_inside(project_root / path, project_root):
        return "extract_doc: relative <path> escapes project_root"
    return None


def _validate_render_doc_argv(args: list[str], *, data_dir: Path) -> str | None:
    """Phase 7 bash-hook gate for `python tools/render_doc/main.py ...`.

    Required: ``--body-file PATH --out PATH``. Optional: ``--title T``,
    ``--font F``.

    Path roots (bound to `data_dir`):
      * ``--body-file`` MUST live under ``<data_dir>/run/render-stage/``.
      * ``--out`` MUST live under ``<data_dir>/media/outbox/`` and end in
        ``.pdf`` or ``.docx``.

    Callers without a `data_dir` are rejected upstream in
    `_validate_python_invocation` ("render-doc requires data_dir-bound
    hooks") — the path guards below assume `data_dir` is a valid Path.
    """
    allowed_flags = {"--body-file", "--out", "--title", "--font"}
    required = {"--body-file", "--out"}
    seen: dict[str, str] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok not in allowed_flags:
            return f"render_doc: flag {tok!r} not allowed"
        if tok in seen:
            return f"render_doc: duplicate flag {tok!r}"
        val, reason = _consume_flag_value(tok, args, i)
        if reason is not None:
            return f"render_doc: {reason}"
        assert val is not None
        seen[tok] = val
        if tok == "--body-file":
            body_path, path_reason = _validate_path_argument(
                val, must_be_absolute=True, label="render_doc: --body-file"
            )
            if path_reason is not None:
                return path_reason
            assert body_path is not None
            stage = data_dir / "run" / "render-stage"
            if not _path_safely_inside(body_path, stage):
                return f"render_doc: --body-file must live under {stage}"
        elif tok == "--out":
            out_path, path_reason = _validate_path_argument(
                val, must_be_absolute=True, label="render_doc: --out"
            )
            if path_reason is not None:
                return path_reason
            assert out_path is not None
            if out_path.suffix.lower() not in _RENDER_DOC_ALLOWED_OUT_SUFFIXES:
                return (
                    f"render_doc: --out must end in one of {list(_RENDER_DOC_ALLOWED_OUT_SUFFIXES)}"
                )
            outbox = data_dir / "media" / "outbox"
            if not _path_safely_inside(out_path, outbox):
                return f"render_doc: --out must live under {outbox}"
        elif tok == "--title":
            if "\n" in val or "\r" in val:
                return "render_doc: --title must not contain newlines"
            if len(val.encode("utf-8")) > _RENDER_DOC_TITLE_MAX_BYTES:
                return f"render_doc: --title exceeds {_RENDER_DOC_TITLE_MAX_BYTES} bytes"
        elif tok == "--font":
            if len(val) > _RENDER_DOC_FONT_MAX_CHARS:
                return f"render_doc: --font exceeds {_RENDER_DOC_FONT_MAX_CHARS} chars"
        i += 2
    missing = required - seen.keys()
    if missing:
        return f"render_doc: missing required flag(s) {sorted(missing)}"
    return None


def _validate_uv_run(argv: list[str], project_root: Path) -> str | None:
    # `uv run <path>` — no flags, path must live under an allowed prefix.
    if len(argv) < 3:
        return "uv run requires a script argument"
    script = argv[2]
    if script.startswith("-"):
        return "uv run flags are not allowed (must be a path)"
    script_path = Path(script)
    if _has_dotdot(script_path.parts):
        return "script path must not contain '..'"
    if not any(script.startswith(p) for p in _UV_RUN_ALLOWED_PREFIXES):
        return (
            f"uv run target must live under one of {list(_UV_RUN_ALLOWED_PREFIXES)}; got {script!r}"
        )
    if not _path_safely_inside(project_root / script_path, project_root):
        return "uv run script escapes project_root"
    return None


def _validate_uv_sync(argv: list[str], project_root: Path) -> str | None:
    # `uv sync --directory tools/<name>` OR `uv sync --directory=tools/<name>`.
    # Rationale: Bash hook cannot observe `cd` (metachars rejected), so the
    # only way to scope `uv sync` is the explicit `--directory` flag.
    directory: str | None = None
    i = 2
    while i < len(argv):
        token = argv[i]
        if token == "--directory":
            if i + 1 >= len(argv):
                return "uv sync --directory requires a value"
            directory = argv[i + 1]
            i += 2
            continue
        if token.startswith("--directory="):
            directory = token.split("=", 1)[1]
            i += 1
            continue
        return f"uv sync flag {token!r} is not allowed"
    if directory is None:
        return "uv sync requires --directory=tools/<name>"
    dir_path = Path(directory)
    if _has_dotdot(dir_path.parts):
        return "uv sync --directory must not contain '..'"
    if not directory.startswith("tools/"):
        return "uv sync --directory must live under tools/"
    if not _path_safely_inside(project_root / dir_path, project_root / "tools"):
        return "uv sync --directory escapes tools/"
    return None


def _validate_uv_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "uv requires a subcommand"
    sub = argv[1]
    if sub == "run":
        return _validate_uv_run(argv, project_root)
    if sub == "sync":
        return _validate_uv_sync(argv, project_root)
    return f"uv subcommand '{sub}' is not allowed"


def _validate_git_clone(argv: list[str], project_root: Path) -> str | None:
    # Shape: `git clone --depth=1 <https-url> <dest>`.
    # Deliberately strict — no LFS, no recurse-submodules, no config override.
    if len(argv) != 5:
        return "git clone must be `git clone --depth=1 <url> <dest>`"
    if argv[2] != "--depth=1":
        return "only `git clone --depth=1 ...` is allowed"
    url = argv[3]
    if not (url.startswith("https://") or url.startswith("git@github.com:")):
        return "git clone: only https:// or git@github.com: URLs are allowed"
    if url.startswith("https://"):
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip()
        if not hostname:
            return "git clone: URL has no hostname"
        try:
            ip_literal = ipaddress.ip_address(hostname)
        except ValueError:
            ip_literal = None
        if ip_literal is not None and _net_is_private_address(ip_literal):
            return f"git clone: URL hostname {hostname!r} is a non-public IP literal"
    dest = argv[4]
    dest_path = Path(dest)
    if _has_dotdot(dest_path.parts):
        return "git clone dest must not contain '..'"
    candidate = dest_path if dest_path.is_absolute() else project_root / dest_path
    if not _path_safely_inside(candidate, project_root):
        return "git clone dest escapes project_root"
    return None


def _validate_git_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) < 2:
        return "git requires a subcommand"
    sub = argv[1]
    if sub not in _GIT_ALLOWED_SUBCMDS:
        return f"git subcommand '{sub}' is not in allowlist {sorted(_GIT_ALLOWED_SUBCMDS)}"
    # Scan args for option-injection regardless of subcommand. `clone` also
    # needs its positional layout validated below.
    for arg in argv[2:]:
        for forbidden in _GIT_FORBIDDEN_FLAGS:
            if arg == forbidden or arg.startswith(forbidden + "="):
                return f"git flag '{arg}' is not allowed (option-injection risk)"
    if sub == "clone":
        return _validate_git_clone(argv, project_root)
    return None


def _validate_gh_argv(args: list[str]) -> str | None:
    """Phase 8 (C6) argv gate for ``python tools/gh/main.py ...``.

    Validates the arguments AFTER the script path, i.e. the output of
    ``argv[2:]``. Returns a deny-reason string or ``None`` to allow.

    Defence-in-depth: the CLI itself re-validates subcommands and flags,
    but the Bash hook must reject malicious shapes BEFORE `gh` / `git` is
    ever launched. Same layered strategy as phase-5 schedule and phase-6
    task validators.

    Rules (SF-C6):
      * first token must be in `_GH_CLI_SUBCMDS`;
      * any `-X`/`--method`/`--force`/`--force-with-lease`/`--no-verify`/
        `--amend`/`--body-file` → deny;
      * duplicate long-flag → deny (closes the
        `--repo legit --repo evil` exfiltration shape);
      * `--limit N` must be an integer in 1..100;
      * `issue` sub-sub ∈ {close, comment, edit, delete, reopen,
        transfer, unlock, lock, develop, pin, unpin, status} → deny
        (reserved for phase 9 keyboard-confirm);
      * `pr` sub-sub ∈ same style → deny;
      * `repo` sub-sub MUST be exactly `view` (SF-C6);
      * `issue create` additionally forbids `--body-file` (already in
        the global forbidden-flag list, but the per-sub branch adds a
        precise error message for operator clarity).
    """
    if not args:
        return "gh CLI requires a subcommand"
    sub = args[0]
    if sub not in _GH_CLI_SUBCMDS:
        return f"gh CLI subcommand {sub!r} not allowed"

    # Duplicate long-flag guard (`--repo a/b --repo c/d`). `--flag=value`
    # and `--flag value` forms are normalised to their key component.
    seen: set[str] = set()
    for arg in args[1:]:
        if arg.startswith("--"):
            key = arg.split("=", 1)[0]
            if key in seen:
                return f"gh CLI duplicate flag {key}"
            seen.add(key)

    # Forbidden-flag matrix — both bare (`--force`) and keyed
    # (`--method=POST`) forms.
    for bad in _GH_CLI_FORBIDDEN_FLAGS:
        if bad in args or any(a.startswith(bad + "=") for a in args):
            return f"gh CLI flag {bad} not allowed"

    # --limit numeric cap (SF-C6 rate-limit defence).
    i = 0
    while i < len(args):
        arg = args[i]
        limit_val: str | None = None
        if arg == "--limit":
            if i + 1 >= len(args):
                return "gh CLI --limit requires a value"
            limit_val = args[i + 1]
            i += 2
        elif arg.startswith("--limit="):
            limit_val = arg.split("=", 1)[1]
            i += 1
        else:
            i += 1
            continue
        try:
            n = int(limit_val)
        except ValueError:
            return "gh CLI --limit requires integer"
        if n < _GH_CLI_LIMIT_MIN or n > _GH_CLI_LIMIT_MAX:
            return f"gh CLI --limit must be {_GH_CLI_LIMIT_MIN}..{_GH_CLI_LIMIT_MAX}"

    # Per-subcommand sub-sub matrix.
    if sub == "issue" and len(args) >= 2:
        subsub = args[1]
        if subsub in _GH_CLI_ISSUE_FORBIDDEN:
            return f"gh issue subsub {subsub!r} not allowed (phase 9)"
    if sub == "pr" and len(args) >= 2:
        subsub = args[1]
        if subsub in _GH_CLI_PR_FORBIDDEN:
            return f"gh pr subsub {subsub!r} not allowed (phase 9)"
    # SF-C6: `repo` MUST be `repo view` exactly; any other sub-sub denied.
    if sub == "repo":
        if len(args) < 2:
            return "gh repo requires a sub-subcommand"
        subsub = args[1]
        if subsub not in _GH_CLI_REPO_ALLOWED_SUBSUB:
            return f"gh repo subsub {subsub!r} not allowed (only 'view' is permitted)"

    return None


def _validate_gh_invocation(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return "gh requires a subcommand"
    sub = argv[1]
    if sub not in _GH_ALLOWED_SUBCMDS:
        return f"gh subcommand '{sub}' not allowed"
    if sub == "auth":
        if len(argv) == 3 and argv[2] in _GH_AUTH_ALLOWED_SUBSUB:
            return None
        return "only `gh auth status` is allowed"
    # sub == "api"
    # Reject write-flags BEFORE looking at the endpoint — a request must
    # never leave the host with `-X POST`.
    for flag in argv[2:]:
        if flag in _GH_FORBIDDEN_FLAGS:
            return f"gh api: flag {flag} not allowed (read-only)"
        if any(flag.startswith(f + "=") for f in _GH_FORBIDDEN_FLAGS):
            return f"gh api: flag {flag.split('=', 1)[0]} not allowed (read-only)"
    endpoint = next((a for a in argv[2:] if a.startswith("/")), None)
    if not endpoint:
        return "gh api requires an endpoint path starting with '/'"
    if not _GH_API_SAFE_ENDPOINT_RE.match(endpoint):
        return f"gh api: endpoint {endpoint!r} not in read-only whitelist"
    return None


def _validate_cat_invocation(argv: list[str], project_root: Path) -> str | None:
    if len(argv) != 2:
        return "cat requires exactly one path argument"
    arg = argv[1]
    if arg.startswith("-"):
        return "cat flags are not allowed"
    target = Path(arg)
    if _has_dotdot(target.parts):
        return "cat path must not contain '..'"
    candidate = target if target.is_absolute() else project_root / target
    if not _path_safely_inside(candidate, project_root):
        return "cat path escapes project_root"
    name = candidate.name.lower()
    if name in _CAT_DENYLIST_BASENAMES:
        return f"cat target '{name}' is in denylist (secrets defence-in-depth)"
    arg_lower = arg.lower()
    for needle in _CAT_DENYLIST_SUBSTRINGS:
        if needle in arg_lower:
            return f"cat path contains denylisted segment '{needle}'"
    for suffix in _CAT_DENYLIST_SUFFIXES:
        if name.endswith(suffix):
            return f"cat target '{name}' is a database file"
    return None


def _validate_ls_invocation(argv: list[str], project_root: Path) -> str | None:
    # `ls` may run with no args (current dir) or paths inside the project.
    # We refuse all flags to keep the surface minimal.
    for arg in argv[1:]:
        if arg.startswith("-"):
            return f"ls flag '{arg}' is not allowed"
        target = Path(arg)
        if _has_dotdot(target.parts):
            return "ls path must not contain '..'"
        candidate = target if target.is_absolute() else project_root / target
        if not _path_safely_inside(candidate, project_root):
            return "ls path escapes project_root"
    return None


def _validate_pwd_invocation(argv: list[str]) -> str | None:
    if len(argv) > 1:
        return "pwd takes no arguments"
    return None


def _validate_echo_invocation(argv: list[str]) -> str | None:
    # echo is harmless even with arbitrary args; newlines/null already rejected
    # at the metachar gate. Nothing left to check.
    del argv
    return None


def _validate_bash_argv(
    argv: list[str],
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> str | None:
    if not argv:
        return "empty command"
    program_path = argv[0]
    program = Path(program_path).name  # accept `/usr/bin/python` too
    if program not in _BASH_PROGRAMS:
        return f"program '{program}' is not in allowlist {sorted(_BASH_PROGRAMS)}"
    match program:
        case "python":
            return _validate_python_invocation(argv, project_root, data_dir=data_dir)
        case "uv":
            return _validate_uv_invocation(argv, project_root)
        case "git":
            return _validate_git_invocation(argv, project_root)
        case "gh":
            return _validate_gh_invocation(argv)
        case "cat":
            return _validate_cat_invocation(argv, project_root)
        case "ls":
            return _validate_ls_invocation(argv, project_root)
        case "pwd":
            return _validate_pwd_invocation(argv)
        case "echo":
            return _validate_echo_invocation(argv)
        case _:  # pragma: no cover -- guarded by the membership check above
            return f"program '{program}' has no validator"


def check_bash_command(
    cmd: str,
    project_root: Path,
    *,
    data_dir: Path | None = None,
) -> str | None:
    """Public for tests: validate a Bash command, return deny-reason or None.

    `data_dir` is the phase-7 addition — forwarded to the python-invocation
    validator so the `render_doc` / `genimage` path guards can bind to
    ``<data_dir>/run/render-stage/`` and ``<data_dir>/media/outbox/``.
    Default `None` preserves the phase-3/5/6 call shape; pre-phase-7 test
    call sites stay green.
    """
    raw = cmd.strip()
    if not raw:
        return "empty command"
    for metachar in _SHELL_METACHARS:
        if metachar in cmd:
            return f"shell metacharacter not allowed: {metachar!r}"
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        return f"unparseable command (shlex): {exc}"
    reason = _validate_bash_argv(argv, project_root, data_dir=data_dir)
    if reason is not None:
        return reason
    if _BASH_SLIP_GUARD_RE.search(cmd):
        return "slip-guard matched (secrets/encoded-payload pattern)"
    return None


def make_bash_hook(
    project_root: Path,
    data_dir: Path | None = None,
) -> HookFn:
    """Build the Bash PreToolUse hook bound to `project_root`.

    `data_dir` (phase-7 addition, keyword-defaulted to `None`) binds the
    `render_doc` / `genimage` path-guard validators to
    ``<data_dir>/run/render-stage/`` and ``<data_dir>/media/outbox/``.
    When `None`, any `tools/render_doc/main.py` invocation is denied
    ("render-doc requires data_dir-bound hooks"); `tools/genimage/main.py`
    stays permissive on its `--out` / `--quota-file` paths (CLI-side
    guards remain the authoritative layer).
    """

    async def bash_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        # SDK only fires this on PreToolUse; downcast to access tool_input.
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        cmd = str(tool_input.get("command", "") or "")
        reason = check_bash_command(cmd, project_root, data_dir=data_dir)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="Bash",
                decision="deny",
                reason=reason,
                cmd=cmd[:200],
            )
            return _deny(reason)
        log.debug(
            "pretool_decision",
            tool_name="Bash",
            decision="allow",
            cmd=cmd[:120],
        )
        return _allow()

    return bash_hook


# -----------------------------------------------------------------------------
# File tools: sandbox to project_root (Read/Write/Edit/Glob/Grep).
# -----------------------------------------------------------------------------


def check_file_path(raw_path: str, project_root_resolved: Path) -> str | None:
    """Return deny-reason iff the path escapes project_root or contains `..`."""
    if not raw_path:
        return None
    target = Path(raw_path)
    if _has_dotdot(target.parts):
        return "path component '..' is not allowed"
    candidate = target if target.is_absolute() else project_root_resolved / target
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"unresolvable path: {exc}"
    if not resolved.is_relative_to(project_root_resolved):
        return f"path escapes project_root ({project_root_resolved}): {resolved}"
    return None


def make_file_hook(
    project_root: Path,
    data_dir: Path | None = None,
) -> HookFn:
    """Build the file-tool PreToolUse hook bound to `project_root`.

    The same hook is registered against Read/Write/Edit/Glob/Grep -- see
    `FILE_TOOLS` for the complete list. SDK gives us `tool_name` so the hook
    can branch on it for tool-specific input keys (Glob has `pattern`, etc.).

    `data_dir` (phase-7 addition, keyword-defaulted to `None`) is reserved
    for future extensions that allow Write/Edit inside
    ``<data_dir>/run/render-stage/`` (the body-file source for
    `tools/render_doc/`). The current policy still sandboxes to
    `project_root` only — the extension slot exists so upstream callers
    can migrate without another signature break.
    """
    del data_dir  # reserved for future use; see docstring
    root_resolved = project_root.resolve()

    async def file_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_name = raw.get("tool_name", "")
        tool_input = raw.get("tool_input") or {}

        # Collect every path-bearing field the SDK might pass for any file
        # tool. Each is independently checked; first violation wins.
        candidates: list[tuple[str, str]] = []
        for key in ("file_path", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                candidates.append((key, value))
        # Glob/Grep specific input.
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern:
            candidates.append(("pattern", pattern))

        for field, value in candidates:
            reason = check_file_path(value, root_resolved)
            if reason is not None:
                log.warning(
                    "pretool_decision",
                    tool_name=tool_name,
                    decision="deny",
                    reason=reason,
                    field=field,
                    value=value[:200],
                )
                return _deny(reason)

        return _allow()

    return file_hook


# -----------------------------------------------------------------------------
# WebFetch: SSRF defence — delegates to `bridge/net.py` for the shared helpers.
# -----------------------------------------------------------------------------


# Keep the private aliases so phase-2 tests / downstream importers that rely on
# them continue to work. The canonical implementation lives in `bridge/net.py`
# under a public name; re-export here.
_is_private_address = _net_is_private_address


async def classify_url(url: str, *, dns_timeout: float = 3.0) -> str | None:
    """Public for tests: deny-reason iff URL targets a non-public destination.

    Thin delegate to `bridge/net.py::classify_url` — the logic lives once,
    inside the mirrored SSRF block, so the installer mirror stays honest.
    """
    return await _classify_url_via_net(url, dns_timeout=dns_timeout)


def make_webfetch_hook(*, dns_timeout: float = 3.0) -> HookFn:
    """Build the WebFetch PreToolUse hook with full SSRF classification."""

    async def webfetch_hook(
        input_data: HookInput,
        tool_use_id: str | None,
        ctx: HookContext,
    ) -> AsyncHookJSONOutput | SyncHookJSONOutput:
        del tool_use_id, ctx
        raw: dict[str, Any] = cast(dict[str, Any], input_data)
        tool_input = raw.get("tool_input") or {}
        url = str(tool_input.get("url", "") or "").strip()
        if not url:
            return _allow()
        reason = await classify_url(url, dns_timeout=dns_timeout)
        if reason is not None:
            log.warning(
                "pretool_decision",
                tool_name="WebFetch",
                decision="deny",
                reason=reason,
                url=url[:200],
            )
            return _deny(reason)
        log.debug(
            "pretool_decision",
            tool_name="WebFetch",
            decision="allow",
            url=url[:200],
        )
        return _allow()

    return webfetch_hook


# -----------------------------------------------------------------------------
# Aggregator -- assembles a HookMatcher list ready to plug into ClaudeAgentOptions.
# -----------------------------------------------------------------------------


def make_pretool_hooks(
    project_root: Path,
    data_dir: Path | None = None,
) -> list[Any]:
    """Return the canonical PreToolUse `HookMatcher` list for ClaudeBridge.

    Importing `HookMatcher` lazily keeps `bridge/hooks.py` a pure-validation
    module with no SDK-options coupling for unit tests; only this aggregator
    pulls in `claude_agent_sdk.HookMatcher`.

    `data_dir` is keyword-defaulted to `None` so the 9 existing phase-3/5/6
    test call sites that construct the factory without a data dir continue
    to work. When `None`, the Bash hook refuses `tools/render_doc/main.py`
    outright (see `make_bash_hook` docstring).
    """
    from claude_agent_sdk import HookMatcher

    return [
        HookMatcher(matcher="Bash", hooks=[make_bash_hook(project_root, data_dir)]),
        *[
            HookMatcher(matcher=t, hooks=[make_file_hook(project_root, data_dir)])
            for t in FILE_TOOLS
        ],
        HookMatcher(matcher="WebFetch", hooks=[make_webfetch_hook()]),
    ]


# -----------------------------------------------------------------------------
# PostToolUse: hot-reload sentinel for Write/Edit inside skills/ or tools/.
# -----------------------------------------------------------------------------


def _is_inside_skills_or_tools(raw_path: str, project_root: Path) -> bool:
    """True iff `raw_path` resolves beneath `<project_root>/skills/` or
    `<project_root>/tools/`. Any `..` or unresolvable path → False.

    Phase-3 §2.2 correction vs detailed-plan: the substring check
    `"/skills/" in path` would match `/tmp/evil/skills/x` even when
    project_root is elsewhere. `Path.is_relative_to` against a resolved
    root is the only honest test.
    """
    if not raw_path:
        return False
    try:
        target = Path(raw_path)
    except ValueError:
        return False
    if _has_dotdot(target.parts):
        return False
    try:
        abs_path = target.resolve() if target.is_absolute() else (project_root / target).resolve()
    except (OSError, ValueError):
        return False
    try:
        root = project_root.resolve()
    except (OSError, ValueError):
        return False
    for sub in ("skills", "tools"):
        base = root / sub
        try:
            if abs_path.is_relative_to(base):
                return True
        except ValueError:  # pragma: no cover -- different anchor on POSIX is impossible
            continue
    return False


def make_posttool_sentinel_hook(project_root: Path, data_dir: Path) -> HookFn:
    """Build the Write/Edit PostToolUse hook that touches the hot-reload sentinel.

    PostToolUse cannot deny — the tool has already run. We only observe
    side effects: if the target lived under `skills/` or `tools/`, touch
    `<data_dir>/run/skills.dirty` so the next `ClaudeBridge._render_system_prompt`
    call will invalidate the manifest cache.

    Any `OSError` is swallowed (logged) — a broken sentinel must not
    propagate into the model's tool-result stream.
    """
    sentinel = data_dir / "run" / "skills.dirty"

    async def posttool_hook(
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
        return cast(SyncHookJSONOutput, {})

    return posttool_hook


def make_posttool_hooks(project_root: Path, data_dir: Path) -> list[Any]:
    """Return the phase-3 PostToolUse `HookMatcher` list.

    Two matchers (Write + Edit), sharing the same underlying callback. Any
    tool landing inside `skills/` or `tools/` triggers a sentinel touch —
    the next bridge turn sees the new manifest without a daemon restart.
    """
    from claude_agent_sdk import HookMatcher

    hook = make_posttool_sentinel_hook(project_root, data_dir)
    return [
        HookMatcher(matcher="Write", hooks=[hook]),
        HookMatcher(matcher="Edit", hooks=[hook]),
    ]
