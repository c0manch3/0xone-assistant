"""Phase 7 / commit 11 — Bash hook allowlist for the render_doc CLI.

Covers:
  * hard deny when `data_dir` is not bound to the hook
    ("render-doc requires data_dir-bound hooks");
  * `--body-file` MUST live under ``<data_dir>/run/render-stage/``;
  * `--out` MUST live under ``<data_dir>/media/outbox/`` AND end in
    ``.pdf`` or ``.docx``;
  * optional `--title` / `--font` length caps;
  * dup-flag deny, unknown-flag deny, metachar deny.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.bridge.hooks import check_bash_command


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "tools" / "render_doc").mkdir(parents=True)
    (tmp_path / "tools" / "render_doc" / "main.py").write_text("# stub\n")
    return tmp_path


@pytest.fixture
def data_dir() -> Path:
    # Short base under /tmp/ — see test_bash_hook_genimage_allowlist.py for
    # the slip-guard rationale. Embedding pytest's tmp_path (long
    # /private/var/folders/... prefix) into a bash command trips
    # `_BASH_SLIP_GUARD_RE` because the long alphanumeric run matches the
    # base64-like pattern.
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="p7r-", dir="/tmp"))
    d = base / "d"
    (d / "run" / "render-stage").mkdir(parents=True)
    (d / "media" / "outbox").mkdir(parents=True)
    return d


def _render_cmd(data_dir: Path, *, body: str | None = None, out: str | None = None) -> str:
    body_file = body or str(data_dir / "run" / "render-stage" / "body.txt")
    out_file = out or str(data_dir / "media" / "outbox" / "doc.pdf")
    return f"python tools/render_doc/main.py --body-file {body_file} --out {out_file}"


# ---------------------------------------------------------------- HARD DENY: no data_dir


def test_deny_when_no_data_dir(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir)
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "render-doc requires data_dir-bound hooks" in reason


def test_deny_when_no_data_dir_even_with_optional_flags(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir) + ' --title "Report"'
    reason = check_bash_command(cmd, project_root)
    assert reason is not None
    assert "render-doc requires data_dir-bound hooks" in reason


# ---------------------------------------------------------------- ALLOW: bound data_dir


def test_allow_pdf_under_outbox(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir)
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is None, reason


def test_allow_docx_under_outbox(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir, out=str(data_dir / "media" / "outbox" / "report.docx"))
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is None, reason


def test_allow_with_title_and_font(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir) + ' --title "Annual Report" --font DejaVu'
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is None, reason


# ---------------------------------------------------------------- DENY: bound data_dir


def test_deny_body_file_outside_stage(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir, body="/tmp/body.txt")
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "render-stage" in reason


def test_deny_out_outside_outbox(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir, out="/tmp/out.pdf")
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "outbox" in reason


def test_deny_out_wrong_suffix(project_root: Path, data_dir: Path) -> None:
    bad_out = str(data_dir / "media" / "outbox" / "doc.html")
    cmd = _render_cmd(data_dir, out=bad_out)
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert ".pdf" in reason or ".docx" in reason


def test_deny_missing_body_file(project_root: Path, data_dir: Path) -> None:
    out = data_dir / "media" / "outbox" / "doc.pdf"
    cmd = f"python tools/render_doc/main.py --out {out}"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "--body-file" in reason


def test_deny_missing_out(project_root: Path, data_dir: Path) -> None:
    body = data_dir / "run" / "render-stage" / "body.txt"
    cmd = f"python tools/render_doc/main.py --body-file {body}"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "--out" in reason


def test_deny_duplicate_body_file(project_root: Path, data_dir: Path) -> None:
    body = str(data_dir / "run" / "render-stage" / "body.txt")
    out = str(data_dir / "media" / "outbox" / "doc.pdf")
    cmd = f"python tools/render_doc/main.py --body-file {body} --body-file {body} --out {out}"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "duplicate" in reason


def test_deny_unknown_flag(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir) + " --password secret"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None


def test_deny_relative_body_file(project_root: Path, data_dir: Path) -> None:
    out = data_dir / "media" / "outbox" / "doc.pdf"
    cmd = f"python tools/render_doc/main.py --body-file body.txt --out {out}"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "absolute" in reason


def test_deny_dotdot_in_out(project_root: Path, data_dir: Path) -> None:
    body = data_dir / "run" / "render-stage" / "body.txt"
    # Use a lexical ".." to bypass naive string checks — the validator
    # rejects on `.parts` containing "..".
    bad_out = f"{data_dir}/media/outbox/../../../etc/x.pdf"
    cmd = f"python tools/render_doc/main.py --body-file {body} --out {bad_out}"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert ".." in reason


def test_deny_title_too_long(project_root: Path, data_dir: Path) -> None:
    long_title = "x" * 512
    cmd = _render_cmd(data_dir) + f' --title "{long_title}"'
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
    assert "--title" in reason


def test_deny_shell_metachar(project_root: Path, data_dir: Path) -> None:
    cmd = _render_cmd(data_dir) + " | tee /tmp/leak"
    reason = check_bash_command(cmd, project_root, data_dir=data_dir)
    assert reason is not None
