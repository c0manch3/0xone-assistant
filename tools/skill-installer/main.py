"""skill-installer CLI — preview / install / status / marketplace.

Stdlib-only (B-4). Runs under the main project interpreter via
`python tools/skill-installer/main.py <cmd>`. No `pyproject.toml`, no
external deps; the installer imports only from the stdlib and from its
own `_lib/` subpackage.

URL-shape support is documented in `_lib/fetch.py`. Preview writes a
cache entry keyed by `sha256(canonical_url)[:16]`; `install --confirm
--url` re-fetches into `verify/` and SHA-compares against the preview
bundle. Any byte-level drift → `exit 7` with a diff in stderr
(see `_lib/install.diff_trees`).

Locking: one flock(LOCK_EX) per cache entry serialises concurrent
`preview` + `install` on the same URL (S-2). Cross-URL calls are
independent.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

# `tools/skill-installer/` is not a package — importing from `_lib` requires
# the directory to be on sys.path. A direct insert is simpler than plumbing
# a console-script entrypoint through an `uv` project (which B-4 forbids).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _lib.fetch import FetchError, fetch_bundle  # noqa: E402
from _lib.install import InstallError, atomic_install, diff_trees  # noqa: E402
from _lib.marketplace import (  # noqa: E402
    MARKETPLACE_REPO,
    MarketplaceError,
    fetch_skill_md,
    install_tree_url,
    list_skills,
)
from _lib.validate import ValidationError, sha256_of_tree, validate_bundle  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_NETWORK = 4
EXIT_INSTALL = 5
EXIT_TOCTOU = 7
EXIT_NO_CACHE = 8
EXIT_MARKETPLACE = 9


# ---------------------------------------------------------------- paths


def _project_root() -> Path:
    # tools/skill-installer/main.py → parents[2] = project root.
    return Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    """Mirror `assistant.config._default_data_dir` without importing it.

    The installer must NOT import from `src/assistant/` (B-4 stdlib-only
    principle — the installer is a separate entrypoint that may run before
    the main package is importable, e.g. inside a CI smoke harness).
    Override via `ASSISTANT_DATA_DIR` for tests.
    """
    override = os.environ.get("ASSISTANT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "0xone-assistant"


def _cache_root() -> Path:
    return _data_dir() / "run" / "installer-cache"


def _sentinel_path() -> Path:
    return _data_dir() / "run" / "skills.dirty"


def _canonicalize_url(url: str) -> str:
    """Drop fragment AND query; case-fold scheme/host; strip `www.` +
    drop default ports (review fix #12).

    B-2: query strings cause cache-entry duplication (`?utm_source=`) and
    can accidentally persist tokens (`?token=ABC`) to disk. GitHub's
    tree/contents URLs we support carry every meaningful bit in the path;
    query never matters. A future URL shape that genuinely needs a query
    parameter would add it to an explicit whitelist.

    Default-port normalisation: `https://github.com:443/x/y` is the same
    resource as `https://github.com/x/y`; without collapsing them we'd
    duplicate cache entries and force a second TOCTOU check on every
    user who happens to paste a URL with the port.
    """
    s = urlparse(url.strip())
    scheme = s.scheme.lower()
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = s.port
    default_ports = {"http": 80, "https": 443}
    is_default = port is None or port == default_ports.get(scheme)
    netloc = host if is_default else f"{host}:{port}"
    path = s.path.rstrip("/") or "/"
    return f"{scheme}://{netloc}{path}"


def _cache_dir_for(url: str) -> Path:
    h = hashlib.sha256(_canonicalize_url(url).encode("utf-8")).hexdigest()[:16]
    return _cache_root() / h


@contextmanager
def _cache_lock(cache_dir: Path) -> Iterator[None]:
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


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- commands


def cmd_preview(args: argparse.Namespace) -> int:
    from _lib.preview import render_preview

    cdir = _cache_dir_for(args.url)
    with _cache_lock(cdir):
        bundle_dir = cdir / "bundle"
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        try:
            fetch_bundle(args.url, bundle_dir)
        except FetchError as exc:
            sys.stderr.write(f"fetch failed: {exc}\n")
            return EXIT_NETWORK
        try:
            report = validate_bundle(bundle_dir)
        except ValidationError as exc:
            shutil.rmtree(bundle_dir, ignore_errors=True)
            sys.stderr.write(f"bundle invalid: {exc}\n")
            return EXIT_VALIDATION
        bsha = sha256_of_tree(bundle_dir)
        manifest = {
            "url": _canonicalize_url(args.url),
            "bundle_sha": bsha,
            "fetched_at": _utcnow_iso(),
            "file_count": report["file_count"],
            "total_size": report["total_size"],
            "name": report["name"],
        }
        (cdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        sys.stdout.write(render_preview(args.url, bundle_dir, bsha, report))
        sys.stdout.write("\n")
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    if not args.confirm:
        sys.stderr.write("install requires --confirm (preview+confirm is mandatory)\n")
        return EXIT_USAGE
    cdir = _cache_dir_for(args.url)
    mpath = cdir / "manifest.json"
    if not mpath.is_file():
        sys.stderr.write(
            f"no cached preview for {args.url!r}; run `skill-installer preview <URL>` first\n"
        )
        return EXIT_NO_CACHE
    manifest = json.loads(mpath.read_text(encoding="utf-8"))

    bundle_dir = cdir / "bundle"
    verify_dir = cdir / "verify"

    # Review fix #6: only rmtree the cache entry in outcomes where another
    # attempt would be wrong anyway — successful install (entry is spent)
    # or TOCTOU (bundle on source changed, cache is stale). Transient
    # network / install errors keep the cache so retry doesn't re-spend
    # the 60 req/h anonymous GitHub quota.
    cleanup_cache = False
    with _cache_lock(cdir):
        if verify_dir.exists():
            shutil.rmtree(verify_dir)
        try:
            try:
                fetch_bundle(args.url, verify_dir)
            except FetchError as exc:
                sys.stderr.write(f"re-fetch failed: {exc}\n")
                return EXIT_NETWORK
            new_sha = sha256_of_tree(verify_dir)
            if new_sha != manifest["bundle_sha"]:
                diff = diff_trees(bundle_dir, verify_dir)
                sys.stderr.write(
                    "bundle on source changed since preview; "
                    "re-run `preview <URL>` to see new content\n"
                )
                for line in diff[:50]:
                    sys.stderr.write(line + "\n")
                if len(diff) > 50:
                    sys.stderr.write(f"... and {len(diff) - 50} more lines\n")
                cleanup_cache = True  # stale cache — force a fresh preview
                return EXIT_TOCTOU
            try:
                report = validate_bundle(verify_dir)
            except ValidationError as exc:
                sys.stderr.write(f"bundle invalid on re-fetch: {exc}\n")
                return EXIT_VALIDATION
            try:
                atomic_install(verify_dir, report, project_root=_project_root())
            except InstallError as exc:
                sys.stderr.write(f"install failed: {exc}\n")
                return EXIT_INSTALL
            _sentinel_path().parent.mkdir(parents=True, exist_ok=True)
            _sentinel_path().touch()
            sys.stdout.write(
                json.dumps(
                    {"status": "ok", "name": report["name"]},
                    ensure_ascii=False,
                )
                + "\n"
            )
            cleanup_cache = True  # success — cache entry is spent
        finally:
            # `verify/` is always disposable.
            shutil.rmtree(verify_dir, ignore_errors=True)
            if cleanup_cache:
                shutil.rmtree(cdir, ignore_errors=True)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Report whether a skill is installed in the project.

    Review fix #8: the phase-3 stub always said `"unknown"`, which lies
    to callers (the SKILL.md tells the model to use this subcommand).
    The async-`uv sync` progress story is still out of scope (Q8), but
    the file-existence answer is cheap and truthful — extending to
    sync-in-progress polling is a drop-in later.
    """
    name = args.name
    if "/" in name or name.startswith("."):
        sys.stderr.write(f"refusing suspicious skill name: {name!r}\n")
        return EXIT_USAGE
    skill_md = _project_root() / "skills" / name / "SKILL.md"
    status = "installed" if skill_md.is_file() else "not-installed"
    sys.stdout.write(json.dumps({"name": name, "status": status}) + "\n")
    return EXIT_OK


def cmd_marketplace_list(args: argparse.Namespace) -> int:
    del args
    try:
        entries = list_skills()
    except MarketplaceError as exc:
        sys.stderr.write(f"marketplace list failed: {exc}\n")
        return EXIT_MARKETPLACE
    sys.stdout.write(json.dumps(entries, indent=2) + "\n")
    return EXIT_OK


def cmd_marketplace_info(args: argparse.Namespace) -> int:
    try:
        body = fetch_skill_md(args.name)
    except MarketplaceError as exc:
        sys.stderr.write(f"marketplace info failed: {exc}\n")
        return EXIT_MARKETPLACE
    sys.stdout.write(body)
    if not body.endswith("\n"):
        sys.stdout.write("\n")
    return EXIT_OK


def cmd_marketplace_install(args: argparse.Namespace) -> int:
    """Convenience shortcut: preview (unless --confirm) or install from
    `{MARKETPLACE_URL}/tree/{ref}/skills/<name>/`.

    Shape:
      * `marketplace install NAME`             -> preview
      * `marketplace install NAME --confirm`   -> preview + install
    """
    try:
        url = install_tree_url(args.name)
    except MarketplaceError as exc:
        sys.stderr.write(f"marketplace install failed: {exc}\n")
        return EXIT_MARKETPLACE
    preview_args = argparse.Namespace(url=url)
    rc = cmd_preview(preview_args)
    if rc != EXIT_OK or not args.confirm:
        return rc
    install_args = argparse.Namespace(url=url, confirm=True)
    return cmd_install(install_args)


# ---------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skill-installer",
        description=(
            f"Install new skills from a URL or from the Anthropic marketplace ({MARKETPLACE_REPO})."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    preview = sub.add_parser("preview", help="fetch + validate + cache a URL bundle")
    preview.add_argument("url")
    preview.set_defaults(func=cmd_preview)

    install = sub.add_parser("install", help="confirm and install a previously-previewed URL")
    install.add_argument("--confirm", action="store_true")
    install.add_argument("--url", required=True)
    install.set_defaults(func=cmd_install)

    status = sub.add_parser("status", help="poll async uv sync progress (phase-3 stub)")
    status.add_argument("name")
    status.set_defaults(func=cmd_status)

    mkt = sub.add_parser("marketplace", help="browse the Anthropic marketplace")
    mkt_sub = mkt.add_subparsers(dest="marketplace_cmd", required=True)

    mkt_list = mkt_sub.add_parser("list", help="list available marketplace skills")
    mkt_list.set_defaults(func=cmd_marketplace_list)

    mkt_info = mkt_sub.add_parser("info", help="print SKILL.md for one marketplace skill")
    mkt_info.add_argument("name")
    mkt_info.set_defaults(func=cmd_marketplace_info)

    mkt_install = mkt_sub.add_parser(
        "install", help="preview + optional install of a marketplace skill"
    )
    mkt_install.add_argument("name")
    mkt_install.add_argument("--confirm", action="store_true")
    mkt_install.set_defaults(func=cmd_marketplace_install)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
