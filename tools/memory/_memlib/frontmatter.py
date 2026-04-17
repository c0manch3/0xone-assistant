"""YAML-frontmatter parse / serialize + body sanitiser.

G2 NOTE: deliberately separate from `src/assistant/bridge/skills.py::parse_skill`.
Different contracts:

* `parse_skill` validates `{name, description, allowed-tools}` for SKILL.md.
* `parse_note` validates `{title, tags, area, created, related}` for vault
  markdown.

Memory CLI is import-less from `src/assistant/` (phase-3 B-4 principle),
so code sharing is impossible even if the shapes overlapped (they don't).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import yaml

_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


class FrontmatterError(ValueError):
    """Raised when a note's frontmatter is malformed."""


def _normalize_tags(raw: Any) -> list[str]:
    """Tags may be `None`, a scalar string, or a list (G4).

    All three forms round-trip to `list[str]`.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    raise FrontmatterError(f"tags must be null, string, or list; got {type(raw).__name__}")


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """Return `(frontmatter_dict, body)`.

    Raises `FrontmatterError` on:
      * missing frontmatter block
      * non-dict YAML root
      * missing mandatory `title`
      * unparseable YAML
      * malformed `tags` type
    """
    match = _FRONT_RE.match(text)
    if not match:
        raise FrontmatterError("missing frontmatter (--- fences required)")
    try:
        meta_raw = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"YAML parse error: {exc}") from exc
    if meta_raw is None:
        meta_raw = {}
    if not isinstance(meta_raw, dict):
        raise FrontmatterError("frontmatter root must be a mapping")

    title = meta_raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise FrontmatterError("`title` is mandatory and must be a non-empty string")

    tags = _normalize_tags(meta_raw.get("tags"))
    area = meta_raw.get("area")
    if area is not None and not isinstance(area, str):
        raise FrontmatterError("`area` must be a string if present")
    created = meta_raw.get("created")
    if created is not None and not isinstance(created, str):
        raise FrontmatterError("`created` must be an ISO8601 string if present")
    related = meta_raw.get("related")
    if related is not None and not isinstance(related, list):
        raise FrontmatterError("`related` must be a list if present")

    body = text[match.end() :]
    return (
        {
            "title": title,
            "tags": tags,
            "area": area,
            "created": created,
            "related": [str(r) for r in (related or [])],
        },
        body,
    )


def serialize_note(frontmatter: dict[str, Any], body: str) -> str:
    """Serialize frontmatter + body into a complete markdown string.

    `created` is auto-filled with UTC ISO8601 when missing. Empty optional
    fields are dropped to keep the on-disk file tidy.
    """
    clean: dict[str, Any] = {"title": frontmatter["title"]}
    if frontmatter.get("tags"):
        clean["tags"] = list(frontmatter["tags"])
    if frontmatter.get("area"):
        clean["area"] = frontmatter["area"]
    created = frontmatter.get("created") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean["created"] = created
    if frontmatter.get("related"):
        clean["related"] = list(frontmatter["related"])
    yaml_text = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{yaml_text}---\n\n{body}"


_FENCE_RE = re.compile(r"^```")


def sanitize_body(body: str) -> str:
    """S3: reject `---` at column 0 which would spoof the frontmatter fence.

    Review wave 3 (should-fix #6): be fence-aware so code blocks that
    legitimately contain `---` at column 0 (a python docstring showing
    YAML, a markdown doc about frontmatter, …) pass through untouched.
    We still indent `---` lines that sit at column 0 in prose — those
    are the ones that could close our frontmatter block.

    Users writing plain markdown horizontal rules should prefer `***` or
    `___` anyway; the sanitiser only protects the frontmatter invariant.
    """
    out: list[str] = []
    in_fence = False
    for line in body.splitlines(keepends=True):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and line.rstrip("\r\n") == "---":
            line = " " + line
        out.append(line)
    return "".join(out)


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def extract_wikilinks(body: str) -> list[str]:
    """Return the list of wikilink targets (`[[target]]`) in order of appearance.

    Pipe-alias form `[[target|alias]]` keeps only the target.
    """
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(body)]
