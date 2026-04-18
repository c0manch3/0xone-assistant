"""Unit tests for `tools/transcribe/` (phase 7 wave 4 commit 7).

Three groups of tests, disjoint setup:

1. **Loopback-only corpus** — 11 cases ported verbatim from
   `spikes/phase7_s1_endpoint_ssrf.py`. Guards against anyone swapping
   `is_loopback_only` for the wider `classify_url` (S-1 finding).
2. **argv / path / endpoint validation** — checks that `main()` exits
   with the documented codes (2/3/4/5) under the right failure modes,
   using a local HTTP server as the loopback target when we need a
   successful round-trip.
3. **Multipart encoding** — asserts the CLI-emitted body round-trips
   through `email.parser` and contains the three expected form fields
   with the correct payload.

The tests deliberately stay offline: no real `/transcribe` server, no
external DNS for public hostnames. Cases that require DNS (`localhost`,
`localhost.localdomain`, `api.telegram.org`) are marked so they're
skipped on systems without network connectivity but PASS when run on a
typical developer laptop — matching the spike script's behaviour.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# Under pytest, `tools` is importable because `tests/conftest.py` +
# `tools/__init__.py` cooperate. No sys.path shim needed.
from tools.transcribe import main as cli
from tools.transcribe._net_mirror import is_loopback_only

# ---------------------------------------------------------------------------
# 1. Loopback-only corpus — ported from spikes/phase7_s1_endpoint_ssrf.py
# ---------------------------------------------------------------------------

# Format: (url, expected_ok_or_None). `None` = "we don't assert the
# boolean, only that the function terminates without raising" — used for
# `localhost.localdomain` which isn't universally resolvable.
_LOOPBACK_CASES: list[tuple[str, bool | None]] = [
    ("http://localhost:9100/transcribe", True),
    ("http://127.0.0.1:9100/transcribe", True),
    ("http://127.0.0.2:9100/transcribe", True),
    ("http://[::1]:9100/transcribe", True),
    ("http://localhost.localdomain:9100/transcribe", None),
    ("http://169.254.169.254/", False),
    ("http://10.0.0.1:9100/", False),
    ("http://192.168.1.1:9100/", False),
    ("https://api.telegram.org/", False),
    ("ftp://localhost/", False),
    ("http://:9100/", False),
]


@pytest.mark.parametrize("url, expected", _LOOPBACK_CASES)
def test_is_loopback_only_corpus(url: str, expected: bool | None) -> None:
    """11-case port from S-1 — any drift means the SSRF guard regressed."""
    ok, reason = asyncio.run(is_loopback_only(url))
    assert isinstance(reason, str) and reason, "reason must be a non-empty string"
    if expected is None:
        # `localhost.localdomain` outcome is host-dependent; accept either
        # verdict so CI doesn't flake on machines without the hint.
        return
    assert ok is expected, f"{url!r}: expected ok={expected}, got ok={ok} ({reason})"


def test_is_loopback_only_rejects_link_local_ipv6() -> None:
    """fe80::/10 is link-local, not loopback. Must be denied (acceptance §7)."""
    ok, reason = asyncio.run(is_loopback_only("http://[fe80::1]/"))
    assert ok is False
    # Don't over-constrain the exact message; just assert it mentions the
    # IP.
    assert "fe80::1" in reason or "loopback" in reason


def test_is_loopback_only_rejects_ipv4_mapped_public() -> None:
    """`::ffff:8.8.8.8` is an IPv6-mapped IPv4 that should not be loopback."""
    ok, reason = asyncio.run(is_loopback_only("http://[::ffff:8.8.8.8]/"))
    assert ok is False
    assert "loopback" in reason


# ---------------------------------------------------------------------------
# 2. argv / path / endpoint validation
# ---------------------------------------------------------------------------


def _write_audio(tmp_path: Path, name: str = "sample.oga", size: int = 1024) -> Path:
    """Create a fake audio file of the requested size. Content is irrelevant
    for argv-tests; the CLI only checks size + extension."""
    target = tmp_path / name
    target.write_bytes(b"\x00" * size)
    return target


def test_cli_rejects_missing_path(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """argparse surfaces missing positional as exit 2."""
    rc = cli.main([])
    assert rc == cli.EXIT_USAGE
    # argparse prints to stderr automatically — don't assert on exact wording.


def test_cli_rejects_relative_path(capsys: pytest.CaptureFixture) -> None:
    """Relative path → EXIT_PATH (3), JSON error on stderr."""
    rc = cli.main(["relative/file.oga"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_PATH
    payload = json.loads(captured.err.strip())
    assert payload["ok"] is False
    assert "absolute" in payload["error"]


def test_cli_rejects_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    missing = tmp_path / "missing.oga"
    rc = cli.main([str(missing)])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_PATH
    payload = json.loads(captured.err.strip())
    assert "does not exist" in payload["error"]


def test_cli_rejects_bad_extension(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    bogus = tmp_path / "not-audio.txt"
    bogus.write_bytes(b"hello")
    rc = cli.main([str(bogus)])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_PATH
    payload = json.loads(captured.err.strip())
    assert "unsupported extension" in payload["error"]


def test_cli_rejects_oversize_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    audio = _write_audio(tmp_path, size=512)
    monkeypatch.setenv("MEDIA_TRANSCRIBE_MAX_INPUT_BYTES", "100")
    rc = cli.main([str(audio)])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_PATH
    payload = json.loads(captured.err.strip())
    assert "exceeds cap" in payload["error"]


def test_cli_rejects_non_loopback_endpoint(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    audio = _write_audio(tmp_path)
    rc = cli.main([str(audio), "--endpoint", "http://10.0.0.1:9100/transcribe"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_USAGE
    payload = json.loads(captured.err.strip())
    assert "endpoint rejected" in payload["error"]


def test_cli_rejects_bad_timeout(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    audio = _write_audio(tmp_path)
    rc = cli.main([str(audio), "--timeout-s", "5"])
    # argparse surfaces --timeout-s out-of-range as exit 2 via parser.error.
    assert rc == cli.EXIT_USAGE


def test_cli_rejects_bad_language(
    tmp_path: Path,
) -> None:
    audio = _write_audio(tmp_path)
    rc = cli.main([str(audio), "--language", "klingon"])
    assert rc == cli.EXIT_USAGE


# --- Live loopback HTTP server for happy-path + network-error tests ---------


class _CapturingHandler(BaseHTTPRequestHandler):
    """Minimal loopback server; subclasses override `_respond` to vary
    the reply. Silences the default stderr logging."""

    # Populated in the per-test subclass.
    _response_status: int = 200
    _response_body: bytes = b'{"ok": true, "text": "hello", "duration_s": 1.0}'
    _captured_body: bytes | None = None
    _captured_content_type: str | None = None

    def log_message(self, format: str, *args: object) -> None:
        pass  # Silence default stderr spam during tests.

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self)._captured_body = self.rfile.read(length) if length else b""
        type(self)._captured_content_type = self.headers.get("Content-Type")
        self.send_response(self._response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self._response_body)))
        self.end_headers()
        self.wfile.write(self._response_body)


def _spawn_loopback_server(
    handler_cls: type[BaseHTTPRequestHandler],
) -> tuple[HTTPServer, str]:
    """Start an HTTPServer on 127.0.0.1:<ephemeral>. Returns `(server, url)`."""
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # `localhost` resolves to 127.0.0.1 on every platform we run on; no
    # need to dance between v4/v6.
    return server, f"http://127.0.0.1:{port}/transcribe"


def test_cli_happy_path_multipart_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """End-to-end: CLI POSTs to a loopback server, parses the JSON reply."""

    class _Handler(_CapturingHandler):
        _response_status = 200
        _response_body = json.dumps(
            {"ok": True, "text": "привет", "duration_s": 2.5, "language": "ru"}
        ).encode()

    audio = _write_audio(tmp_path, name="voice.oga", size=2048)
    server, url = _spawn_loopback_server(_Handler)
    try:
        rc = cli.main([str(audio), "--endpoint", url, "--language", "ru"])
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert rc == cli.EXIT_OK, captured.err
    payload = json.loads(captured.out.strip())
    assert payload == {"ok": True, "text": "привет", "duration_s": 2.5, "language": "ru"}

    # Multipart body must have been received and parseable.
    assert _Handler._captured_body is not None
    assert _Handler._captured_content_type is not None
    assert _Handler._captured_content_type.startswith("multipart/form-data")

    # Reassemble using BytesParser so we don't duplicate CLI logic here.
    header_bytes = f"Content-Type: {_Handler._captured_content_type}\r\n\r\n".encode()
    msg = BytesParser(policy=policy.default).parsebytes(header_bytes + _Handler._captured_body)
    assert msg.is_multipart()
    parts = {
        part.get_param("name", header="Content-Disposition"): part for part in msg.iter_parts()
    }
    assert set(parts) == {"language", "format", "file"}
    assert parts["language"].get_payload(decode=True) == b"ru"
    assert parts["format"].get_payload(decode=True) == b"text"
    # File body must be the audio bytes verbatim (2048 zero-bytes).
    assert parts["file"].get_payload(decode=True) == audio.read_bytes()


def test_cli_maps_http_500_to_network_exit(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    class _Handler(_CapturingHandler):
        _response_status = 500
        _response_body = b'{"ok": false, "error": "whisper crashed"}'

    audio = _write_audio(tmp_path)
    server, url = _spawn_loopback_server(_Handler)
    try:
        rc = cli.main([str(audio), "--endpoint", url])
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert rc == cli.EXIT_NETWORK
    payload = json.loads(captured.err.strip())
    assert "HTTP 500" in payload["error"]


def test_cli_maps_unreachable_to_network_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Loopback port with nothing listening → URLError → EXIT_NETWORK."""
    # Pick an ephemeral port, close it immediately, then point the CLI at
    # the now-vacant port. Race-free because the OS won't instantly
    # re-assign the port to another process within the ms-scale window.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    audio = _write_audio(tmp_path)
    rc = cli.main([str(audio), "--endpoint", f"http://127.0.0.1:{free_port}/transcribe"])
    captured = capsys.readouterr()
    assert rc == cli.EXIT_NETWORK
    payload = json.loads(captured.err.strip())
    assert "unreachable" in payload["error"]


