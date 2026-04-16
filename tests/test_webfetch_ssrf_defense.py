"""SSRF defence test for the WebFetch hook.

Each case is fed through `assistant.bridge.hooks.classify_url` (the public
seam used by `make_webfetch_hook`). Hostname → IP resolution is monkey-patched
so the test does not perform real DNS.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable
from typing import Any

import pytest

import assistant.bridge.hooks as hooks_module
from assistant.bridge.hooks import classify_url


def _fake_getaddrinfo(public_ips: Iterable[str]) -> Any:
    fixed = list(public_ips)

    async def _impl(host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        del host, args, kwargs
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                (ip, 0),
            )
            for ip in fixed
        ]

    return _impl


# ---------------------------------------------------------------- DENY: IP literals

DENY_IP_LITERALS = [
    "http://localhost/",
    "http://127.0.0.1/",
    "http://127.1.2.3/",
    "http://0.0.0.0/",
    "http://10.0.0.1/",
    "http://192.168.1.1/",
    "http://172.20.5.5/",
    "http://169.254.169.254/latest/meta-data/",  # AWS IMDS
    "http://[::1]/",
    "http://[fc00::1]/",
    "http://[fe80::1]/",
]


@pytest.mark.parametrize("url", DENY_IP_LITERALS)
async def test_ssrf_deny_ip_literals(url: str) -> None:
    reason = await classify_url(url)
    assert reason is not None, f"expected DENY: {url!r}"


# ---------------------------------------------------------------- DENY: scheme / shape

DENY_SCHEMES = [
    "file:///etc/passwd",
    "ftp://example.com/file",
    "gopher://example.com:70/",
    "ldap://example.com/",
]


@pytest.mark.parametrize("url", DENY_SCHEMES)
async def test_ssrf_deny_scheme(url: str) -> None:
    reason = await classify_url(url)
    assert reason is not None
    assert "scheme" in reason or "hostname" in reason or "malformed" in reason


# ---------------------------------------------------------------- DENY: hostname → private IP


async def test_ssrf_deny_hostname_resolves_private(monkeypatch: pytest.MonkeyPatch) -> None:
    # `localhost.attacker.com` would have slipped past the substring check
    # in the v1 hook. With DNS classification, only the resolved IP matters.
    monkeypatch.setattr(hooks_module, "_resolve_hostname", _resolver_returning(["10.0.0.5"]))
    reason = await classify_url("https://localhost.attacker.com/path")
    assert reason is not None
    assert "10.0.0.5" in reason or "non-public" in reason


async def test_ssrf_deny_hostname_resolves_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks_module, "_resolve_hostname", _resolver_returning(["127.0.0.1"]))
    reason = await classify_url("https://example.com/")
    assert reason is not None
    assert "non-public" in reason or "127.0.0.1" in reason


# ---------------------------------------------------------------- ALLOW: public IPs


async def test_ssrf_allow_public_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    # `socket.AF_INET6` for an IPv6 public IP, plus an IPv4 — both should
    # classify as public and the URL should be allowed.
    monkeypatch.setattr(
        hooks_module,
        "_resolve_hostname",
        _resolver_returning(["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]),
    )
    reason = await classify_url("https://example.com/")
    assert reason is None, f"expected ALLOW, got: {reason}"


async def test_ssrf_allow_public_ip_literal() -> None:
    reason = await classify_url("https://8.8.8.8/")
    assert reason is None


# ---------------------------------------------------------------- DENY: DNS errors


async def test_ssrf_deny_on_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _failing(host: str, *, deadline_s: float) -> list[Any]:
        del host, deadline_s
        raise socket.gaierror("simulated DNS failure")

    monkeypatch.setattr(hooks_module, "_resolve_hostname", _failing)
    reason = await classify_url("https://does-not-exist.invalid/")
    assert reason is not None
    assert "resolve" in reason


async def test_ssrf_deny_on_dns_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _timing_out(host: str, *, deadline_s: float) -> list[Any]:
        del host, deadline_s
        raise TimeoutError

    monkeypatch.setattr(hooks_module, "_resolve_hostname", _timing_out)
    reason = await classify_url("https://slow-dns.example.com/")
    assert reason is not None
    assert "timed out" in reason or "timeout" in reason.lower()


# ---------------------------------------------------------------- helpers


def _resolver_returning(ip_strings: Iterable[str]) -> Any:
    """Build an async resolver that returns the supplied IP strings."""
    import ipaddress

    async def _impl(host: str, *, deadline_s: float) -> list[Any]:
        del host, deadline_s
        return [ipaddress.ip_address(ip) for ip in ip_strings]

    return _impl
