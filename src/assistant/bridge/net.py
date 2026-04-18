"""Shared SSRF-classification helpers.

This module hosts the *canonical* implementation of three tiny helpers:

* `is_private_address` — one-liner classification of an `ipaddress` object.
* `resolve_hostname`   — async DNS resolution with an `asyncio.timeout`.
* `classify_url`       — combined URL validator: scheme check, DNS resolve,
  private-range reject.

The block between the `SSRF_MIRROR_START` / `SSRF_MIRROR_END` sentinels is
mirrored **byte-for-byte** into `tools/skill_installer/_lib/_net_mirror.py`.
The installer is a separate entrypoint (stdlib-only, see
`plan/phase3/implementation.md §2.8`) that cannot import from the main
package without coupling `sys.path` to it — duplicating the ~60 LOC block
and asserting equality via a test is cheaper than plumbing an installable
shared sub-package.

If you edit anything between the sentinels you MUST also re-copy the block
into `_net_mirror.py`. The test `tests/test_ssrf_mirror_in_sync.py`
fails fast when the two drift.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


# --- SSRF_MIRROR_START (mirrored to tools/skill_installer/_lib/_net_mirror.py) ---
def is_private_address(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """True iff `addr` falls in any non-public range we refuse to fetch."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def resolve_hostname(
    hostname: str, *, deadline_s: float
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve `hostname` to a list of IPs via the loop's resolver.

    Raises `socket.gaierror` / `OSError` on failure and `TimeoutError` on
    hang. Parameter name `deadline_s` sidesteps ruff ASYNC109; we enforce
    the deadline with `asyncio.timeout()` internally.
    """
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(deadline_s):
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if family == socket.AF_INET6:
            ip_str = ip_str.split("%", 1)[0]
        out.append(ipaddress.ip_address(ip_str))
    return out


async def classify_url(url: str, *, dns_timeout: float = 3.0) -> str | None:
    """Return deny-reason iff URL targets a non-public destination, else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"malformed URL: {url!r}"
    if parsed.scheme not in {"http", "https"}:
        return f"only http(s) is allowed; got scheme {parsed.scheme!r}"
    hostname = (parsed.hostname or "").strip()
    if not hostname:
        return "URL has no hostname"

    try:
        ip_literal = ipaddress.ip_address(hostname)
    except ValueError:
        ip_literal = None
    if ip_literal is not None:
        if is_private_address(ip_literal):
            return f"IP literal targets non-public range: {ip_literal}"
        return None

    try:
        addrs = await resolve_hostname(hostname, deadline_s=dns_timeout)
    except TimeoutError:
        return f"DNS lookup for {hostname!r} timed out"
    except (socket.gaierror, OSError) as exc:
        return f"cannot resolve host {hostname!r}: {exc}"
    if not addrs:
        return f"DNS returned no addresses for {hostname!r}"
    for addr in addrs:
        if is_private_address(addr):
            return f"hostname {hostname!r} resolves to non-public address {addr} (SSRF defence)"
    return None


# --- SSRF_MIRROR_END ---
