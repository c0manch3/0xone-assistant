"""SSRF helper mirror for tools/genimage — stdlib-only, loopback-only gate.

Phase-7 pitfall #5 (S-1 spike): the CLI MUST NOT delegate to
`assistant.bridge.net.classify_url` because `classify_url` permits ALL
non-public addresses, including `10.x` / `192.168.x` / link-local. The
phase-7 architecture reaches the Mac host via an SSH reverse tunnel on
`127.0.0.1:<port>` only — anything else is a misconfiguration or an
SSRF attempt.

This helper is strictly narrower: require every resolved address of the
URL's hostname to be loopback. IPv6 link-local (`fe80::/10`) is
explicitly rejected.

The module is named `_net_mirror` for symmetry with
`tools/skill_installer/_lib/_net_mirror.py`, but the logic is
deliberately distinct — a byte-identical mirror would defeat the
"loopback-only" purpose. See `spikes/phase7_s1_endpoint_ssrf.py` for
the 11-case corpus this implementation passes.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_loopback_only(url: str, *, dns_timeout_s: float = 3.0) -> tuple[bool, str]:
    """Return ``(True, reason)`` iff every resolution target is loopback.

    Synchronous wrapper — the genimage CLI is stdlib-only and must not
    pull in asyncio just for a single DNS lookup.

    Rules (in order):
      1. URL must be well-formed; scheme must be http or https.
      2. Hostname must be present.
      3. If the hostname is an IP literal, it must satisfy ``is_loopback``
         (IPv4 127.0.0.0/8, IPv6 ::1). Link-local (IPv4 169.254/16 or
         IPv6 fe80::/10), private (10/8, 172.16/12, 192.168/16), and
         every other range are rejected.
      4. Otherwise the hostname is resolved via ``socket.getaddrinfo``
         and EVERY returned address must be loopback. A single
         non-loopback resolution causes rejection (protects against
         DNS hijacking that resolves ``localhost.example`` to a public
         IP).

    Returns a ``(allowed, reason)`` pair so callers can log the exact
    deny reason without re-inspecting the URL.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "malformed URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False, f"scheme must be http(s), got {scheme!r}"
    host = (parsed.hostname or "").strip()
    if not host:
        return False, "URL has no hostname"

    # Direct IP literal (IPv4 or IPv6). `urlparse` strips the surrounding
    # `[...]` from IPv6 hosts automatically; `ipaddress.ip_address`
    # accepts both bare forms.
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        if addr.is_loopback:
            return True, f"ip literal {addr} is loopback"
        return False, f"ip literal {addr} is not loopback"

    # DNS resolution. `getaddrinfo` uses the configured system timeout
    # by default; we set a socket-level default to bound the call. We
    # restore the prior default in a finally block so the mutation is
    # localized — important because stdlib-only CLIs are occasionally
    # re-used inside tests that depend on the default socket timeout.
    prev_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(dns_timeout_s)
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"DNS resolve failed for {host!r}: {exc}"
    except OSError as exc:
        return False, f"DNS lookup for {host!r} errored: {exc}"
    finally:
        socket.setdefaulttimeout(prev_timeout)

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _stype, _proto, _canon, sockaddr in infos:
        # sockaddr[0] is str for AF_INET/AF_INET6 per the stdlib docs;
        # the type stub widens it to `str | int` because of AF_BLUETOOTH
        # edge cases that cannot appear here.
        ip_str = str(sockaddr[0])
        if family == socket.AF_INET6:
            # Strip the scope-id suffix that link-local addresses carry
            # (e.g. ``fe80::1%en0``); `ipaddress.ip_address` rejects it.
            ip_str = ip_str.split("%", 1)[0]
        try:
            addrs.append(ipaddress.ip_address(ip_str))
        except ValueError:
            # Should be unreachable: `getaddrinfo` returns parsed
            # addresses. Surfacing as a deny is the conservative choice.
            return False, f"unparseable address from DNS: {ip_str!r}"

    if not addrs:
        return False, f"DNS returned no addresses for {host!r}"
    non_loopback = [a for a in addrs if not a.is_loopback]
    if non_loopback:
        rendered = ", ".join(str(a) for a in non_loopback)
        return (
            False,
            f"hostname {host!r} resolves to non-loopback addresses: {rendered}",
        )
    return True, f"{host!r} resolves to loopback-only"
