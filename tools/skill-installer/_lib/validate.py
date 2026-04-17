"""Bundle validation + stable tree-hash for skill-installer.

Stdlib-only (B-4). The frontmatter parser is a minimal YAML-subset
implementation that handles scalar strings and `[a, b]`-style lists — the
only shapes ever seen in Anthropic skill manifests. Multiline / block-style
YAML is intentionally not supported; a bundle that uses it will fail the
schema check downstream, which is acceptable (and explicit) behaviour.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any

# --- Frontmatter ------------------------------------------------------------

_FRONT_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_SCALAR_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$")
_LIST_RE = re.compile(r"^\[(.*)\]$")


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the tiny YAML subset used by SKILL.md frontmatter.

    Accepts `key: value` and `key: [a, b]` forms. Surrounding quotes on
    scalars are stripped. Anything else (block-style lists, multiline
    strings) is silently ignored — the caller validates required keys
    through `required_keys` below.
    """
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    out: dict[str, Any] = {}
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        sm = _SCALAR_RE.match(line.strip())
        if not sm:
            continue
        key, value = sm.group(1), sm.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        lm = _LIST_RE.match(value)
        if lm:
            items = [x.strip().strip('"').strip("'") for x in lm.group(1).split(",")]
            out[key] = [x for x in items if x]
        else:
            out[key] = value
    return out


# --- Limits (Q5) ------------------------------------------------------------

MAX_FILES = 100
MAX_TOTAL_SIZE = 10 * 1024 * 1024
MAX_SINGLE_FILE = 2 * 1024 * 1024

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_VALID_FRONTMATTER_TOOL_NAMES: frozenset[str] = frozenset(
    {"Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch"}
)


class ValidationError(Exception):
    """Raised when a bundle fails a hard invariant."""


# --- Tree hash (B-3 + H-3) --------------------------------------------------

