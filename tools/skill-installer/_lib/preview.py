"""Human-readable preview renderer for skill-installer.

Output is plain text (no markdown rendering — sent straight to Telegram
which in phase 3 has `parse_mode=None`). Caps the file list at 40 entries
so a 100-file bundle doesn't spam the chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FILE_LIST_LIMIT = 40


def render_preview(url: str, bundle: Path, bundle_sha: str, report: dict[str, Any]) -> str:
    """Return a multi-line plain-text preview of `bundle`.

    Shape (stable; skill-installer SKILL.md references the `To install run`
    line so the model knows exactly which command to surface to the user):

        Preview of <url>
        name: <slug>
        description: <one-line description>
        allowed-tools: missing (permissive default) | [Bash, Read]
        file_count / total_size / bundle_sha (first 16)
        files:
          - SKILL.md
          - ...
        To install run: python tools/skill-installer/main.py install --confirm --url <URL>
    """
    lines = [f"Preview of {url}"]
    lines.append(f"name:        {report['name']}")
    lines.append(f"description: {report['description']}")
    allowed = report.get("allowed_tools")
    if allowed is None:
        lines.append("allowed-tools: missing (phase-3 global baseline applies)")
    elif allowed == []:
        lines.append("allowed-tools: [] (lockdown not enforced in phase 3)")
    else:
        lines.append(f"allowed-tools: {list(allowed)}")
    lines.append(f"file_count:  {report['file_count']}")
    lines.append(f"total_size:  {report['total_size']} bytes")
    lines.append(f"bundle_sha:  {bundle_sha[:16]}...")
    lines.append(f"has_inner_tools: {report.get('has_inner_tools', False)}")

    rel_paths = sorted(
        str(p.relative_to(bundle).as_posix())
        for p in bundle.rglob("*")
        if p.is_file() and not p.is_symlink()
    )
    lines.append(f"files ({len(rel_paths)}):")
    for rp in rel_paths[:FILE_LIST_LIMIT]:
        lines.append(f"  - {rp}")
    if len(rel_paths) > FILE_LIST_LIMIT:
        lines.append(f"  ... and {len(rel_paths) - FILE_LIST_LIMIT} more")

    lines.append("")
    lines.append(
        f"To install run: python tools/skill-installer/main.py install --confirm --url {url}"
    )
    return "\n".join(lines)
