"""Phase 7 spike S-4 — shared venv transitive deps (devil Gap #12).

Probes the dep tree + artefact size when we add the full phase-7 stack:

    pypdf python-docx openpyxl striprtf defusedxml fpdf2

Critical to verify:
  * Presence / absence of lxml (C extension that can add ~4MB per wheel)
  * Presence of Pillow (fpdf2 transitive — confirmed in S-3)
  * Total venv size delta
  * ARM64 vs x86_64 wheel availability (for macOS dev + Linux deploy)

Uses `uv pip install --dry-run` against an isolated venv so we don't
mutate the project's .venv.

Run:  uv run python spikes/phase7_s4_venv_deps.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s4_report.json"

PHASE7_DEPS = [
    "pypdf>=4.0",
    "python-docx>=1.0",
    "openpyxl>=3.1",
    "striprtf>=0.0.28",
    "defusedxml>=0.7",
    "fpdf2>=2.7",
]


def _du(path: Path) -> int:
    """Total size in bytes of files under path."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def main() -> None:
    findings: dict[str, object] = {
        "deps_requested": PHASE7_DEPS,
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s4_"))
    venv = tmp / "venv"

    # 1. Create clean venv
    r = subprocess.run(
        ["uv", "venv", str(venv), "--python", "3.12"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        findings["verdict"] = "FAIL_VENV_CREATE"
        findings["error"] = r.stderr
        REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
        return

    # 2. Size baseline (empty venv)
    baseline_bytes = _du(venv)
    findings["baseline_venv_bytes"] = baseline_bytes

    # 3. Install with uv pip (actually install, not dry-run, so we get size + tree)
    import os

    env = {
        "VIRTUAL_ENV": str(venv),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    r = subprocess.run(
        ["uv", "pip", "install", *PHASE7_DEPS],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    findings["install_returncode"] = r.returncode
    findings["install_stderr_tail"] = r.stderr[-2000:]
    findings["install_stdout_tail"] = r.stdout[-2000:]
    if r.returncode != 0:
        findings["verdict"] = "FAIL_INSTALL"
        REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
        print(f"[FAIL] install: {r.stderr}")
        return

    # 4. Size after install
    after_bytes = _du(venv)
    findings["after_install_venv_bytes"] = after_bytes
    findings["delta_bytes"] = after_bytes - baseline_bytes
    findings["delta_mb"] = round((after_bytes - baseline_bytes) / (1024 * 1024), 2)

    # 5. Per-package size (site-packages breakdown)
    sp_candidates = list(venv.glob("lib/python*/site-packages"))
    if sp_candidates:
        sp = sp_candidates[0]
        sizes: dict[str, int] = {}
        for entry in sp.iterdir():
            if entry.is_dir():
                sizes[entry.name] = _du(entry)
        findings["site_packages_sizes_bytes"] = dict(sorted(sizes.items(), key=lambda kv: -kv[1]))
        findings["lxml_present"] = "lxml" in sizes
        findings["pillow_present"] = "PIL" in sizes or any(n.lower().startswith("pillow") for n in sizes)
        findings["fonttools_present"] = any(n == "fontTools" for n in sizes)

    # 6. Dry-run dep tree (only)
    r_tree = subprocess.run(
        ["uv", "pip", "list", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if r_tree.returncode == 0:
        try:
            pkgs = json.loads(r_tree.stdout)
            findings["installed_packages"] = sorted(
                [{"name": p["name"], "version": p["version"]} for p in pkgs],
                key=lambda x: x["name"].lower(),
            )
        except json.JSONDecodeError:
            findings["pip_list_raw"] = r_tree.stdout

    # 7. Verdict
    if after_bytes - baseline_bytes > 200 * 1024 * 1024:
        findings["verdict"] = "PARTIAL_LARGE_DELTA"
    else:
        findings["verdict"] = "PASS"

    # Cleanup
    shutil.rmtree(tmp, ignore_errors=True)

    REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
    print(f"delta_mb: {findings['delta_mb']}  lxml_present={findings.get('lxml_present')}  pillow_present={findings.get('pillow_present')}")
    print(f"verdict: {findings['verdict']}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    main()
