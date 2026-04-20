from __future__ import annotations

import socket
from unittest.mock import patch

from assistant.bridge.hooks import make_webfetch_hook


def _is_deny(resp: dict[str, object]) -> bool:
    out = resp.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


def _input(url: str) -> dict[str, object]:
    return {"tool_name": "WebFetch", "tool_input": {"url": url}}


async def test_literal_private_host_blocked() -> None:
    """Layer 1: literal string match against known private ranges."""
    hook = make_webfetch_hook()
    for url in [
        "http://localhost/foo",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
    ]:
        resp = await hook(_input(url), None, {})
        assert _is_deny(resp), url


async def test_public_hostname_resolving_private_ip_blocked() -> None:
    """Layer 2: hostname looks harmless but DNS resolves to private IP.

    Patches ``socket.getaddrinfo`` because ``make_webfetch_hook`` wraps it
    in ``asyncio.to_thread`` — the patch still applies since to_thread
    calls the patched symbol.
    """
    hook = make_webfetch_hook()
    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.42", 443))]

    with patch("socket.getaddrinfo", return_value=fake_dns):
        resp = await hook(_input("https://totally-innocent.example/"), None, {})
    assert _is_deny(resp)


async def test_public_hostname_resolving_public_ip_allowed() -> None:
    hook = make_webfetch_hook()
    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]

    with patch("socket.getaddrinfo", return_value=fake_dns):
        resp = await hook(_input("https://dns.google/"), None, {})
    assert not _is_deny(resp)


async def test_nxdomain_allows() -> None:
    """Layer 2 fail-open on DNS errors — CLI will fail the actual fetch."""
    hook = make_webfetch_hook()
    with patch("socket.getaddrinfo", side_effect=socket.gaierror("boom")):
        resp = await hook(_input("https://nxdomain.invalid/"), None, {})
    assert not _is_deny(resp)


async def test_oserror_allows() -> None:
    """B10: DNS OSError path is caught."""
    hook = make_webfetch_hook()
    with patch("socket.getaddrinfo", side_effect=OSError(1, "busy")):
        resp = await hook(_input("https://example.test/"), None, {})
    assert not _is_deny(resp)


async def test_timeout_allows() -> None:
    """B10: DNS timeout path is caught."""
    hook = make_webfetch_hook()
    with patch("socket.getaddrinfo", side_effect=TimeoutError("slow")):
        resp = await hook(_input("https://example.test/"), None, {})
    assert not _is_deny(resp)


async def test_malformed_url_denied() -> None:
    hook = make_webfetch_hook()
    resp = await hook(_input("http://[invalid"), None, {})
    assert _is_deny(resp)


async def test_empty_url_allows() -> None:
    """Empty URL = no-op; SDK wouldn't fire this anyway, but guard is cheap."""
    hook = make_webfetch_hook()
    resp = await hook(_input(""), None, {})
    assert not _is_deny(resp)


async def test_layer1_no_substring_false_positives() -> None:
    """SW1: hostnames that only CONTAIN a blocked substring (but are not
    the blocked host) must pass Layer 1. Layer 2 (DNS → public IP) then
    admits them. This closes the old ``10example.com`` / ``/v10.0/`` class
    of false-positive denials.
    """
    hook = make_webfetch_hook()
    fake_public = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]

    candidates = [
        "https://10example.com/",  # old Layer-1 prefix match false positive
        "https://1000guns.com/",
        "https://api.example.com/v10.0/foo",  # old `needle in raw` false positive
        "https://127example.com/",
        "https://localhostile.net/",  # not exactly ``localhost``
        "https://my-localhost-example.com/",  # hostname embeds the word
        "https://172-16-0-1.nip.io/",  # hostname looks private but resolves public
    ]
    with patch("socket.getaddrinfo", return_value=fake_public):
        for url in candidates:
            resp = await hook(_input(url), None, {})
            assert not _is_deny(resp), f"expected ALLOW, got deny for {url!r}"


async def test_layer1_exact_hostname_blocks() -> None:
    """SW1: ``localhost`` and subdomains of blocked hostnames still deny at
    Layer 1 (no DNS round-trip needed)."""
    hook = make_webfetch_hook()
    for url in (
        "http://localhost/",
        "http://foo.localhost/",  # subdomain match
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://something.metadata.google.internal/",
    ):
        resp = await hook(_input(url), None, {})
        assert _is_deny(resp), url


async def test_layer1_ip_literal_categories() -> None:
    """SW1: IP literal paths use ``ipaddress`` categories — private/
    loopback/link-local/reserved all deny at Layer 1."""
    hook = make_webfetch_hook()
    for url in (
        "http://127.0.0.1/",
        "http://127.1.2.3/",  # whole 127/8 is loopback
        "http://10.0.0.1/",  # private
        "http://192.168.1.1/",  # private
        "http://172.16.0.1/",  # private (within 172.16.0.0/12)
        "http://169.254.169.254/",  # link-local metadata
        "http://0.0.0.0/",  # unspecified
        "http://[::1]/",  # IPv6 loopback
        "http://[fc00::1]/",  # IPv6 ULA (private)
        "http://[fe80::1]/",  # IPv6 link-local
    ):
        resp = await hook(_input(url), None, {})
        assert _is_deny(resp), url


async def test_layer1_public_ip_literal_allows() -> None:
    """SW1: public IP literal passes Layer 1 cleanly. Layer 2 then
    ``getaddrinfo``s the literal (which trivially returns the same IP)
    and the ``ipaddress`` category check admits it. End-to-end: allow.
    """
    hook = make_webfetch_hook()
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
    with patch("socket.getaddrinfo", return_value=fake):
        resp = await hook(_input("http://8.8.8.8/"), None, {})
    assert not _is_deny(resp)


async def test_layer2_still_blocks_public_hostname_private_ip() -> None:
    """SW1 regression: Layer 2 still catches public hostnames that
    resolve to private IPs. Drop no security with the Layer-1 rewrite."""
    hook = make_webfetch_hook()
    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 443))]
    with patch("socket.getaddrinfo", return_value=fake_dns):
        resp = await hook(_input("https://innocuous.example/"), None, {})
    assert _is_deny(resp)
