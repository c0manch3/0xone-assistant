"""tools/render_doc CLI — happy path + path-guard tests.

The CLI is exercised via ``subprocess.run`` (not direct ``main()`` call)
so we hit the real argparse + env-resolution boundaries a Telegram turn
would hit. ``ASSISTANT_DATA_DIR`` points tests at an isolated tmp tree
seeded with the ``run/render-stage`` + ``media/outbox`` subdirs the
daemon would otherwise create.
"""
# ruff: noqa: RUF001
# Test bodies intentionally contain Cyrillic characters to verify the
# vendored DejaVu font + UTF-8 encoding path. ruff's ambiguous-letter
# detector is loud here; muting file-wide is cleaner than per-line.

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CLI = _PROJECT_ROOT / "tools" / "render_doc" / "main.py"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Seeded <data_dir> mirroring what ``Daemon.start`` would create."""
    (tmp_path / "run" / "render-stage").mkdir(parents=True)
    (tmp_path / "media" / "outbox").mkdir(parents=True)
    return tmp_path


def _stage_body(data_dir: Path, name: str, content: str) -> Path:
    path = data_dir / "run" / "render-stage" / name
    path.write_text(content, encoding="utf-8")
    return path


def _run(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "ASSISTANT_DATA_DIR": str(data_dir),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        # Avoid leaking the calling user's real XDG_DATA_HOME — we want
        # ASSISTANT_DATA_DIR to be the sole truth here.
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


# ---------------------------------------------------------------------------
# Happy-path renders
# ---------------------------------------------------------------------------


def test_render_pdf_cyrillic_body(data_dir: Path) -> None:
    body = _stage_body(
        data_dir,
        "stage-pdf.txt",
        "Привет, мир!\nMixed Cyrillic + Latin — всё в порядке.\n",
    )
    out = data_dir / "media" / "outbox" / "greetings.pdf"
    proc = _run(data_dir, "--body-file", str(body), "--out", str(out),
                "--title", "Проверка")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["data"]["format"] == "pdf"
    assert out.exists()
    # PDF magic header — guarantees fpdf2 produced a real document.
    assert out.read_bytes().startswith(b"%PDF-")
    assert out.stat().st_size == payload["data"]["bytes"]


def test_render_docx_cyrillic_body(data_dir: Path) -> None:
    body = _stage_body(
        data_dir,
        "stage-docx.txt",
        "Отчёт по проекту.\nСтрока два.\n",
    )
    out = data_dir / "media" / "outbox" / "report.docx"
    proc = _run(data_dir, "--body-file", str(body), "--out", str(out),
                "--title", "Отчёт")

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["data"]["format"] == "docx"
    assert out.exists()
    # DOCX is a zip; PK\x03\x04 signature confirms python-docx produced
    # a real package rather than silently falling back to a text blob.
    assert out.read_bytes().startswith(b"PK\x03\x04")


# ---------------------------------------------------------------------------
# Path-guard rejections
# ---------------------------------------------------------------------------


def test_body_file_outside_stage_rejected(data_dir: Path, tmp_path: Path) -> None:
    # Body staged OUTSIDE the sanctioned render-stage dir.
    rogue = tmp_path / "rogue.txt"
    rogue.write_text("nope", encoding="utf-8")
    out = data_dir / "media" / "outbox" / "x.pdf"
    proc = _run(data_dir, "--body-file", str(rogue), "--out", str(out))

    assert proc.returncode == 3, proc.stderr
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert err["ok"] is False
    assert "must live under" in err["error"]
    assert not out.exists()


def test_out_outside_outbox_rejected(data_dir: Path, tmp_path: Path) -> None:
    body = _stage_body(data_dir, "stage-escape.txt", "body")
    rogue_out = tmp_path / "escaped.pdf"  # Outside data_dir/media/outbox/.
    proc = _run(data_dir, "--body-file", str(body), "--out", str(rogue_out))

    assert proc.returncode == 3, proc.stderr
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert err["ok"] is False
    assert "must live under" in err["error"]
    assert not rogue_out.exists()


def test_unsupported_suffix_rejected(data_dir: Path) -> None:
    body = _stage_body(data_dir, "stage-html.txt", "body")
    out = data_dir / "media" / "outbox" / "report.html"
    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 3
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "suffix" in err["error"]


def test_symlink_escape_from_stage_rejected(data_dir: Path, tmp_path: Path) -> None:
    """A symlink inside stage-dir pointing outside MUST be rejected.

    ``resolve(strict=True)`` walks the link, so the resolved path lands
    outside the stage root and the guard fires. Regressed once during
    the phase-2 path-guard work — keep a direct assertion here.
    """
    target = tmp_path / "outside-body.txt"
    target.write_text("not allowed", encoding="utf-8")
    link = data_dir / "run" / "render-stage" / "evil.txt"
    link.symlink_to(target)

    out = data_dir / "media" / "outbox" / "x.pdf"
    proc = _run(data_dir, "--body-file", str(link), "--out", str(out))

    assert proc.returncode == 3
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "must live under" in err["error"]


def test_empty_body_rejected(data_dir: Path) -> None:
    body = _stage_body(data_dir, "stage-empty.txt", "")
    out = data_dir / "media" / "outbox" / "empty.pdf"
    proc = _run(data_dir, "--body-file", str(body), "--out", str(out))

    assert proc.returncode == 2
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "empty" in err["error"].lower()


def test_out_filename_with_path_separator_rejected(data_dir: Path) -> None:
    body = _stage_body(data_dir, "stage-sep.txt", "body")
    # ``a/b.pdf`` would resolve inside outbox's parent, still rejected
    # because the filename portion carries a separator. Argparse passes
    # it through verbatim.
    proc = _run(
        data_dir,
        "--body-file",
        str(body),
        "--out",
        str(data_dir / "media" / "outbox" / ".." / "escaped.pdf"),
    )

    assert proc.returncode == 3
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "must live under" in err["error"] or "separator" in err["error"]


def test_body_exceeds_cap_rejected(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    body = _stage_body(data_dir, "stage-big.txt", "x" * 1024)
    out = data_dir / "media" / "outbox" / "big.pdf"
    # Shrink the cap via env override so we don't have to stage a 512 KB
    # file. The CLI honours ``RENDER_DOC_MAX_BODY_BYTES``.
    env = {
        "ASSISTANT_DATA_DIR": str(data_dir),
        "RENDER_DOC_MAX_BODY_BYTES": "256",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(data_dir),
    }
    proc = subprocess.run(
        [sys.executable, str(_CLI), "--body-file", str(body),
         "--out", str(out)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_PROJECT_ROOT),
        timeout=30,
    )
    assert proc.returncode == 3
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "exceeds cap" in err["error"]
