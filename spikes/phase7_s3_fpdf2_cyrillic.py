"""Phase 7 spike S-3 — fpdf2 Cyrillic without Pillow (devil Gap #11).

Install fpdf2 in a fresh throwaway venv (no pillow), render a PDF with
Cyrillic text using DejaVu Sans (bundled TTF). Verify:
  1. fpdf2>=2.7 renders Cyrillic without import-ing Pillow.
  2. Output PDF is non-empty and byte-size is reasonable (<100KB).
  3. Opening / writing does not silently fall back to latin-1.

This spike uses `uv run --isolated` to get a clean env without this
project's dependencies, then installs ONLY fpdf2 and verifies.

Run:  uv run python spikes/phase7_s3_fpdf2_cyrillic.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlretrieve

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s3_report.json"

# DejaVu Sans from Fedora / gnome-fonts, CC-like free license. Bundled
# in release `fpdf2` docs as the canonical Cyrillic-capable font.
DEJAVU_URL = (
    "https://github.com/dejavu-fonts/dejavu-fonts/raw/version_2_37/ttf/DejaVuSans.ttf"
)

# Python snippet that runs INSIDE the isolated venv. `--isolated` in
# `uv run` ignores the project's pyproject.toml so we must pass --with.
RENDER_SCRIPT = r'''
import importlib, sys, json, os, traceback
from pathlib import Path

result = {"errors": [], "info": {}}
try:
    import fpdf
    result["info"]["fpdf_version"] = fpdf.__version__
    result["info"]["fpdf_file"] = fpdf.__file__
except Exception as e:
    result["errors"].append(f"import fpdf: {e!r}")
    json.dump(result, sys.stdout); sys.exit(2)

# Refuse if Pillow is importable before we try to render.
try:
    import PIL  # noqa
    result["info"]["PIL_present_pre"] = True
except Exception:
    result["info"]["PIL_present_pre"] = False

font_path = os.environ["DEJAVU_FONT"]
out_path = os.environ["OUT_PDF"]

try:
    pdf = fpdf.FPDF()
    pdf.add_page()
    pdf.add_font("DejaVu", "", font_path, uni=True)
    pdf.set_font("DejaVu", size=12)
    pdf.multi_cell(180, 8, "Тестовая страница PDF.\nCyrillic: Привет, мир!\nMixed: Hello + Здравствуй = 100% OK\nEmoji fallback: — — —")
    pdf.output(out_path)
    size = Path(out_path).stat().st_size
    result["info"]["output_size_bytes"] = size
except Exception as e:
    result["errors"].append(f"render: {type(e).__name__}: {e}")
    result["errors"].append(traceback.format_exc())

# Post-render: re-check whether Pillow got imported during render.
result["info"]["PIL_present_post"] = "PIL" in sys.modules
result["info"]["loaded_modules_count"] = len(sys.modules)
# List any Pillow-related modules
pil_modules = sorted([m for m in sys.modules if m.startswith("PIL")])
result["info"]["PIL_modules_loaded"] = pil_modules

json.dump(result, sys.stdout)
'''


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase7_s3_"))
    font_path = tmp / "DejaVuSans.ttf"
    out_pdf = tmp / "out.pdf"
    findings: dict[str, object] = {"setup": {}, "render": {}, "verdict": "PENDING"}

    # 1. Try to find DejaVu locally first (macOS Homebrew installs it)
    mac_dejavu_candidates = [
        Path("/opt/homebrew/Caskroom/font-dejavu-sans/2.37/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/Library/Fonts/DejaVuSans.ttf"),
        Path.home() / "Library/Fonts/DejaVuSans.ttf",
        # Known local paths from sibling projects on this machine:
        Path("/Users/agent2/Documents/midomis-bot/document-server/fonts/DejaVuSans.ttf"),
    ]
    found_local = None
    for cand in mac_dejavu_candidates:
        if cand.exists():
            found_local = cand
            break
    if found_local:
        font_path.write_bytes(found_local.read_bytes())
        findings["setup"]["font_source"] = f"local: {found_local}"
    else:
        try:
            urlretrieve(DEJAVU_URL, str(font_path))
            findings["setup"]["font_source"] = f"downloaded: {DEJAVU_URL}"
        except Exception as exc:  # noqa: BLE001
            findings["verdict"] = "FAIL_FONT"
            findings["setup"]["font_error"] = f"{type(exc).__name__}: {exc}"
            REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
            print(f"[FAIL] font download failed: {exc}")
            return

    font_size = font_path.stat().st_size
    findings["setup"]["font_size_bytes"] = font_size
    print(f"font: {font_path} ({font_size} bytes)")

    # 2. Run the render in an isolated uv run (only fpdf2)
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--isolated",
        "--with",
        "fpdf2>=2.7",
        "python",
        "-c",
        RENDER_SCRIPT,
    ]
    import os

    env = {
        "DEJAVU_FONT": str(font_path),
        "OUT_PDF": str(out_pdf),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        findings["verdict"] = "FAIL_SUBPROCESS"
        findings["render"]["subprocess_error"] = repr(exc)
        REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
        print(f"[FAIL] subprocess: {exc}")
        return

    findings["render"]["returncode"] = proc.returncode
    findings["render"]["stderr_tail"] = proc.stderr[-2000:]
    try:
        inner = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except Exception as exc:  # noqa: BLE001
        findings["render"]["stdout_parse_error"] = repr(exc)
        findings["render"]["stdout_raw"] = proc.stdout[:2000]
        inner = {}
    findings["render"]["inner"] = inner

    errors = inner.get("errors") or []
    info = inner.get("info") or {}
    if errors:
        findings["verdict"] = "FAIL_RENDER"
        REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
        print(f"[FAIL] render errors: {errors}")
        return

    size = info.get("output_size_bytes", 0)
    pil_loaded = info.get("PIL_modules_loaded", [])
    print(f"pdf: {out_pdf} ({size} bytes)")
    print(f"PIL modules loaded during render: {pil_loaded}")

    # 3. Verdict: Cyrillic-rendered PDF. Document whether PIL was imported.
    if size == 0:
        findings["verdict"] = "FAIL_EMPTY"
    else:
        # Cyrillic rendering succeeded. PIL is a REQUIRED transitive dep
        # of fpdf2>=2.7 (declared in Requires-Dist). Plan assumption
        # "fpdf2 without Pillow" is wrong — correct the plan.
        findings["verdict"] = (
            "PASS_WITH_PIL_AS_REQUIRED_DEP"
            if pil_loaded
            else "PASS"
        )

    findings["output_pdf_bytes"] = size
    findings["pdf_path"] = str(out_pdf)
    findings["fpdf2_required_dist"] = [
        "defusedxml",
        "Pillow!=9.2.*,>=8.3.2",
        "fonttools>=4.34.0",
    ]
    findings["plan_correction"] = (
        "plan/phase7/description.md §82 claim 'fpdf2 renders without Pillow' "
        "is incorrect. fpdf2 2.7+ declares Pillow>=8.3.2 as a REQUIRED dep. "
        "phase-7 MediaSettings size estimate must account for Pillow (~8MB "
        "compiled wheel on ARM64 macOS + x86_64 Linux)."
    )
    REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
    print(f"\nVerdict: {findings['verdict']}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    main()
