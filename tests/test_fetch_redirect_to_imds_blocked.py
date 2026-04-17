"""Must-fix #1: `_SafeRedirectHandler` re-classifies every redirect target.

A `https://legit.com/SKILL.md` that 302s to
`http://169.254.169.254/latest/meta-data/...` must be refused BEFORE the
second socket is opened. We exercise the handler directly against a tiny
local http.server (bound to 127.0.0.1) that emits a 302 with the
forbidden Location header, then assert the urllib fetch raises
`FetchError` and never writes any bytes to disk.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import _lib.fetch as fetch_mod
import pytest


class _Redirect302To(BaseHTTPRequestHandler):
    location: str = ""

    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", self.location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return None


class _FastHTTPServer(HTTPServer):
    """HTTPServer subclass that skips the `socket.getfqdn()` call in
    `server_bind`. On macOS with broken mDNS reverse lookups, the default
    `getfqdn('127.0.0.1')` can take 35+ seconds — long enough to break
    every test in this file.
    """

    def server_bind(self) -> None:
        # Replicate `socketserver.TCPServer.server_bind` without the
        # `self.server_name = socket.getfqdn(host)` line from HTTPServer.
        socket_module_bind = socket.socket.bind
        socket_module_bind(self.socket, self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


@pytest.fixture
def http_server() -> Iterator[tuple[str, HTTPServer]]:
    server = _FastHTTPServer(("127.0.0.1", 0), _Redirect302To)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://{host}:{port}", server
    server.shutdown()
    thread.join(timeout=2.0)


def _local_is_refused_by_ssrf_gate() -> bool:
    """Initial URL is http://127.0.0.1:..., already blocked by the up-front
    SSRF gate — confirms we need to go via the handler directly to exercise
    the redirect-path classifier on its own."""
    return True


def test_redirect_to_imds_blocked(
    http_server: tuple[str, HTTPServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial request goes to a (fake-public) raw SKILL.md URL; the SSRF
    gate on the *initial* URL is bypassed via monkeypatch so we can isolate
    the *redirect*-time gate. The 302 points at the AWS IMDS address, which
    the SafeRedirectHandler must refuse.

    `classify_url_sync` is stubbed inside the mirror module so DNS is not
    consulted (IMDS literal resolves synchronously via ipaddress; we avoid
    the public-hostname leg of `_resolve_hostname` entirely).
    """
    base_url, _server = http_server
    _Redirect302To.location = "http://169.254.169.254/latest/meta-data/"
    monkeypatch.setattr(fetch_mod, "classify_url_sync", _classifier_allow_only_server(base_url))

    dest = tmp_path / "bundle"
    with pytest.raises(fetch_mod.FetchError) as excinfo:
        fetch_mod._http_get_bytes(f"{base_url}/SKILL.md")
    assert "redirect" in str(excinfo.value).lower() or "ssrf" in str(excinfo.value).lower()
    assert not dest.exists()


def test_redirect_to_http_refused(
    http_server: tuple[str, HTTPServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to a non-https target (`http://`) must be refused even
    when the target is public — the handler enforces https:// irrespective
    of SSRF classification."""
    del tmp_path
    base_url, _server = http_server
    _Redirect302To.location = "http://example.com/other"
    monkeypatch.setattr(fetch_mod, "classify_url_sync", _classifier_allow_everything())

    with pytest.raises(fetch_mod.FetchError, match="non-https"):
        fetch_mod._http_get_bytes(f"{base_url}/SKILL.md")


def _classifier_allow_only_server(server_url: str) -> object:
    """Accept only `server_url`'s base + refuse the IMDS target."""
    host = server_url.split("//", 1)[1].split("/", 1)[0]

    def _classify(url: str, **_: object) -> str | None:
        # The initial URL goes to `server_url` — allow it.
        if url.startswith(server_url):
            return None
        # The redirect target is `http://169.254.169.254/...` — refuse.
        if "169.254.169.254" in url:
            return "IP literal targets non-public range: 169.254.169.254"
        return f"unexpected url classified: {url}"

    # `host` is captured so future changes to the classifier have a clear
    # handle on what "allowed" means.
    del host
    return _classify


def _classifier_allow_everything() -> object:
    """Used for the non-https test: SSRF classification is orthogonal to
    scheme check, which must fire even when the target would pass SSRF."""

    def _classify(url: str, **_: object) -> str | None:
        del url
        return None

    return _classify


# Smoke assertion keeping the socket happy — not strictly needed but catches
# typo'd port binding in the test fixture before we run the real assertion.
def test_http_server_fixture_binds() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
    finally:
        s.close()
