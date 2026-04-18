"""Phase 7 / commit 8 — tools/genimage/main.py CLI coverage.

Covers the four S-5 spike scenarios (R-1 midnight rollover, R-2 same-day
cap, R-3 10-worker flock contention, R-4 clock-rollback jitter) plus
argv / path / SSRF / network error paths.

The CLI is stdlib-only and must stay importable without side effects.
Quota-logic tests import `_check_and_increment_quota` directly (faster
and deterministic). End-to-end shape tests invoke via `subprocess.run`
against a mock HTTP server bound to `127.0.0.1`.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CLI = _ROOT / "tools" / "genimage" / "main.py"

# Import the CLI module so unit-level tests can call the helpers
# directly without spawning a subprocess. sys.path is mutated BEFORE
# the import; the E402 suppression keeps lint quiet about the order.
sys.path.insert(0, str(_ROOT))

from tools.genimage.main import _check_and_increment_quota  # noqa: E402

# ---------------------------------------------------------------- helpers


@contextmanager
def _mock_mflux_server(
    *, png_bytes: bytes | None = None, status: int = 200, content_type: str = "image/png"
) -> Iterator[str]:
    """Spin a loopback HTTPServer that returns a fixed PNG or error."""

    png = png_bytes if png_bytes is not None else b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(png)))
            self.send_header("X-Image-Seed", "42")
            self.send_header("X-Image-Width", "1024")
            self.send_header("X-Image-Height", "1024")
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, format: str, *args: object) -> None:
            return  # silence stderr in tests

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/generate"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _run_cli(
    tmp_path: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ASSISTANT_DATA_DIR": str(tmp_path),
        "MEDIA_OUTBOX_DIR": str(tmp_path / "media" / "outbox"),
        "MEDIA_GENIMAGE_QUOTA_FILE": str(tmp_path / "run" / "genimage-quota.json"),
    }
    (tmp_path / "media" / "outbox").mkdir(parents=True, exist_ok=True)
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
    )


# ---------------------------------------------------------------- S-5 R-1


def test_quota_cross_midnight_both_allowed(tmp_path: Path) -> None:
    """R-1: same file, count wraps on date change — both requests succeed."""
    qf = tmp_path / "q.json"
    allowed_d1, state_d1 = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
    allowed_d2, state_d2 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
    assert allowed_d1 is True and state_d1["count"] == 1
    assert allowed_d2 is True and state_d2["count"] == 1
    persisted = json.loads(qf.read_text())
    assert persisted == {"date": "2026-04-18", "count": 1}


# ---------------------------------------------------------------- S-5 R-2


def test_quota_same_day_cap_denies_second(tmp_path: Path) -> None:
    """R-2: cap=1 — second call same day must deny without incrementing."""
    qf = tmp_path / "q.json"
    allowed1, state1 = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
    allowed2, state2 = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
    assert allowed1 is True and state1["count"] == 1
    assert allowed2 is False and state2["count"] == 1  # unchanged on deny


# ---------------------------------------------------------------- S-5 R-3


def test_quota_concurrent_flock_exactly_one_winner(tmp_path: Path) -> None:
    """R-3: 10 threads race on the same file; cap=1 → exactly one wins."""
    qf = tmp_path / "q.json"
    n = 10
    results: list[bool] = []
    lock = threading.Lock()
    start = threading.Barrier(n)

    def worker() -> None:
        start.wait()
        ok, _ = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for x in results if x) == 1
    persisted = json.loads(qf.read_text())
    assert persisted == {"date": "2026-04-17", "count": 1}


# ---------------------------------------------------------------- S-5 R-4


def test_quota_clock_rollback_is_tolerated(tmp_path: Path) -> None:
    """R-4: clock jitters backward across midnight — algo stays sane.

    Documented known jitter: a rollback resets the counter to day 1
    because the file's `date` mismatches. Both requests end up allowed;
    final state reflects day 1 with count=1. Accepted per pitfall #7.
    """
    qf = tmp_path / "q.json"
    a1, s1 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
    a2, s2 = _check_and_increment_quota(qf, cap=1, today="2026-04-17")  # back in time
    assert a1 is True and s1["count"] == 1
    assert a2 is True and s2["count"] == 1
    assert json.loads(qf.read_text()) == {"date": "2026-04-17", "count": 1}


# ---------------------------------------------------------------- argv


def test_cli_help_exits_zero() -> None:
    r = subprocess.run(
        [sys.executable, str(_CLI), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert r.returncode == 0, r.stderr
    assert "genimage" in r.stdout.lower()


def test_cli_missing_required_exits_2(tmp_path: Path) -> None:
    r = _run_cli(tmp_path)
    assert r.returncode == 2


def test_cli_prompt_with_newline_exits_2(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "a.png"
    r = _run_cli(tmp_path, "--prompt", "bad\nprompt", "--out", str(out))
    assert r.returncode == 2
    assert "newline" in r.stderr.lower()


# ---------------------------------------------------------------- path guard


def test_cli_out_outside_outbox_exits_3(tmp_path: Path) -> None:
    bad = tmp_path / "elsewhere" / "x.png"
    bad.parent.mkdir()
    r = _run_cli(tmp_path, "--prompt", "p", "--out", str(bad))
    assert r.returncode == 3
    assert "outbox" in r.stderr.lower()


def test_cli_out_not_png_exits_3(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "a.jpg"
    r = _run_cli(tmp_path, "--prompt", "p", "--out", str(out))
    assert r.returncode == 3


def test_cli_endpoint_non_loopback_exits_3(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "a.png"
    r = _run_cli(
        tmp_path,
        "--prompt",
        "p",
        "--out",
        str(out),
        "--endpoint",
        "http://169.254.169.254/",
    )
    assert r.returncode == 3
    assert "loopback" in r.stderr.lower()


# ---------------------------------------------------------------- happy path


def test_cli_happy_path_writes_png_and_bumps_quota(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "happy.png"
    with _mock_mflux_server() as endpoint:
        r = _run_cli(
            tmp_path,
            "--prompt",
            "a minimalist sunset",
            "--out",
            str(out),
            "--endpoint",
            endpoint,
            "--daily-cap",
            "3",
        )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["path"] == str(out)
    assert out.is_file() and out.stat().st_size > 0
    quota = json.loads((tmp_path / "run" / "genimage-quota.json").read_text())
    assert quota["count"] == 1
    assert quota["date"] == datetime.now(UTC).strftime("%Y-%m-%d")


def test_cli_quota_exhausted_exits_6(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "q.png"
    # Pre-fill quota at cap=1 for today.
    qf = tmp_path / "run" / "genimage-quota.json"
    qf.parent.mkdir(parents=True, exist_ok=True)
    qf.write_text(
        json.dumps({"date": datetime.now(UTC).strftime("%Y-%m-%d"), "count": 1})
    )
    with _mock_mflux_server() as endpoint:
        r = _run_cli(
            tmp_path,
            "--prompt",
            "p",
            "--out",
            str(out),
            "--endpoint",
            endpoint,
            "--daily-cap",
            "1",
        )
    assert r.returncode == 6
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert "quota" in payload["reason"].lower()
    assert not out.exists()


def test_cli_network_error_exits_4(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "net.png"
    # Bind-grab a free port, then release — guaranteed to be unreachable
    # for the duration of the test (within the usual port-reuse race).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    r = _run_cli(
        tmp_path,
        "--prompt",
        "p",
        "--out",
        str(out),
        "--endpoint",
        f"http://127.0.0.1:{port}/generate",
        "--daily-cap",
        "3",
        "--timeout-s",
        "30",
    )
    assert r.returncode == 4
    assert not out.exists()


def test_cli_server_500_exits_4(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "err.png"
    with _mock_mflux_server(status=500) as endpoint:
        r = _run_cli(
            tmp_path,
            "--prompt",
            "p",
            "--out",
            str(out),
            "--endpoint",
            endpoint,
            "--daily-cap",
            "3",
        )
    assert r.returncode == 4
    assert not out.exists()


def test_cli_refuses_to_overwrite_existing_out(tmp_path: Path) -> None:
    out = tmp_path / "media" / "outbox" / "exists.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"pre-existing")
    with _mock_mflux_server() as endpoint:
        r = _run_cli(
            tmp_path,
            "--prompt",
            "p",
            "--out",
            str(out),
            "--endpoint",
            endpoint,
            "--daily-cap",
            "3",
        )
    assert r.returncode == 3
    assert out.read_bytes() == b"pre-existing"


# ---------------------------------------------------------------- skill.md


def test_skill_md_contains_colon_space_rule() -> None:
    """H-13: SKILL.md must document the 'space after `:` before path' rule."""
    skill = _ROOT / "tools" / "genimage" / "SKILL.md"
    body = skill.read_text(encoding="utf-8")
    assert "space after" in body.lower() or "пробел после" in body.lower()
    # Good/bad example must both be present so the model has a contrast.
    assert "/abs/outbox/" in body
    assert "готово: /" in body  # good form
    assert "готово:/" in body  # bad counter-example


def test_skill_md_has_frontmatter() -> None:
    skill = _ROOT / "tools" / "genimage" / "SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    assert any(line.startswith("name: genimage") for line in lines[:10])


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-v"])
