"""SSRF helper mirror — kept byte-identical with src/assistant/bridge/net.py.

The installer is a separate entrypoint invoked via the phase-2 Bash
allowlist with cwd=project_root; importing from src/assistant/bridge/ would
require adding src/ to sys.path and coupling the installer to the main
package layout. Mirror is ugly but local; a unit test
(tests/test_ssrf_mirror_in_sync.py) asserts the block between the
`SSRF_MIRROR_START` / `SSRF_MIRROR_END` sentinels in the source file is
also present verbatim in this mirror.

If you edit anything inside the sentinels you MUST also copy it into this
file. Do NOT drift.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse


# --- SSRF_MIRROR_START (mirrored to tools/skill-installer/_lib/_net_mirror.py) ---
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


def classify_url_sync(url: str, *, dns_timeout: float = 3.0) -> str | None:
    """Synchronous SSRF gate for urllib-based fetches inside the installer.

    The installer is stdlib-only (B-4) and deliberately avoids pulling in
    asyncio for simple HTTPS calls. This helper wraps the async
    `classify_url` with a short-lived event loop per invocation.
    """
    return asyncio.run(classify_url(url, dns_timeout=dns_timeout))