_HASH_SKIP_PART_NAMES: frozenset[str] = frozenset(
    {".git", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"}
)
_HASH_SKIP_NAME_SUFFIXES: tuple[str, ...] = (".pyc",)
_HASH_SKIP_EXACT_NAMES: frozenset[str] = frozenset({".DS_Store"})


def _should_hash(p: Path, root: Path) -> bool:
    """True iff `p` is a regular file (not symlink) outside the skip list."""
    if not p.is_file() or p.is_symlink():
        return False
    rel_parts = p.relative_to(root).parts
    if any(part in _HASH_SKIP_PART_NAMES for part in rel_parts):
        return False
    if p.name in _HASH_SKIP_EXACT_NAMES:
        return False
    return not any(p.name.endswith(sfx) for sfx in _HASH_SKIP_NAME_SUFFIXES)


def sha256_of_tree(root: Path) -> str:
    """Deterministic hash of all regular files under `root`.

    * Excludes `.git/`, `__pycache__/`, `.ruff_cache/`, `.mypy_cache/`,
      `.pytest_cache/`, `.DS_Store`, `*.pyc` — any of these would make
      the digest flap between re-clones of the same commit and break
      TOCTOU detection (B-3).
    * Sorts file order by `.as_posix()` relative path (H-3: portable
      across POSIX / Windows; `sorted(Path)` alone sorts on repr, which
      uses different separators).
    * Each record is length-prefixed (`len(rel).to_bytes(4, "big")` +
      `rel` + NUL + `len(data).to_bytes(8, "big")` + data) — unambiguous
      framing; two files whose names concatenate to a third can't collide.
    * `p.is_file() and not p.is_symlink()` is load-bearing: `is_file()`
      alone follows symlinks. If the validator ever slipped, the hasher
      still refuses to follow.
    """
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


# --- Bundle-level validation ------------------------------------------------


def _reject_unsafe_paths(bundle: Path) -> None:
    """Raise on the first symlink OR hardlink found anywhere under `bundle`.

    Symlinks are rejected unconditionally — even links pointing at a file
    inside the bundle. Anthropic bundles do not use symlinks (verified
    empirically in spike S1.c); a bundle that introduces one is either
    malformed or malicious, and we do not need a policy carve-out.

    Hardlinks are rejected too (review must-fix #4): `Path.is_symlink`
    returns False for a hardlink to (e.g.) `/etc/passwd`, and the
    path-traversal check `p.resolve().is_relative_to(bundle)` passes
    trivially because `resolve()` of a hardlink yields the path itself
    (hardlinks have no symbolic redirect to follow). The only reliable
    signal is `stat().st_nlink > 1` — any regular file with more than one
    hard link is suspicious in a freshly-extracted skill bundle.
    """
    for p in bundle.rglob("*"):
        # `Path.is_symlink` uses lstat under the hood — it does not follow.
        if p.is_symlink():
            try:
                target = os.readlink(p)
            except OSError:
                target = "<unreadable>"
            raise ValidationError(f"symlink not allowed: {p.relative_to(bundle)} -> {target}")
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError as exc:
            raise ValidationError(f"cannot stat {p.relative_to(bundle)}: {exc}") from exc
        if st.st_nlink > 1:
            raise ValidationError(
                f"hardlink not allowed: {p.relative_to(bundle)} (nlink={st.st_nlink})"
            )


def _reject_path_traversal(bundle: Path) -> None:
    """Refuse any regular file whose resolved path escapes `bundle`."""
    bundle_resolved = bundle.resolve()
    for p in bundle.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            resolved = p.resolve()
        except OSError as exc:
            raise ValidationError(f"unresolvable path: {p}: {exc}") from exc
        if not resolved.is_relative_to(bundle_resolved):
            raise ValidationError(f"path escapes bundle: {p.relative_to(bundle)} -> {resolved}")


def _enforce_limits(bundle: Path) -> tuple[int, int]:
    """Return `(file_count, total_size)`; raise on any cap breach."""
    count = 0
    total = 0
    for p in bundle.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        count += 1
        size = p.stat().st_size
        if size > MAX_SINGLE_FILE:
            raise ValidationError(
                f"file too large ({size} > {MAX_SINGLE_FILE}): {p.relative_to(bundle)}"
            )
        total += size
    if count > MAX_FILES:
        raise ValidationError(f"bundle has {count} files, cap is {MAX_FILES}")
    if total > MAX_TOTAL_SIZE:
        raise ValidationError(f"bundle total size {total} exceeds cap {MAX_TOTAL_SIZE}")
    return count, total


def _ast_parse_python_files(bundle: Path) -> None:
    """Best-effort syntax check of every `.py` inside the bundle.

    Refuses to copy a bundle that ships unparseable Python — if the model
    would end up invoking `python <bundle>/x.py` via Bash, we'd rather
    fail now than hand a broken skill to the operator. The check is
    static only (AST parse); we do NOT execute anything.
    """
    for p in bundle.rglob("*.py"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise ValidationError(f"python syntax error in {p.relative_to(bundle)}: {exc}") from exc


def validate_bundle(bundle: Path) -> dict[str, Any]:
    """Run all bundle-level checks; return a metadata `report` dict.

    Order matters:
      1. unsafe paths      (symlinks + hardlinks — must-fix #4)
      2. path-traversal    (defence-in-depth against lstat-skipping FS)
      3. limits            (file count + per-file + total size)
      4. AST parse         (every .py inside the bundle)
      5. SKILL.md parse    (frontmatter + required fields)
    Returned dict is consumed by `preview.render_preview` and
    `install.atomic_install` (keyed by `name`).
    """
    if not bundle.is_dir():
        raise ValidationError(f"bundle root is not a directory: {bundle}")
    _reject_unsafe_paths(bundle)
    _reject_path_traversal(bundle)
    file_count, total_size = _enforce_limits(bundle)
    _ast_parse_python_files(bundle)

    skill_md = bundle / "SKILL.md"
    if not skill_md.is_file():
        raise ValidationError("bundle is missing SKILL.md at the root")
    fm = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()
    if not name:
        raise ValidationError("SKILL.md frontmatter missing `name`")
    if not _NAME_RE.match(name):
        raise ValidationError(
            f"SKILL.md frontmatter `name` must match {_NAME_RE.pattern!r}; got {name!r}"
        )
    if not description:
        raise ValidationError("SKILL.md frontmatter missing `description`")

    # allowed-tools is optional; if present as a list, every item must be known.
    allowed_tools = fm.get("allowed-tools")
    if isinstance(allowed_tools, list):
        for tool in allowed_tools:
            if tool not in _VALID_FRONTMATTER_TOOL_NAMES:
                raise ValidationError(f"SKILL.md allowed-tools: unknown tool name {tool!r}")

    has_inner_tools = (bundle / "tools").is_dir()

    return {
        "name": name,
        "description": description,
        "allowed_tools": allowed_tools,
        "file_count": file_count,
        "total_size": total_size,
        "has_inner_tools": has_inner_tools,
    }
