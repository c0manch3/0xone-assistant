"""Anthropic marketplace wrapper around `gh api`.

Hardcoded single marketplace (M1 decision):
`https://github.com/anthropics/skills`; skills live under `skills/<name>/`
inside the repo (not at the root — verified in spike S1.a).
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from typing import Any

MARKETPLACE_URL = "https://github.com/anthropics/skills"
MARKETPLACE_REPO = "anthropics/skills"
MARKETPLACE_BASE_PATH = "skills"  # S1.a: load-bearing
MARKETPLACE_REF = "main"

GH_TIMEOUT = 30.0


class MarketplaceError(Exception):
    """Raised when the marketplace wrapper cannot complete a request."""


def _parse_gh_json(stdout: str) -> Any:
    """Skip leading `gh` warning lines; parse first JSON line we see.

    Phase-3 spike S2.d + devil's-advocate H-4: older `gh` versions
    occasionally print an "update available" banner before the JSON body.
    Scan line-by-line; the first line starting with `{` or `[` is the
    payload.
    """
    if not stdout.strip():
        raise MarketplaceError("gh api: empty stdout")
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                # Fallthrough: this line is the first line of multi-line JSON.
                break
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MarketplaceError(f"gh api returned non-JSON: {stdout[:200]!r}: {exc}") from exc


def _gh_api(endpoint: str) -> Any:
    """Invoke `gh api <endpoint>` and surface 404s as `MarketplaceError`.

    Spike S2.d: `gh api` returns rc=0 even for HTTP 404 — the 4xx body
    carries `{"message": "...", "status": "404"}`. We must sniff both.
    """
    gh = shutil.which("gh")
    if gh is None:
        raise MarketplaceError(
            "gh CLI not found on PATH; install https://cli.github.com/ "
            "to enable marketplace access."
        )
    try:
        proc = subprocess.run(
            [gh, "api", endpoint],
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise MarketplaceError(f"gh api timed out after {GH_TIMEOUT}s") from exc
    if proc.returncode != 0:
        raise MarketplaceError(
            f"gh api rc={proc.returncode}: {proc.stderr[:300] if proc.stderr else ''}"
        )
    payload = _parse_gh_json(proc.stdout)
    if isinstance(payload, dict) and "message" in payload and "status" in payload:
        raise MarketplaceError(f"GitHub API error {payload['status']}: {payload['message']}")
    return payload


def list_skills() -> list[dict[str, str]]:
    """List available skills in the marketplace.

    Returns `[{"name": <slug>, "path": <repo-path>}, ...]`. Filters to
    directory entries that don't start with `.` (rejects `.gitattributes`
    etc. that occasionally slip into the listing).
    """
    entries = _gh_api(f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}")
    if not isinstance(entries, list):
        raise MarketplaceError(
            f"marketplace index: expected JSON array, got {type(entries).__name__}"
        )
    out: list[dict[str, str]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "")
        kind = str(e.get("type") or "")
        path = str(e.get("path") or "")
        if kind != "dir" or not name or name.startswith("."):
            continue
        out.append({"name": name, "path": path})
    return out


def fetch_skill_md(name: str) -> str:
    """Return the decoded contents of `skills/<name>/SKILL.md`."""
    if "/" in name or name.startswith("."):
        raise MarketplaceError(f"refusing suspicious skill name: {name!r}")
    payload = _gh_api(f"/repos/{MARKETPLACE_REPO}/contents/{MARKETPLACE_BASE_PATH}/{name}/SKILL.md")
    if not isinstance(payload, dict):
        raise MarketplaceError(f"unexpected SKILL.md payload shape: {type(payload).__name__}")
    if payload.get("encoding") != "base64":
        raise MarketplaceError(f"SKILL.md encoding not base64: {payload.get('encoding')!r}")
    content = payload.get("content")
    if not isinstance(content, str):
        raise MarketplaceError("SKILL.md content is not a string")
    return base64.b64decode(content).decode("utf-8")


def install_tree_url(name: str) -> str:
    """Return the canonical tree URL for `skills/<name>/`.

    The installer's preview+install flow fetches via `fetch.fetch_bundle`,
    which understands `https://github.com/<owner>/<repo>/tree/<ref>/<path>`.
    """
    return f"{MARKETPLACE_URL}/tree/{MARKETPLACE_REF}/{MARKETPLACE_BASE_PATH}/{name}"
