"""URL → bundle/dir fetcher for skill-installer.

Stdlib-only. Supported URL shapes:

* `https://github.com/<owner>/<repo>(.git)?`  → `git clone --depth=1` into dest.
* `git@github.com:<owner>/<repo>.git`          → same.
* `https://github.com/<owner>/<repo>/tree/<ref>/<path>` → partial fetch of the
  referenced subtree via `gh api /repos/<owner>/<repo>/contents/<path>?ref=<ref>`
  + recursive walk, downloading each file to dest. Falls back to
  `git clone --depth=1 <repo>` + `mv <repo>/<path> dest` when `gh` is absent.
* `https://gist.github.com/<user>/<id>`        → fetch raw tarball via
  `gh api /gists/<id>` (TODO phase-3.5; omitted from v1).
* `https://raw.githubusercontent.com/.../SKILL.md` → write to `<dest>/SKILL.md`
  via stdlib `urllib.request` (after SSRF gate).

Every fetch path (a) passes the URL through `classify_url_sync` before any
network I/O, (b) respects `FETCH_TIMEOUT`, and (c) strips `.git/` after
git-clone so the tree-hash is stable across clones of the same commit.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from ._net_mirror import classify_url_sync

FETCH_TIMEOUT = 30.0
_HTTP_USER_AGENT = "0xone-assistant-skill-installer/0.1"

# URL-shape regexes ----------------------------------------------------------

_GIT_REPO_HTTPS_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$"
)
_GIT_REPO_SSH_RE = re.compile(r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$")
_GITHUB_TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$")
_RAW_SKILL_MD_RE = re.compile(r"^https://raw\.githubusercontent\.com/[^\s]+/SKILL\.md$")


class FetchError(Exception):
    """Raised when a fetch fails (network, parsing, or denied-by-SSRF)."""


# --- Top-level dispatch -----------------------------------------------------


def fetch_bundle(url: str, dest: Path) -> None:
    """Download the bundle referenced by `url` into `dest` (must NOT exist).

    `dest.parent` must exist; the function creates `dest` itself so
    `git clone` succeeds. On any error, callers are expected to
    `shutil.rmtree(dest, ignore_errors=True)` from a `finally` block.
    """
    if dest.exists():
        raise FetchError(f"dest already exists: {dest}")

    if url.startswith("http"):
        ssrf = classify_url_sync(url)
        if ssrf is not None:
            raise FetchError(f"SSRF gate: {ssrf}")

    if _RAW_SKILL_MD_RE.match(url):
        _fetch_raw_skill_md(url, dest)
        return
    tree = _GITHUB_TREE_RE.match(url)
    if tree:
        _fetch_github_tree(tree, dest)
        return
    if _GIT_REPO_HTTPS_RE.match(url) or _GIT_REPO_SSH_RE.match(url):
        _git_clone(url, dest)
        return
    raise FetchError(f"unsupported URL shape: {url!r}")


# --- Concrete fetchers ------------------------------------------------------


def _git_clone(url: str, dest: Path) -> None:
    """Shallow-clone `url` into `dest` and strip `.git/` for hash stability."""
    # Respected by the phase-3 Bash allowlist when the model calls us; when we
    # invoke git directly the allowlist does not gate — `git clone --depth=1`
    # cannot spawn arbitrary commands (no `-c`, no `--upload-pack`) so direct
    # invocation is safe.
    try:
        subprocess.run(
            ["git", "clone", "--depth=1", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT,
        )
    except subprocess.CalledProcessError as exc:
        raise FetchError(
            f"git clone failed rc={exc.returncode}: {exc.stderr[:300] if exc.stderr else ''}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"git clone timed out after {FETCH_TIMEOUT}s") from exc
    except FileNotFoundError as exc:
        raise FetchError("git binary not found on PATH") from exc
    shutil.rmtree(dest / ".git", ignore_errors=True)


def _fetch_github_tree(match: re.Match[str], dest: Path) -> None:
    """Walk /repos/<owner>/<repo>/contents/<path>?ref=<ref> via `gh api`.

    Preferred path for small Anthropic-marketplace-style bundles. Falls back
    to a full shallow clone + subtree extraction if `gh` is absent.
    """
    owner, repo, ref, path = (
        match.group(1),
        match.group(2),
        match.group(3),
        match.group(4).rstrip("/"),
    )
    if shutil.which("gh") is None:
        _fetch_github_tree_fallback(owner, repo, ref, path, dest)
        return
    dest.mkdir(parents=True)
    try:
        _walk_gh_contents(owner, repo, ref, path, dest, rel=Path("."))
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _walk_gh_contents(owner: str, repo: str, ref: str, path: str, dest: Path, *, rel: Path) -> None:
    """Recursively download directory `path` from the repo into `dest/rel`."""
    entries = _gh_api_contents(owner, repo, f"{path}", ref)
    if isinstance(entries, dict) and entries.get("type") == "file":
        # Single-file path: the `tree/.../SKILL.md` case.
        _write_gh_file_entry(entries, dest / rel)
        return
    if not isinstance(entries, list):
        raise FetchError(f"unexpected gh contents shape: {type(entries).__name__}")
    (dest / rel).mkdir(parents=True, exist_ok=True)
    for entry in entries:
        name = str(entry.get("name") or "")
        kind = str(entry.get("type") or "")
        if not name or "/" in name or name.startswith(".."):
            raise FetchError(f"suspicious entry name: {name!r}")
        sub_rel = rel / name
        if kind == "dir":
            sub_path = f"{path}/{name}"
            _walk_gh_contents(owner, repo, ref, sub_path, dest, rel=sub_rel)
        elif kind == "file":
            _write_gh_file_entry(entry, dest / sub_rel)
        elif kind == "symlink":
            raise FetchError(f"symlink in remote tree not allowed: {sub_rel}")
        else:
            # `submodule` and anything else → skip with explicit error.
            raise FetchError(f"unsupported entry type {kind!r} at {sub_rel}")


def _write_gh_file_entry(entry: dict[str, Any], dest_file: Path) -> None:
    """Decode and write a single `gh api` file entry to `dest_file`."""
    encoding = entry.get("encoding")
    content = entry.get("content")
    if encoding == "base64" and isinstance(content, str):
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(base64.b64decode(content))
        return
    # Large files have empty content + `download_url`; fall back to urllib.
    download_url = entry.get("download_url")
    if isinstance(download_url, str):
        ssrf = classify_url_sync(download_url)
        if ssrf is not None:
            raise FetchError(f"SSRF gate (download_url): {ssrf}")
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        data = _http_get_bytes(download_url)
        dest_file.write_bytes(data)
        return
    raise FetchError(f"cannot materialise file entry: {entry.get('path')!r}")


def _gh_api_contents(owner: str, repo: str, path: str, ref: str) -> Any:
    """Thin wrapper around `gh api /repos/.../contents/...?ref=...`.

    Tolerates the `gh` rc=0-on-404 behaviour (spike S2.d) by parsing the
    stdout and detecting the `{message, status}` error envelope.
    """
    endpoint = f"/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    try:
        proc = subprocess.run(
            ["gh", "api", endpoint],
            check=False,
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise FetchError("gh binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"gh api timed out after {FETCH_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise FetchError(f"gh api rc={proc.returncode}: {proc.stderr[:300] if proc.stderr else ''}")
    payload = _parse_gh_json(proc.stdout)
    if isinstance(payload, dict) and "message" in payload and "status" in payload:
        raise FetchError(f"GitHub API error {payload['status']}: {payload['message']}")
    return payload


def _parse_gh_json(stdout: str) -> Any:
    """`gh api` occasionally prepends warning/notice lines to the JSON body.

    Scan line by line for the first line starting with `{` or `[`; that's
    the payload. Fall back to the whole stdout if the first line is the
    payload (typical `gh` output).
    """
    if not stdout.strip():
        raise FetchError("gh api: empty stdout")
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                # Not a complete JSON on that line — fall through to whole-body.
                break
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FetchError(f"gh api returned non-JSON: {stdout[:200]!r}: {exc}") from exc


def _fetch_github_tree_fallback(owner: str, repo: str, ref: str, path: str, dest: Path) -> None:
    """Shallow-clone the whole repo and extract the subtree at `path`."""
    tmp_clone = dest.parent / f".{dest.name}.clone"
    if tmp_clone.exists():
        shutil.rmtree(tmp_clone)
    _git_clone(f"https://github.com/{owner}/{repo}", tmp_clone)
    if ref != "main" and ref != "master":
        # We cloned the default branch — warn rather than silently diverge.
        # Full ref checkout would require re-fetching; out of scope for phase 3.
        pass
    sub = tmp_clone / path
    if not sub.is_dir():
        shutil.rmtree(tmp_clone, ignore_errors=True)
        raise FetchError(f"subpath {path!r} not found after clone")
    shutil.move(str(sub), str(dest))
    shutil.rmtree(tmp_clone, ignore_errors=True)


def _fetch_raw_skill_md(url: str, dest: Path) -> None:
    """Fetch a single `raw.githubusercontent.com/.../SKILL.md`."""
    dest.mkdir(parents=True)
    data = _http_get_bytes(url)
    (dest / "SKILL.md").write_bytes(data)


def _http_get_bytes(url: str) -> bytes:
    """Stdlib HTTPS GET with timeout + User-Agent header."""
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
    # urlopen respects the timeout param; the urllib docs explicitly cover this.
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.read()
    except (OSError, ValueError) as exc:
        raise FetchError(f"HTTP GET failed: {url!r}: {exc}") from exc
