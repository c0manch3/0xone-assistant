"""Probe: marketplace shape against anthropics/skills via `gh api`.

Run:

    uv run python spikes/marketplace_probe.py

Emits `spikes/marketplace_probe_report.json`. No auth scope needed beyond
public read (works even when `gh auth status` reports "not logged in" — at
60 req/hour lower limit). Confirmed empirically on 2026-04-17:

  * `/repos/anthropics/skills/contents/skills` -> 17 entries (all dirs).
  * Every SKILL.md inspected has only `name` + `description` (+ optional
    `license`) in frontmatter. `allowed-tools` is absent in all 5 sampled.
  * No top-level `tools/` subdirectory inside any skill bundle.
  * No symlinks anywhere in the public repo (depth=1 clone audited).
  * Largest bundle: canvas-design (5.5 MB, 83 files) — within phase-3 MVP
    limits (MAX_TOTAL=10 MB, MAX_FILES=100, MAX_FILE=2 MB).
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MARKETPLACE_REPO = "anthropics/skills"
MARKETPLACE_BASE_PATH = "skills"
SAMPLES = ["skill-creator", "pdf", "docx", "mcp-builder", "claude-api"]


def gh_api(endpoint: str) -> Any:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI not found")
    proc = subprocess.run(
        [gh, "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {endpoint!r} rc={proc.returncode}: {proc.stderr[:300]}")
    return json.loads(proc.stdout)


def probe_list() -> list[dict[str, str]]:
    entries = gh_api(f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}")
    return [
        {"name": e["name"], "type": e["type"], "path": e["path"]}
        for e in entries
    ]


def probe_skill_frontmatter(name: str) -> dict[str, Any]:
    payload = gh_api(
        f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md"
    )
    assert payload["encoding"] == "base64", payload["encoding"]
    text = base64.b64decode(payload["content"]).decode("utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        frontmatter = text[:end + 3]
    else:
        frontmatter = "<none>"
    return {
        "name": name,
        "size_bytes": payload["size"],
        "has_allowed_tools": "allowed-tools" in frontmatter,
        "has_license": "license:" in frontmatter,
        "frontmatter": frontmatter,
    }


def main() -> int:
    report: dict[str, Any] = {}
    try:
        report["list"] = probe_list()
        report["list_count"] = len(report["list"])
        report["list_all_dir"] = all(e["type"] == "dir" for e in report["list"])
        report["samples"] = [probe_skill_frontmatter(n) for n in SAMPLES]
        report["samples_any_allowed_tools"] = any(
            s["has_allowed_tools"] for s in report["samples"]
        )
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    out = HERE / "marketplace_probe_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
