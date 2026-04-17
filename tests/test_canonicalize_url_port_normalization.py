"""Review fix #12: `_canonicalize_url` collapses default-port URLs.

`https://github.com:443/x/y` and `https://github.com/x/y` must end up
with the same cache key. Non-default ports stay; the scheme decides
which number is "default".
"""

from __future__ import annotations

import importlib

installer_main = importlib.import_module("main")
_canonicalize_url = installer_main._canonicalize_url


def test_https_default_port_collapses() -> None:
    assert _canonicalize_url("https://github.com:443/x/y") == _canonicalize_url(
        "https://github.com/x/y"
    )


def test_http_default_port_collapses() -> None:
    assert _canonicalize_url("http://example.com:80/x/y") == _canonicalize_url(
        "http://example.com/x/y"
    )


def test_non_default_port_preserved() -> None:
    assert _canonicalize_url("https://gh.example.com:8443/x/y") != _canonicalize_url(
        "https://gh.example.com/x/y"
    )
    assert ":8443" in _canonicalize_url("https://gh.example.com:8443/x/y")


def test_https_with_http_port_preserved() -> None:
    """Port 80 on an `https` URL is NOT the default for that scheme;
    keep it so we don't conflate with plain https traffic."""
    assert ":80" in _canonicalize_url("https://example.com:80/x")


def test_case_folding_still_holds_with_port() -> None:
    assert _canonicalize_url("HTTPS://GitHub.COM:443/a/b") == _canonicalize_url(
        "https://github.com/a/b"
    )


def test_www_strip_still_holds_with_port() -> None:
    assert _canonicalize_url("https://www.github.com:443/a/b") == _canonicalize_url(
        "https://github.com/a/b"
    )
