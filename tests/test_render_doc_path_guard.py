"""Phase 7 / commit 18k — explicit path-guard rejection matrix for
``tools/render_doc/main.py``.

``tests/test_tools_render_doc_cli.py`` already exercises the CLI end to
end; this file is the focused **rejection matrix** that Wave-11 /
commit 18k carved out so reviewers can spot-check the invariants (I-7.3
in detailed-plan §7) without wading through the broader render suite.

Covered cases (each maps to a row in the plan's rejection matrix):

  1. ``--body-file`` OUTSIDE ``<data_dir>/run/render-stage/``   → exit 3.
  2. ``--body-file`` via symlink that ESCAPES the stage-dir     → exit 3.
  3. ``--body-file`` inside the stage-dir but MISSING           → exit 3.
  4. ``--out`` OUTSIDE ``<data_dir>/media/outbox/``             → exit 3.
  5. ``--out`` inside outbox but wrong suffix (``.jpg``)        → exit 3.
  6. ``--out`` pointing at an EXISTING file                     → exit 0
     (current policy: ``os.replace`` overwrites the target; documented
     here so any future tightening surfaces as an explicit test break).
  7. Stage-dir body with ``..`` components that STAY inside stage → exit 0.
  8. Stage-dir body with ``..`` that escapes the stage            → exit 3.
  9. Happy-path PDF + DOCX under the sanctioned tree              → exit 0.

The CLI is driven via ``subprocess.run`` so argparse, env resolution and
the ``resolve(strict=True) + is_relative_to`` combo all execute as the
real Bash-hook caller would see them. We purposefully do NOT import
``tools.render_doc.main`` here — that would bypass the argparse
boundary where real path rejection happens.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CLI = _PROJECT_ROOT / "tools" / "render_doc" / "main.py"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Mirror the tree ``Daemon.start`` creates (0o700 already granted
    by the tmp_path fixture default perms; the CLI only checks for
    existence + containment, not mode)."""
    (tmp_path / "run" / "render-stage").mkdir(parents=True)
    (tmp_path / "media" / "outbox").mkdir(parents=True)
    return tmp_path


def _stage_body(data_dir: Path, name: str, content: str = "hello") -> Path:
    path = data_dir / "run" / "render-stage" / name
    path.write_text(content, encoding="utf-8")
    return path


