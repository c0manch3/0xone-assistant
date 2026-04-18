"""Loopback-only endpoint gate for the transcribe CLI.

Phase-7 S-1 finding (see `spikes/phase7_s1_endpoint_ssrf.py` and the
accompanying `phase7_s1_report.json`): delegating `_validate_endpoint(url)`
to `assistant.bridge.net.classify_url` is **wrong** for thin HTTP clients
that front an SSH reverse-tunneled service. `classify_url` treats
`10.x.x.x` and `192.168.x.x` as "private" (same bucket as loopback), but
phase-7 architecture only ever reaches the Mac transcoder on
`127.0.0.1:<port>` via reverse tunnel. A narrower helper is required.

This mirror is **intentionally NOT byte-identical** with
`src/assistant/bridge/net.py` — the phase-3 SSRF gate permits any
non-public destination (loopback, link-local, private LAN), while the
CLI must accept **only** loopback. Writing this helper here (stdlib-only)
keeps the CLI independent of `src/assistant/…` and avoids a `sys.path`
shim into `src/`. The shape deliberately mirrors the spike script
verbatim so the 11-case corpus in `tests/test_tools_transcribe_cli.py`
can be lifted without translation.

Accepted:
- IPv4 literal in `127.0.0.0/8` (including `127.0.0.2`, etc.)
- IPv6 literal `::1`
- Hostname that DNS-resolves exclusively to loopback addresses
  (every address family returned by `getaddrinfo` must satisfy
  `ipaddress.ip_address(...).is_loopback`).

Rejected:
- Non-http(s) scheme (`ftp://`, `file://`, empty)
- Missing hostname (`http://:9100/`)
- Any non-loopback IP literal — public, link-local (`fe80::/10`,
  `169.254.0.0/16`), private LAN (`10.x`, `192.168.x`, `172.16-31.x`),
  IPv4-mapped IPv6 pointing outside loopback, metadata endpoints
  (`169.254.169.254`), etc.
- Hostname that fails DNS (`localhost.localdomain` on systems without
  the hint)
- Hostname that resolves to any non-loopback address (even if SOME
  resolved addresses are loopback — "every address must be loopback"
  is strictly stricter than "at least one", and the weaker rule would
  let DNS rebinding defeat the gate).

Returned value is a `(ok: bool, reason: str)` tuple. The CLI exits with
code 2 on `ok == False` (see `tools/transcribe/main.py::_validate_endpoint`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


async def is_loopback_only(url: str, *, dns_timeout_s: float = 3.0) -> tuple[bool, str]:
    """Return `(True, reason)` iff `url` targets only loopback addresses.

    Narrower than phase-3 `classify_url`. See module docstring for the
    acceptance / rejection matrix. `dns_timeout_s` caps DNS resolution;
    the async/await shape lets the CLI run this under `asyncio.run(...)`
    without pulling aiogram/httpx.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, f"malformed URL: {url!r}"
    if parsed.scheme not in {"http", "https"}:
        return False, f"scheme must be http(s), got {parsed.scheme!r}"
    host = (parsed.hostname or "").strip()
    if not host:
        return False, "URL has no hostname"

    # Direct IP literal? `ipaddress.ip_address` accepts both v4 and v6.
    try:
        addr: _IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr is not None:
        if addr.is_loopback:
            return True, "ip literal is loopback"
        return False, f"ip literal {addr} is not loopback"

    # Hostname: resolve and require ALL returned addresses to be loopback.
    # Note: an attacker controlling DNS could flip a loopback-resolving
    # hostname to a public IP on the next query; users pinning to the
    # literal `127.0.0.1` sidestep this entirely. The CLI documents both
    # forms as acceptable; the only hostname we ever ship as default
    # (`localhost`) is well-known-stable per /etc/hosts.
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(dns_timeout_s):
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except TimeoutError:
        return False, f"DNS lookup for {host!r} timed out"
    except (socket.gaierror, OSError) as exc:
        return False, f"DNS resolve failed: {exc}"

    addrs: list[_IPAddress] = []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if family == socket.AF_INET6:
            # Strip scope-id suffix (`fe80::1%en0`) before parsing.
            ip_str = ip_str.split("%", 1)[0]
        try:
            addrs.append(ipaddress.ip_address(ip_str))
        except ValueError:
            # Defensive: skip malformed entries rather than abort.
            continue

    if not addrs:
        return False, f"DNS returned no usable addresses for {host!r}"

    non_loopback = [a for a in addrs if not a.is_loopback]
    if non_loopback:
        return (
            False,
            f"hostname {host!r} resolves to non-loopback: {non_loopback}",
        )
    return True, f"{host} loopback-only"


def is_loopback_only_sync(url: str, *, dns_timeout_s: float = 3.0) -> tuple[bool, str]:
    """Synchronous wrapper so urllib-based clients can call directly.

    Mirrors `tools/skill_installer/_lib/_net_mirror.classify_url_sync`:
    the transcribe CLI is stdlib-only (urllib + mimetypes + email +
    ipaddress) and deliberately avoids pulling asyncio into the hot
    path. This helper spins a short-lived event loop per invocation.
    """
    return asyncio.run(is_loopback_only(url, dns_timeout_s=dns_timeout_s))
