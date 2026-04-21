"""Installer shared helpers (fetch, validate, atomic install, marketplace).

These are TRUSTED in-process helpers — not ``@tool``-decorated. They are
called directly from ``installer.py`` ``@tool`` bodies and from the
daemon's ``_bootstrap_skill_creator_bg`` path. The trust boundary between
this module and the model-invoked ``@tool`` layer is argument validation
done inside each ``@tool`` before dispatching to these helpers.
"""

from __future__ import annotations

import ast
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
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml

from assistant.bridge.hooks import _ip_is_blocked

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
MARKETPLACE_URL = "https://github.com/anthropics/skills"
MARKETPLACE_REPO = "anthropics/skills"
MARKETPLACE_BASE_PATH = "skills"
MARKETPLACE_REF = "main"

MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_FILES = 100
MAX_SINGLE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SEC = 30
UV_SYNC_TIMEOUT_SEC = 120
INSTALLER_CACHE_TTL_SEC = 7 * 86400
INSTALLER_TMP_TTL_SEC = 3600

_GIT_REPO_RE = re.compile(
    r"^(https://github\.com/[^/\s]+/[^/\s]+(?:\.git)?"
    r"|git@github\.com:[^/\s]+/[^/\s]+\.git)$"
)
_GITHUB_TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)$")
_RAW_SKILL_RE = re.compile(r"^https://raw\.githubusercontent\.com/[^/]+/[^/]+/[^/]+/.+/SKILL\.md$")
_GIST_RE = re.compile(r"^https://gist\.github\.com/[^/]+/[0-9a-f]+$")