def _run(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI with ``ASSISTANT_DATA_DIR`` pinned to our tmp tree.

    We strip the ambient environment so the test never picks up the
    caller's real ``XDG_DATA_HOME`` (macOS dev laptops carry one) and so
    ``PATH`` is trimmed to system binaries only — fpdf2 / python-docx
    are resolved via ``sys.executable`` anyway.
    """
    env = {
        "ASSISTANT_DATA_DIR": str(data_dir),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(data_dir),
    }
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_PROJECT_ROOT),
        timeout=60,
    )


def _parse_err(proc: subprocess.CompletedProcess[str]) -> dict:
    """Last stderr line is the JSON error envelope (traceback lines, if
    any, precede it)."""
    stderr_lines = [ln for ln in proc.stderr.strip().splitlines() if ln]
    assert stderr_lines, f"no stderr from CLI; stdout={proc.stdout!r}"
    return json.loads(stderr_lines[-1])


def _parse_ok(proc: subprocess.CompletedProcess[str]) -> dict:
    stdout_lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    assert stdout_lines, f"no stdout from CLI; stderr={proc.stderr!r}"
    return json.loads(stdout_lines[-1])


# ---------------------------------------------------------------------------
# --body-file rejections
# ---------------------------------------------------------------------------


def test_body_file_outside_stage_dir_rejected(
    data_dir: Path, tmp_path: Path
) -> None:
    """Row 1 of the matrix: body staged anywhere other than the
    sanctioned ``<data_dir>/run/render-stage/`` subtree exits 3."""
    rogue = tmp_path / "rogue-body.txt"
    rogue.write_text("not allowed", encoding="utf-8")
    out = data_dir / "media" / "outbox" / "x.pdf"

    proc = _run(data_dir, "--body-file", str(rogue), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert err["ok"] is False
    assert "--body-file" in err["error"]
    assert "must live under" in err["error"]
    # Guard MUST short-circuit before any renderer runs.
    assert not out.exists()


def test_body_file_symlink_escapes_stage_rejected(
    data_dir: Path, tmp_path: Path
) -> None:
    """Row 2: a symlink inside stage-dir that points OUTSIDE must be
    rejected — ``resolve(strict=True)`` follows the link so the target
    path lands outside the stage root.
    """
    target = tmp_path / "external-body.txt"
    target.write_text("escape payload", encoding="utf-8")
    link = data_dir / "run" / "render-stage" / "evil.txt"
    link.symlink_to(target)
    out = data_dir / "media" / "outbox" / "x.pdf"

    proc = _run(data_dir, "--body-file", str(link), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert "must live under" in err["error"]
    assert not out.exists()


def test_body_file_inside_stage_but_missing_rejected(data_dir: Path) -> None:
    """Row 3: a path that *would* live under the stage dir but does not
    exist on disk exits 3 via the ``resolve(strict=True)`` branch —
    crucial so the CLI never silently creates stage-dir files for the
    daemon."""
    ghost = data_dir / "run" / "render-stage" / "ghost.txt"  # never written
    out = data_dir / "media" / "outbox" / "x.pdf"

    proc = _run(data_dir, "--body-file", str(ghost), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert err["ok"] is False
    # ``resolve(strict=True)`` raises ``FileNotFoundError`` which the
    # guard formats as "cannot be resolved".
    assert "cannot be resolved" in err["error"] or "must live under" in err["error"]
    assert not out.exists()


def test_body_file_dotdot_but_resolves_inside_stage_accepted(
    data_dir: Path,
) -> None:
    """Row 7 (positive): ``a/../b.txt`` pattern that collapses to a
    legitimate stage-dir path MUST be accepted. The guard relies on
    ``resolve()``, not lexical inspection, so harmless ``..`` traversal
    inside the stage must not trip the guard."""
    stage = data_dir / "run" / "render-stage"
    (stage / "sub").mkdir()
    real = _stage_body(data_dir, "straight.txt", "hello dotdot")
    twisted = stage / "sub" / ".." / "straight.txt"
    assert twisted.resolve() == real.resolve()

    out = data_dir / "media" / "outbox" / "ok.pdf"
    proc = _run(data_dir, "--body-file", str(twisted), "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    payload = _parse_ok(proc)
    assert payload["ok"] is True
    assert payload["data"]["format"] == "pdf"
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")


def test_body_file_dotdot_escapes_stage_rejected(
    data_dir: Path, tmp_path: Path
) -> None:
    """Row 8: the ``..``-escape mirror image — a path whose ``resolve()``
    lands OUTSIDE the stage-dir must fail even though it lexically
    begins with the stage-dir prefix.

    We plant a real file one level above the stage dir and point at it
    with a ``render-stage/../external.txt`` spelling. ``resolve()``
    normalises away the ``..`` and the ``is_relative_to(stage)`` check
    rejects it.
    """
    external = data_dir / "run" / "external-body.txt"
    external.write_text("outside stage", encoding="utf-8")
    twisted = data_dir / "run" / "render-stage" / ".." / "external-body.txt"
    assert twisted.resolve() == external.resolve()

    out = data_dir / "media" / "outbox" / "x.pdf"
    proc = _run(data_dir, "--body-file", str(twisted), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert "must live under" in err["error"]
    assert not out.exists()


# ---------------------------------------------------------------------------
# --out rejections
# ---------------------------------------------------------------------------


def test_out_outside_outbox_rejected(
    data_dir: Path, tmp_path: Path
) -> None:
    """Row 4: target outside ``<data_dir>/media/outbox/`` exits 3."""
    body = _stage_body(data_dir, "stage-out-escape.txt", "body")
    rogue_out = tmp_path / "escaped.pdf"  # Not even inside data_dir.

    proc = _run(data_dir, "--body-file", str(body), "--out", str(rogue_out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert "--out" in err["error"]
    assert "must live under" in err["error"]
    assert not rogue_out.exists()


def test_out_inside_outbox_wrong_suffix_rejected(data_dir: Path) -> None:
    """Row 5: suffix ``.jpg`` (or anything other than ``.pdf`` /
    ``.docx``) is rejected with exit 3 by ``_resolve_out_path``. Keeping
    this distinct from ``.html`` (covered in test_tools_render_doc_cli)
    so the allowlist stays honest: only the two document formats are
    permitted even if the caller picked a familiar media suffix."""
    body = _stage_body(data_dir, "stage-jpg.txt", "body")
    out = data_dir / "media" / "outbox" / "report.jpg"

    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = _parse_err(proc)
    assert "suffix" in err["error"]
    assert ".jpg" in err["error"]
    assert not out.exists()


def test_out_pointing_at_existing_file_overwrites(data_dir: Path) -> None:
    """Row 6: current policy is to OVERWRITE via ``os.replace``. This
    test nails down that behaviour so a future tightening (e.g.
    ``O_EXCL``) shows up as an explicit regression rather than silent
    data loss.

    We seed the outbox with a stub file sharing the target name and
    confirm the rendered PDF replaces it byte-for-byte (real ``%PDF-``
    header, not the stub content).
    """
    body = _stage_body(data_dir, "stage-overwrite.txt", "payload body")
    out = data_dir / "media" / "outbox" / "existing.pdf"
    out.write_bytes(b"STALE")  # pre-existing target
    assert out.read_bytes() == b"STALE"

    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    payload = _parse_ok(proc)
    assert payload["ok"] is True
    assert payload["data"]["format"] == "pdf"
    # Overwrite actually happened — we see a real PDF, not the stub.
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")
    assert out.stat().st_size == payload["data"]["bytes"]


# ---------------------------------------------------------------------------
# Happy paths (PDF + DOCX) — required by the matrix to anchor the
# negative cases above against a known-good baseline.
# ---------------------------------------------------------------------------


def test_happy_path_pdf_render(data_dir: Path) -> None:
    body = _stage_body(
        data_dir,
        "stage-happy-pdf.txt",
        "Line one.\nLine two.\n",
    )
    out = data_dir / "media" / "outbox" / "happy.pdf"

    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    payload = _parse_ok(proc)
    assert payload["ok"] is True
    assert payload["data"]["format"] == "pdf"
    assert payload["data"]["out"] == str(out)
    assert out.exists()
    assert out.read_bytes().startswith(b"%PDF-")


def test_happy_path_docx_render(data_dir: Path) -> None:
    body = _stage_body(
        data_dir,
        "stage-happy-docx.txt",
        "Paragraph one.\nParagraph two.\n",
    )
    out = data_dir / "media" / "outbox" / "happy.docx"

    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 0, proc.stderr
    payload = _parse_ok(proc)
    assert payload["ok"] is True
    assert payload["data"]["format"] == "docx"
    assert out.exists()
    # DOCX is a zip — the signature proves python-docx produced a real
    # package instead of silently writing a text blob.
    assert out.read_bytes().startswith(b"PK\x03\x04")
