"""URL → bundle/dir fetcher for skill_installer.

Stdlib-only. Supported URL shapes:

* `https://github.com/<owner>/<repo>(.git)?`  → `git clone --depth=1` into dest.
* `git@github.com:<owner>/<repo>.git`          → same.
* `https://github.com/<owner>/<repo>/tree/<ref>/<path>` → partial fetch of the
  referenced subtree. Primary path is **tarball** via
  `gh api /repos/<owner>/<repo>/tarball/<ref>` → stdlib `tarfile` safe
  extract (Python 3.12 `filter="data"`) → keep only the `<path>` subtree.
  A single `gh api` call replaces what used to be 1-per-directory + 1
  per-file, dodging the 60 req/hour anonymous cap.
* `https://raw.githubusercontent.com/.../SKILL.md` → write to `<dest>/SKILL.md`
  via stdlib `urllib.request` (after SSRF gate).

Every fetch path (a) passes the URL through `classify_url_sync` before any
network I/O, (b) respects `FETCH_TIMEOUT`, and (c) re-runs the SSRF gate on
every redirect target via `_SafeRedirectHandler` (defence against
`302 Location: http://169.254.169.254/...` metadata exfil).
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request

from ._net_mirror import classify_url_sync

FETCH_TIMEOUT = 30.0
_TARBALL_TIMEOUT = 60.0  # larger — bundles up to 10 MB + tar overhead
_HTTP_USER_AGENT = "0xone-assistant-skill_installer/0.1"

# URL-shape regexes ----------------------------------------------------------
#
# Must-fix #9: `[A-Za-z0-9_.-]+` permits `.` and `..` as full segments. The
# regex alone can't distinguish "foo.bar" (valid) from ".." (not), so every
# code-path that derives a git/repo URL from user input *also* runs
# `_reject_dotdot_segments(urlparse(url).path)` below.

_GIT_REPO_HTTPS_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?/?$"
)
_GIT_REPO_SSH_RE = re.compile(r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$")
_GITHUB_TREE_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$")
_RAW_SKILL_MD_RE = re.compile(r"^https://raw\.githubusercontent\.com/[^\s]+/SKILL\.md$")

_DEFAULT_REFS: frozenset[str] = frozenset({"main", "master"})


class FetchError(Exception):
    """Raised when a fetch fails (network, parsing, or denied-by-SSRF)."""


# --- urllib safe-redirect opener (must-fix #1) ------------------------------


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Re-classify every redirect target through the SSRF gate.

    urllib follows up to 10 redirects by default without re-validating the
    target. An attacker-controlled legitimate-looking URL could 302 to
    `http://169.254.169.254/...` and exfil cloud metadata. This handler
    rejects any redirect to a non-https URL or to a host that fails the
    SSRF classification.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        if not newurl.startswith("https://"):
            raise FetchError(f"redirect to non-https blocked: {newurl!r}")
        verdict = classify_url_sync(newurl)
        if verdict is not None:
            raise FetchError(f"SSRF gate (redirect): {verdict}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener: urllib.request.OpenerDirector = urllib.request.build_opener(_SafeRedirectHandler())


def _urlopen_safe(url: str, *, timeout: float) -> Any:
    """Wrap urlopen through the SSRF-redirect opener + User-Agent header."""
    req = Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
    return _opener.open(req, timeout=timeout)


def _reject_dotdot_segments(path: str) -> None:
    """Must-fix #9: forbid `.` / `..` segments in a parsed URL path."""
    for seg in path.split("/"):
        if seg in (".", ".."):
            raise FetchError(f"URL path segment not allowed: {seg!r}")


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
        _reject_dotdot_segments(urlparse(url).path)

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
    """Fetch a subtree of a GitHub repo via the tarball endpoint.

    Must-fix #5: replaces the previous recursive-contents walk that hit
    the 60 req/h anonymous cap (skill-creator = 83 files = 84+ calls).
    One `gh api /tarball/<ref>` call returns the whole repo snapshot;
    stdlib `tarfile` safely extracts only the requested subpath.

    Must-fix #3: when `gh` is absent, a non-default ref (`tree/v2.0/...`)
    MUST fail loudly — the old fallback silently cloned `main`, giving a
    supply-chain downgrade (user saw "v2.0" content, got main).
    """
    owner, repo, ref, path = (
        match.group(1),
        match.group(2),
        match.group(3),
        match.group(4).rstrip("/"),
    )
    if shutil.which("gh") is None:
        if ref not in _DEFAULT_REFS:
            raise FetchError(
                f"non-default ref {ref!r} requires `gh` on PATH; install https://cli.github.com/"
            )
        _fetch_github_tree_fallback(owner, repo, path, dest)
        return
    dest.mkdir(parents=True)
    try:
        _fetch_via_tarball(owner, repo, ref, path, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _fetch_via_tarball(owner: str, repo: str, ref: str, path: str, dest: Path) -> None:
    """Download `/repos/<owner>/<repo>/tarball/<ref>` and extract `<path>`.

    `gh api /repos/.../tarball/...` returns binary tar.gz on stdout. We
    keep the child tree at `<owner>-<repo>-<sha>/<path>/…` and strip the
    top two directory levels so `dest/` contains the skill root directly.
    Uses Python 3.12's `tarfile.extractall(..., filter="data")` — safely
    rejects absolute paths, `..`, symlinks, hardlinks, and device files.
    """
    endpoint = f"/repos/{owner}/{repo}/tarball/{ref}"
    # `gh api` writes binary to stdout for tarball endpoints — use bytes,
    # not text=, to preserve gzip integrity.
    try:
        proc = subprocess.run(
            ["gh", "api", endpoint],
            check=False,
            capture_output=True,
            timeout=_TARBALL_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise FetchError("gh binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"gh api tarball timed out after {_TARBALL_TIMEOUT}s") from exc
    if proc.returncode != 0:
        stderr_s = proc.stderr.decode("utf-8", "replace")[:300] if proc.stderr else ""
        raise FetchError(f"gh api tarball rc={proc.returncode}: {stderr_s}")
    if not proc.stdout:
        raise FetchError("gh api tarball: empty stdout")

    # Extract into a staging dir, then move the specific subtree into dest.
    staging = dest.parent / f".{dest.name}.tar-stage"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tf:
            # `filter="data"` is the secure preset shipped in Python 3.12:
            # blocks absolute paths, `..`, device files, symlinks, hardlinks.
            # Extracting the whole archive into a scratch dir keeps the
            # code clean — the archive is bounded (Anthropic skill-creator
            # is 5.5 MB per S1.c), and we only keep the subtree we want.
            tf.extractall(staging, filter="data")

        # Top-level dir is `<owner>-<repo>-<short-sha>/`. Find it.
        top_entries = [p for p in staging.iterdir() if p.is_dir()]
        if len(top_entries) != 1:
            raise FetchError(f"tarball root shape unexpected (entries={len(top_entries)})")
        top = top_entries[0]

        # Normalise the subpath and reject traversal defensively, even
        # though the regex caught the URL-form.
        path_parts = [seg for seg in path.split("/") if seg]
        for seg in path_parts:
            if seg in (".", ".."):
                raise FetchError(f"subpath segment not allowed: {seg!r}")

        source = top.joinpath(*path_parts) if path_parts else top
        try:
            source_resolved = source.resolve()
            top_resolved = top.resolve()
        except OSError as exc:
            raise FetchError(f"resolve failed for extracted subpath: {exc}") from exc
        if not source_resolved.is_relative_to(top_resolved):
            raise FetchError(f"subpath escapes tarball root: {path!r}")
        if not source.is_dir():
            raise FetchError(f"subpath {path!r} not found in tarball")

        # shutil.move atomic on same FS. dest was just mkdir'd empty.
        shutil.rmtree(dest)
        shutil.move(str(source), str(dest))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fetch_github_tree_fallback(owner: str, repo: str, path: str, dest: Path) -> None:
    """Shallow-clone default branch + extract subtree. Only used when `gh`
    is absent AND the ref is `main` / `master` (see must-fix #3)."""
    tmp_clone = dest.parent / f".{dest.name}.clone"
    if tmp_clone.exists():
        shutil.rmtree(tmp_clone)
    _git_clone(f"https://github.com/{owner}/{repo}", tmp_clone)
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
    """Stdlib HTTPS GET with timeout + User-Agent header.

    Redirects are followed through `_SafeRedirectHandler`, which re-runs
    the SSRF gate on every `Location:` (must-fix #1).
    """
    try:
        with _urlopen_safe(url, timeout=FETCH_TIMEOUT) as resp:
            data: bytes = resp.read()
            return data
    except FetchError:
        raise
    except (OSError, ValueError) as exc:
        raise FetchError(f"HTTP GET failed: {url!r}: {exc}") from exc