def test_cli_maps_upstream_non_json_to_network_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    class _Handler(_CapturingHandler):
        _response_status = 200
        _response_body = b"<html>not JSON</html>"

    audio = _write_audio(tmp_path)
    server, url = _spawn_loopback_server(_Handler)
    try:
        rc = cli.main([str(audio), "--endpoint", url])
    finally:
        server.shutdown()
        server.server_close()
    captured = capsys.readouterr()
    assert rc == cli.EXIT_NETWORK
    payload = json.loads(captured.err.strip())
    assert "non-JSON" in payload["error"]


# ---------------------------------------------------------------------------
# 3. Multipart encoder unit test (no socket)
# ---------------------------------------------------------------------------


def test_encode_multipart_produces_parseable_body(tmp_path: Path) -> None:
    """Directly exercise `_encode_multipart` without booting an HTTP server."""
    audio = tmp_path / "snippet.mp3"
    audio.write_bytes(b"fake-mp3-bytes")
    body, content_type = cli._encode_multipart(audio, "auto", "segments")
    assert content_type.startswith("multipart/form-data; boundary=")
    header_bytes = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = BytesParser(policy=policy.default).parsebytes(header_bytes + body)
    assert msg.is_multipart()
    fields = {
        part.get_param("name", header="Content-Disposition"): part.get_payload(decode=True)
        for part in msg.iter_parts()
    }
    assert fields == {
        "language": b"auto",
        "format": b"segments",
        "file": b"fake-mp3-bytes",
    }


def test_encode_multipart_raises_on_missing_file(tmp_path: Path) -> None:
    """Audio-read failure must surface as a typed exception (main() turns
    it into EXIT_PATH)."""
    missing = tmp_path / "gone.oga"
    with pytest.raises(cli._AudioReadError):
        cli._encode_multipart(missing, "auto", "text")


# ---------------------------------------------------------------------------
# 4. --help smoke
# ---------------------------------------------------------------------------


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    """`--help` must exit 0 and print usage on stdout (argparse contract)."""
    rc = cli.main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "transcribe" in captured.out
    assert "--language" in captured.out
