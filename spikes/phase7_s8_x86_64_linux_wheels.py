"""Spike S-8 — x86_64 manylinux wheel availability for phase-7 deps.

Runs `uv pip compile --only-binary=:all:` against a pinned set of phase-7
deps (pypdf, python-docx, openpyxl, striprtf, defusedxml, fpdf2, Pillow
with safe CVE floor, lxml, fonttools) under the `x86_64-manylinux_2_28`
platform tag. This proves that the owner's Linux VPS (glibc-based,
NOT Alpine/musl) can `uv sync` without a source build.

Rationale: devil wave-2 H-7 flagged that S-4 measured dep delta on
macOS ARM64; we never verified manylinux wheels. A source-build on VPS
would require system `libxml2-dev` (for lxml), `libjpeg-dev` + zlib
(for Pillow), and a functional C toolchain. If any required dep lacked
a wheel we would have to document the runtime prerequisite and possibly
carry a compile step through `uv sync`.

Output: spikes/phase7_s8_report.json — per-pkg wheel tag observed by
uv's resolver, plus a rolled-up "all-wheels-available" verdict.

Run with: uv run python spikes/phase7_s8_x86_64_linux_wheels.py

Exit codes: 0 ok, 1 unexpected failure (missing wheel / resolver error).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPORT = Path(__file__).with_name("phase7_s8_report.json")

REQS = [
    "pypdf>=4.0",
    "python-docx>=1.0",
    "openpyxl>=3.1",
    "striprtf>=0.0.28",
    "defusedxml>=0.7",
    "fpdf2>=2.7",
    "Pillow>=10.4,<13",  # CVE floor per H-8
    "lxml>=5.0",
    "fonttools>=4.34",
]


def main() -> int:
    reqs_in = Path("/tmp/phase7_s8_reqs.in")
    reqs_in.write_text("\n".join(REQS) + "\n")

    cmd = [
        "uv", "pip", "compile",
        "--python-platform", "x86_64-manylinux_2_28",
        "--python-version", "3.12",
        "--only-binary=:all:",
        str(reqs_in),
        "-o", "/tmp/phase7_s8_reqs.txt",
        "-v",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    report: dict[str, object] = {
        "cmd": cmd,
        "returncode": proc.returncode,
        "platform_tag": "x86_64-manylinux_2_28",
        "python_version": "3.12",
        "resolved": [],
        "wheel_tags_observed": {},
    }
    if proc.returncode != 0:
        report["error"] = proc.stderr[-4000:]
        REPORT.write_text(json.dumps(report, indent=2))
        return 1

    # Parse resolved versions from stdout (requirements.txt format).
    for line in Path("/tmp/phase7_s8_reqs.txt").read_text().splitlines():
        m = re.match(r"^([a-zA-Z0-9_.\-]+)==([0-9][^ ]*)$", line.strip())
        if m:
            report["resolved"].append({"name": m.group(1), "version": m.group(2)})

    # Parse wheel selections from verbose stderr.
    # Format: "Selecting: pillow==12.2.0 [preference] (pillow-12.2.0-cp312-…-manylinux….whl)"
    rx = re.compile(
        r"Selecting:\s+([a-zA-Z0-9_.\-]+)==([^\s]+)\s+\[[^\]]*\]\s+\(([^)]+)\)"
    )
    for m in rx.finditer(proc.stderr):
        name, ver, filename = m.group(1), m.group(2), m.group(3)
        # Pure-python wheels carry `py3-none-any` tag and work on every
        # platform, including manylinux; flag separately so the verdict
        # is only failed for C-extensions without a manylinux wheel.
        is_pure = "py3-none-any" in filename or "py2.py3-none-any" in filename
        report["wheel_tags_observed"][name] = {
            "version": ver,
            "wheel": filename,
            "is_manylinux": "manylinux" in filename,
            "is_pure_python": is_pure,
        }

    # Verdict: every resolved pkg must have EITHER a manylinux wheel OR
    # a pure-python (py3-none-any) wheel. Anything else = source build risk.
    missing_wheels = [
        r["name"]
        for r in report["resolved"]
        if r["name"] not in report["wheel_tags_observed"]
    ]
    non_manylinux_c_ext = [
        name
        for name, info in report["wheel_tags_observed"].items()
        if not info["is_manylinux"] and not info["is_pure_python"]
    ]
    report["missing_wheels"] = missing_wheels
    report["non_manylinux_c_ext"] = non_manylinux_c_ext
    report["all_binaries_available"] = not (missing_wheels or non_manylinux_c_ext)
    report["verdict"] = "PASS" if report["all_binaries_available"] else "FAIL"

    REPORT.write_text(json.dumps(report, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
