#!/usr/bin/env python3
"""One-off migration script: midomis-bot vault -> 0xone-assistant vault.

Source layout (midomis, per-user):
    <midomis_data>/users/<chat_id>/vault/<area>/<note>.md

Target layout (0xone, single-user):
    <target_vault>/<area>/<note>.md

Frontmatter transform:
    midomis schema: {created, tags, source}
    0xone schema:   {title, area, tags, created}     (phase 4 contract)

Rules:
    * title:  derived from filename stem (kebab/snake -> Title Case; cyrillic
              keeps its casing but first letter is capitalized).
    * area:   parent folder name, unchanged.
    * tags:   preserved as-is (if present).
    * created: preserved if present; otherwise synthesized from file mtime
               (UTC date ISO-8601).
    * source: DROPPED (midomis-only, not in 0xone schema).

    * _index.md files are skipped entirely -- 0xone uses FTS5 to generate
      virtual indexes, old index files would be orphans.
    * Files without frontmatter get synthetic frontmatter.
    * Files with malformed YAML frontmatter are logged as errors and skipped;
      migration continues with remaining files.
    * Body (everything after the closing `---\\n`) is preserved byte-for-byte,
      including wiki-links [[link]], image embeds ![[img.png]], code fences,
      and cyrillic text.

Exit codes:
    0  success, all files migrated (or cleanly skipped)
    1  fatal error (source path missing, argparse failure, etc.)
    2  partial success -- at least one file produced an error, but the run
       completed and wrote what it could
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # PyYAML, already a project dep (see pyproject.toml)

# Matches an Obsidian-style YAML frontmatter block at the very top of a file.
# Note: `re.DOTALL` lets `.` span newlines so the YAML body can be multi-line.
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)

# Directory mode 0o700 matches the rest of 0xone-assistant's data-dir hardening
# (see src/assistant/paths.py and phase-4 vault spec).
_DIR_MODE = 0o700


def title_from_filename(stem: str) -> str:
    """Derive a human title from a filename stem.

    ASCII examples:
        "studio44-workload-platform" -> "Studio44 Workload Platform"
        "api_gateway_v2"             -> "Api Gateway V2"
        "simple"                     -> "Simple"

    Cyrillic (and other non-ASCII scripts) keep their original casing on the
    tail of each word; only the first code-point is upper-cased. This matters
    for abbreviations like "GPT" or mixed-case names -- str.title() would
    wrongly lower-case them. See test_cyrillic_* in the sibling test file
    for concrete examples.

    Empty / pathological stems fall back to the original stem so we never
    produce an empty title (phase 4 spec says title is mandatory).
    """
    words = [w for w in re.split(r"[-_]+", stem) if w]
    if not words:
        return stem or "untitled"
    # Per-word: uppercase first code-point, keep rest as-is.
    # We deliberately do NOT use str.title() -- it lower-cases the tail
    # which would destroy things like "v2" -> "V2" becoming "V2" fine, but
    # "GPT4" -> "Gpt4" (wrong). The "first-char upper + rest verbatim" rule
    # is safer and works for cyrillic too.
    return " ".join(w[:1].upper() + w[1:] for w in words)


def parse_source(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter_dict, body_str).

    Returns ({}, text) for files with no frontmatter at all.
    Raises ValueError on malformed YAML so the caller can log and skip.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        parsed = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ValueError(f"malformed frontmatter: {e}") from e
    if parsed is None:
        # Empty frontmatter block ("---\n---\n") is legal -- treat as {}.
        parsed = {}
    if not isinstance(parsed, dict):
        # Non-mapping YAML (e.g. a bare list at top level) is not valid
        # frontmatter; surface it so the operator can fix the source.
        raise ValueError(
            f"frontmatter is not a mapping (got {type(parsed).__name__})"
        )
    return parsed, m.group(2)


def transform_frontmatter(
    src_fm: dict[str, Any],
    filename_stem: str,
    area: str,
    file_mtime: float,
) -> dict[str, Any]:
    """Build the 0xone-schema frontmatter dict.

    Field order in the output is fixed (title, area, tags, created) so the
    on-disk YAML is deterministic and diffs stay small during re-runs.
    """
    out: dict[str, Any] = {
        "title": title_from_filename(filename_stem),
        "area": area,
    }

    tags = src_fm.get("tags")
    if tags is not None:
        # Normalise to a plain list to avoid YAML dumping tuples/sets weirdly.
        if isinstance(tags, (list, tuple)):
            out["tags"] = [str(t) for t in tags]
        elif isinstance(tags, str):
            # Some notes stored tags as a comma-separated string; keep that
            # shape (passing it through as-is) -- 0xone's frontmatter parser
            # tolerates either form per phase-4 spec.
            out["tags"] = tags
        else:
            # Unknown shape -- stringify to preserve information without
            # losing the file.
            out["tags"] = [str(tags)]

    created = src_fm.get("created")
    if created is not None:
        out["created"] = created
    else:
        out["created"] = (
            datetime.fromtimestamp(file_mtime, tz=UTC).date().isoformat()
        )

    # `source:` from midomis schema is intentionally dropped.
    return out


def dump_frontmatter(fm: dict[str, Any]) -> str:
    """Serialise frontmatter dict to YAML, preserving key order and unicode."""
    return yaml.safe_dump(
        fm,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )


def migrate_file(
    src_path: Path,
    area: str,
    target_dir: Path,
    dry_run: bool,
) -> dict[str, Any]:
    """Migrate a single .md file.

    Returns a dict with one of the following shapes:
        {"action": "skipped_index", "path": "..."}
        {"action": "migrated",      "path": "...", "target": "..."}
        {"action": "error",         "path": "...", "reason": "..."}
    """
    if src_path.name == "_index.md":
        return {"action": "skipped_index", "path": str(src_path)}

    try:
        text = src_path.read_text(encoding="utf-8")
    except OSError as e:
        return {"action": "error", "path": str(src_path), "reason": f"read failed: {e}"}

    try:
        src_fm, body = parse_source(text)
    except ValueError as e:
        return {"action": "error", "path": str(src_path), "reason": str(e)}

    try:
        mtime = src_path.stat().st_mtime
    except OSError:
        # Very unlikely after a successful read, but fall back to "now" so
        # we still produce a valid `created:` field.
        mtime = datetime.now(tz=UTC).timestamp()

    new_fm = transform_frontmatter(src_fm, src_path.stem, area, mtime)

    target_area_dir = target_dir / area
    target_path = target_area_dir / src_path.name

    if not dry_run:
        try:
            target_area_dir.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            # Note: body already contains its leading newline (if any) because
            # FRONTMATTER_RE consumes exactly one `\n` after the closing `---`.
            # We reattach `---\n{yaml}---\n` verbatim.
            target_path.write_text(
                f"---\n{dump_frontmatter(new_fm)}---\n{body}",
                encoding="utf-8",
            )
        except OSError as e:
            return {
                "action": "error",
                "path": str(src_path),
                "reason": f"write failed: {e}",
            }

    return {
        "action": "migrated",
        "path": str(src_path),
        "target": str(target_path),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migrate_midomis_vault",
        description=(
            "Migrate a single user's Obsidian vault from midomis-bot layout "
            "to 0xone-assistant single-user layout. Transforms frontmatter "
            "per phase-4 memory spec."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the midomis data dir (the one that contains users/). "
        "Example: /tmp/midomis-backup-2026-04-20/data",
    )
    parser.add_argument(
        "--chat-id",
        required=True,
        help="The midomis chat_id (user id) whose vault will be migrated. "
        "Example: 177309887",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Destination 0xone vault dir. Example: /opt/0xone-assistant/data/vault",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write any files; just report what would happen.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    """Main entry-point. Returns the process exit code."""
    args = _parse_args(argv)

    source_data = Path(args.source).resolve()
    vault_src = source_data / "users" / args.chat_id / "vault"
    target = Path(args.target).resolve()

    if not vault_src.is_dir():
        print(
            f"fatal: source vault not found at {vault_src}",
            file=sys.stderr,
        )
        return 1

    results: dict[str, Any] = {
        "source_files": 0,
        "skipped_index": 0,
        "migrated": 0,
        "errors": [],
        "target_dir": str(target),
        "areas": [],
    }
    areas_seen: set[str] = set()

    # Sorted traversal gives deterministic output -- important for tests and
    # for the operator to eyeball progress.
    for md in sorted(vault_src.rglob("*.md")):
        rel = md.relative_to(vault_src)
        results["source_files"] += 1

        if len(rel.parts) < 2:
            # A file directly under vault/ (e.g. vault/_index.md or an
            # orphan at the root) -- count it as a skipped index-like file.
            # 0xone's area-based layout has no concept of root-level notes.
            results["skipped_index"] += 1
            if md.name != "_index.md":
                print(
                    f"warning: skipping root-level file {md} (not in any area)",
                    file=sys.stderr,
                )
            continue

        area = rel.parts[0]
        r = migrate_file(md, area, target, args.dry_run)

        if r["action"] == "skipped_index":
            results["skipped_index"] += 1
        elif r["action"] == "error":
            results["errors"].append(r)
            print(
                f"warning: error on {r['path']}: {r['reason']}",
                file=sys.stderr,
            )
        else:  # migrated
            results["migrated"] += 1
            areas_seen.add(area)

    results["areas"] = sorted(areas_seen)

    print(json.dumps(results, ensure_ascii=False, indent=2))

    return 2 if results["errors"] else 0


def main() -> None:  # pragma: no cover - thin wrapper for `python -m` use
    sys.exit(run())


if __name__ == "__main__":
    main()