_SCHEME_WHITELIST = frozenset({"https"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

# Wave-3 S9 fix: tighten the gh-api endpoint regex with a negative
# lookahead rejecting ``..`` anywhere in the path, AND keep the
# post-match check as a belt-and-suspenders guard (we also re-check in
# bridge/hooks.py where the same regex is duplicated).
_GH_API_SAFE_ENDPOINT_RE = re.compile(
    r"^/repos/(?!.*\.\.)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/"
    r"(contents(/[^?\s]*)?|tarball(/[^?\s]*)?)"
    r"(\?[^\s]*)?$"
)

# @tool error-code constants (surface in tool result dicts):
CODE_URL_BAD = 1
CODE_NOT_PREVIEWED = 2
CODE_NOT_CONFIRMED = 3
CODE_SSRF = 4
CODE_VALIDATION = 5
CODE_TOCTOU = 7
CODE_NO_FETCH_TOOL = 9
CODE_MARKETPLACE = 10
CODE_NAME_INVALID = 11

_HASH_SKIP_PARTS = frozenset({".git", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"})
_HASH_SKIP_SUFFIXES: tuple[str, ...] = (".pyc", ".DS_Store")


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------
class FetchToolMissing(RuntimeError):  # noqa: N818 — historical API name
    """Raised by _fetch_tool() when neither gh nor git is on PATH."""


class URLError(ValueError):
    """Canonical installer URL-rejection error."""


class ValidationError(ValueError):
    """Bundle failed static validation (schema, size, traversal, symlink)."""


class InstallError(RuntimeError):
    """Atomic install pipeline failure (tmp/rename race)."""


class MarketplaceError(RuntimeError):
    """gh api or git operation failed for marketplace queries."""


# ---------------------------------------------------------------------------
# Tool-error helper
# ---------------------------------------------------------------------------
def tool_error(message: str, code: int) -> dict[str, Any]:
    """Return an MCP tool error response.

    S1 fix (wave-2): the SDK's ``call_tool`` wrapper only forwards
    ``content[]`` and ``is_error`` to the model. Extra dict keys like
    ``error`` and ``code`` are visible ONLY to Python tests that invoke
    ``.handler(...)`` directly; the model never sees them.

    The authoritative model-facing surface is the formatted text
    containing ``(code=N)``.
    """
    return {
        "content": [{"type": "text", "text": f"error: {message} (code={code})"}],
        "is_error": True,
        "error": message,
        "code": code,
    }


# ---------------------------------------------------------------------------
# Fetch tool dispatch
# ---------------------------------------------------------------------------
def _fetch_tool() -> Literal["gh", "git"]:
    """Pick the first-available fetch tool.

    Per NH-S2: no caching — ``shutil.which`` is cheap and we must see
    newly-installed binaries on every call (owner may install gh mid-run).
    Raises :class:`FetchToolMissing` when neither ``gh`` nor ``git`` is
    present.
    """
    if shutil.which("gh"):
        return "gh"
    if shutil.which("git"):
        return "git"
    raise FetchToolMissing("neither gh nor git is on PATH")


# ---------------------------------------------------------------------------
# URL canonicalisation + cache-key derivation
# ---------------------------------------------------------------------------
def canonicalize_url(url: str) -> str:
    """Normalise a URL for cache-keying (drop query/fragment, lowercase host).

    Raises :class:`URLError` if the scheme is not in the whitelist.
    ``git@host:path`` SSH URLs are passed through as-is (no parsing).
    """
    raw = url.strip()
    if raw.startswith("git@"):
        return raw
    s = urlparse(raw)
    if s.scheme.lower() not in _SCHEME_WHITELIST:
        raise URLError(f"unsupported scheme: {s.scheme!r}")
    scheme = s.scheme.lower()
    netloc = (s.netloc or "").lower().removeprefix("www.")
    path = (s.path or "/").rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def cache_key(url: str) -> str:
    return hashlib.sha256(canonicalize_url(url).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# SSRF guard (Layer-2 on top of ``bridge.hooks._ip_is_blocked``)
# ---------------------------------------------------------------------------
def check_host_safety(hostname: str) -> None:
    """Layer-2 SSRF check (resolve + category compare).

    Raises :class:`URLError` on block.
    """
    if not hostname:
        raise URLError("empty host")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, 443, 0, socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            return
        for _, _, _, _, sockaddr in infos:
            reason = _ip_is_blocked(str(sockaddr[0]))
            if reason:
                raise URLError(f"SSRF: {hostname} -> {sockaddr[0]} ({reason})") from None
        return
    reason = _ip_is_blocked(str(ip))
    if reason:
        raise URLError(f"SSRF: literal {hostname} ({reason})")


# ---------------------------------------------------------------------------
# Fetch bundle dispatch
# ---------------------------------------------------------------------------
async def fetch_bundle_async(url: str, dest: Path) -> None:
    """Dispatch to the right fetch backend.

    Raises :class:`URLError` on bad URL or SSRF; :class:`FetchToolMissing`
    when we need ``gh`` or ``git`` and neither is present. ``dest`` must
    not exist on entry.
    """
    await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=False)
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
        "git",
        "clone",
        "--depth=1",
        url,
        str(dest),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=FETCH_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise URLError("git clone timeout") from None
    if proc.returncode != 0:
        raise URLError(f"git clone failed rc={proc.returncode}: {err[:300]!r}")


async def _github_tree_download_async(
    owner: str, repo: str, ref: str, path: str, dest: Path
) -> None:
    """Use gh api when available; else shallow-clone + subtree extract."""
    tool = _fetch_tool()
    if tool == "gh":
        await _gh_recursive_contents_into(owner, repo, ref, path, dest)
        return
    with tempfile.TemporaryDirectory() as td:
        clone_dest = Path(td) / "clone"
        await _git_clone_async(
            f"https://github.com/{owner}/{repo}.git",
            clone_dest,
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
    """Run ``gh api <endpoint>`` asynchronously and parse JSON stdout."""
    if not _GH_API_SAFE_ENDPOINT_RE.match(endpoint):
        raise MarketplaceError(f"endpoint {endpoint!r} not in installer whitelist")
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        endpoint,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(),
            timeout=FETCH_TIMEOUT_SEC,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise MarketplaceError("gh api timeout") from None
    if proc.returncode != 0:
        out_text = out.decode("utf-8", "replace")
        err_text = err.decode("utf-8", "replace")
        if "rate limit" in out_text.lower() or "rate limit" in err_text.lower():
            raise MarketplaceError(
                "GitHub API rate-limited. Run `gh auth login` to raise "
                "limit from 60 to 5000 req/hour."
            )
        try:
            for line in out_text.splitlines():
                s = line.strip()
                if s.startswith("{"):
                    payload_err = json.loads(s)
                    if isinstance(payload_err, dict) and payload_err.get("message"):
                        raise MarketplaceError(f"GitHub API: {payload_err['message']}")
        except json.JSONDecodeError:
            pass
        raise MarketplaceError(f"gh api rc={proc.returncode}: {err_text[:300]!r}")
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
    """Parse JSON from gh api stdout (handles single-line + multi-line)."""
    text = stdout.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    raise MarketplaceError(f"gh api returned unparseable output: {text[:200]!r}")


async def _gh_recursive_contents_into(
    owner: str, repo: str, ref: str, path: str, dest: Path
) -> None:
    """Walk the GitHub REST contents endpoint into dest/."""
    queue: list[tuple[str, Path]] = [(path, dest)]
    total_bytes = 0
    total_files = 0
    while queue:
        sub_path, sub_dest = queue.pop()
        entries = await _gh_api_async(f"/repos/{owner}/{repo}/contents/{sub_path}?ref={ref}")
        if not isinstance(entries, list):
            entries = [entries]
        sub_dest.mkdir(parents=True, exist_ok=True)
        for e in entries:
            typ = e.get("type")
            name = e.get("name", "")
            # S6 wave-3: mirror the gist-downloader name check — reject any
            # name containing ``..``, slashes, or leading dots to block
            # nested traversal (``foo/../bar``), absolute-style names, and
            # hidden-file clobbers.
            if not name or ".." in name or "/" in name or name.startswith("."):
                raise ValidationError(f"rejected entry name {name!r}")
            if typ == "dir":
                queue.append((f"{sub_path}/{name}", sub_dest / name))
            elif typ == "file":
                size = int(e.get("size") or 0)
                if size > MAX_SINGLE_BYTES:
                    raise ValidationError(f"file too large: {name} ({size} bytes)")
                total_bytes += size
                total_files += 1
                if total_files > MAX_FILES:
                    raise ValidationError(f"too many files (>{MAX_FILES})")
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValidationError(f"bundle too large (>{MAX_TOTAL_BYTES})")
                data = await _fetch_file_bytes(e, owner, repo, ref)
                (sub_dest / name).write_bytes(data)
            elif typ in ("submodule", "symlink"):
                raise ValidationError(f"rejected entry type: {typ}")
            else:
                raise ValidationError(f"unknown entry type: {typ!r}")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject HTTP redirects to non-https schemes or private/blocked hosts.

    S4 wave-3: the default :mod:`urllib.request` redirect handler follows
    302s without re-validating the target host. A compromised upstream
    could redirect ``https://raw.githubusercontent.com/...`` to
    ``http://169.254.169.254/...`` and the installer would happily fetch
    it. This handler runs ``check_host_safety`` on every redirect target
    and enforces the https-only scheme whitelist (S5).
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urlparse(newurl)
        if parsed.scheme.lower() != "https":
            raise urllib.error.URLError(f"refusing non-https redirect to {newurl!r}")
        if parsed.hostname:
            check_host_safety(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_opener() -> urllib.request.OpenerDirector:
    """Build a redirect-aware opener sharing our SSRF checks.

    Created per-call (cheap) so tests that monkey-patch ``check_host_safety``
    see the patched version rather than a captured reference.
    """
    return urllib.request.build_opener(_SafeRedirectHandler())


def _require_https(url: str) -> None:
    """Enforce https-only for direct urllib fetches (S5 wave-3).

    :class:`urllib.error.URLError` is raised because callers already catch
    urllib network failures — a simple ``ValueError`` would bypass the
    existing ``URLError`` handlers in ``skill_preview``/``skill_install``.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise urllib.error.URLError(f"refusing non-https URL {url!r}")


async def _fetch_file_bytes(entry: dict[str, Any], owner: str, repo: str, ref: str) -> bytes:
    """Return bytes for a single contents-API entry."""
    del owner, repo, ref
    if entry.get("encoding") == "base64" and entry.get("content"):
        return base64.b64decode(entry["content"])
    url = entry.get("download_url")
    if not url:
        raise MarketplaceError(f"no download_url for {entry.get('name')}")
    _require_https(url)
    parsed = urlparse(url)
    if parsed.hostname:
        check_host_safety(parsed.hostname)

    def _read() -> bytes:
        opener = _safe_opener()
        with opener.open(url, timeout=FETCH_TIMEOUT_SEC) as r:
            data: bytes = r.read(MAX_SINGLE_BYTES + 1)
            return data

    data = await asyncio.to_thread(_read)
    if len(data) > MAX_SINGLE_BYTES:
        raise ValidationError(f"file too large via download_url: {entry.get('name')}")
    return data


async def _gist_download_async(url: str, dest: Path) -> None:
    """Fetch gist file list via /gists/<id>; write each file into dest/."""
    gist_id = url.rstrip("/").rsplit("/", 1)[-1]
    api_url = f"https://api.github.com/gists/{gist_id}"
    _require_https(api_url)
    parsed = urlparse(api_url)
    if parsed.hostname:
        check_host_safety(parsed.hostname)

    def _read() -> bytes:
        opener = _safe_opener()
        with opener.open(api_url, timeout=FETCH_TIMEOUT_SEC) as r:
            data: bytes = r.read(MAX_TOTAL_BYTES + 1)
            return data

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
                {"download_url": raw_url},
                "",
                "",
                "",
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
    _require_https(url)
    parsed = urlparse(url)
    if parsed.hostname:
        check_host_safety(parsed.hostname)

    def _read() -> bytes:
        opener = _safe_opener()
        with opener.open(url, timeout=FETCH_TIMEOUT_SEC) as r:
            data: bytes = r.read(MAX_SINGLE_BYTES + 1)
            return data

    data = await asyncio.to_thread(_read)
    if len(data) > MAX_SINGLE_BYTES:
        raise ValidationError("raw SKILL.md too large")
    (dest / "SKILL.md").write_bytes(data)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_bundle(bundle: Path) -> dict[str, Any]:
    """Static checks on a downloaded bundle dir.

    Returns a report dict. Raises :class:`ValidationError` on fatal.
    """
    root = bundle.resolve()
    skill_md = bundle / "SKILL.md"
    if not skill_md.is_file():
        raise ValidationError("SKILL.md missing at bundle root")

    total_bytes = 0
    total_files = 0
    for p in bundle.rglob("*"):
        if p.is_symlink():
            raise ValidationError(
                f"symlink not allowed: {p.relative_to(bundle)} -> {os.readlink(p)}"
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
                raise ValidationError(f"file too large: {p.relative_to(bundle)}")
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
        raise ValidationError(f"frontmatter YAML parse failed: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValidationError("frontmatter is not a mapping")
    name = str(meta.get("name", "")).strip()
    description = str(meta.get("description", "")).strip()
    if not _NAME_RE.match(name):
        raise ValidationError(f"invalid skill name: {name!r}")
    if not description:
        raise ValidationError("description is required in SKILL.md frontmatter")
    allowed_tools = _normalize_allowed_tools_inline(meta.get("allowed-tools"))

    for py in bundle.rglob("*.py"):
        try:
            ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise ValidationError(f"py syntax error: {py.relative_to(bundle)}: {exc}") from exc

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
    """Duplicate of bridge/skills.py::_normalize_allowed_tools."""
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return None


# ---------------------------------------------------------------------------
# Deterministic tree hash (TOCTOU guard)
# ---------------------------------------------------------------------------
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
    return not any(p.name.endswith(sfx) for sfx in _HASH_SKIP_SUFFIXES)


# ---------------------------------------------------------------------------
# Atomic install
# ---------------------------------------------------------------------------
def atomic_install(
    tmp_bundle: Path,
    report: dict[str, Any],
    *,
    project_root: Path,
) -> None:
    """Install a validated bundle into ``skills/<name>/`` (+ optional
    ``tools/<name>/``).

    Invariant: ``.0xone-installed`` marker is touched ONLY after every
    rename has succeeded.
    """
    name = report["name"]
    if not _NAME_RE.match(name):
        raise InstallError(f"invalid name in report: {name!r}")
    skills_dst = project_root / "skills" / name
    tools_dst = project_root / "tools" / name
    if skills_dst.exists():
        raise InstallError(f"skill {name} already installed at {skills_dst}")
    if tools_dst.exists() and (tmp_bundle / "tools").is_dir():
        raise InstallError(f"tools/{name} already exists")

    (project_root / "skills").mkdir(parents=True, exist_ok=True)
    (project_root / "tools").mkdir(parents=True, exist_ok=True)

    stage_skill = project_root / "skills" / f".tmp-{name}-{uuid.uuid4().hex}"
    shutil.copytree(tmp_bundle, stage_skill, symlinks=True)

    inner_tools = stage_skill / "tools"
    stage_tools: Path | None = None
    if inner_tools.is_dir():
        stage_tools = project_root / "tools" / f".tmp-{name}-{uuid.uuid4().hex}"
        shutil.move(str(inner_tools), str(stage_tools))

    # B3 fix (wave-2): on rename failure, rollback ALL partial state.
    skills_dst_new_created = False
    try:
        stage_skill.rename(skills_dst)
        skills_dst_new_created = True
        if stage_tools is not None:
            stage_tools.rename(tools_dst)
    except OSError as exc:
        if skills_dst_new_created and skills_dst.exists():
            shutil.rmtree(skills_dst, ignore_errors=True)
        if stage_skill.exists():
            shutil.rmtree(stage_skill, ignore_errors=True)
        if stage_tools is not None and stage_tools.exists():
            shutil.rmtree(stage_tools, ignore_errors=True)
        raise InstallError(f"atomic rename failed: {exc}") from exc

    (skills_dst / ".0xone-installed").touch()


# ---------------------------------------------------------------------------
# Background uv sync launcher
# ---------------------------------------------------------------------------
_BG_TASKS: set[asyncio.Task[Any]] = set()


async def spawn_uv_sync_bg(name: str, *, project_root: Path, data_dir: Path) -> None:
    """Launch ``uv sync --directory tools/<name>`` as a background task."""
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
            "uv",
            "sync",
            f"--directory={target}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(),
                timeout=UV_SYNC_TIMEOUT_SEC,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            status_path.write_text(
                json.dumps({"status": "timeout", "finished_at": time.time()}),
                encoding="utf-8",
            )
            return
        if proc.returncode == 0:
            status_path.write_text(
                json.dumps({"status": "ok", "finished_at": time.time()}),
                encoding="utf-8",
            )
        else:
            status_path.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "stderr": err.decode("utf-8", "replace")[:2000],
                    }
                ),
                encoding="utf-8",
            )

    task = asyncio.create_task(_runner(), name=f"uv-sync-{name}")
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def cancel_bg_tasks() -> None:
    """Cancel all in-flight ``uv sync`` background tasks (S8 wave-3).

    Called from ``Daemon.stop`` before closing the SQLite connection so
    that a Ctrl-C during a long-running ``uv sync`` doesn't leak an
    orphan subprocess (whose ``_runner`` still holds a reference to the
    status file path). Idempotent: safe to call even when no tasks are
    running.
    """
    for t in list(_BG_TASKS):
        t.cancel()
    if _BG_TASKS:
        await asyncio.gather(*_BG_TASKS, return_exceptions=True)
    _BG_TASKS.clear()


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------
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


async def sweep_legacy_stage_dirs(project_root: Path) -> None:
    """Remove crashed-install staging dirs (``.tmp-*`` inside skills/tools)."""
    for sub in ("skills", "tools"):
        base = project_root / sub
        if not base.is_dir():
            continue
        for entry in base.iterdir():
            if not entry.name.startswith(".tmp-"):
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Marketplace helpers
# ---------------------------------------------------------------------------
async def marketplace_list_entries() -> list[dict[str, Any]]:
    entries = await _gh_api_async(f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}")
    if not isinstance(entries, list):
        return []
    return [
        {"name": e["name"], "path": e["path"]}
        for e in entries
        if e.get("type") == "dir" and not str(e.get("name", "")).startswith(".")
    ]


async def marketplace_fetch_skill_md(name: str) -> str:
    payload = await _gh_api_async(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md"
    )
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise MarketplaceError(f"unexpected marketplace info shape for {name!r}")
    return base64.b64decode(payload["content"]).decode("utf-8")


def marketplace_tree_url(name: str) -> str:
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid marketplace name {name!r}")
    return f"{MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}"
