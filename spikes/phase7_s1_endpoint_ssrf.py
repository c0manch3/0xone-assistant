"""Phase 7 spike S-1 — endpoint SSRF guard (devil Gap #6).

Probes the SSRF classification via `classify_url` (same pattern phase-3
uses). Thin CLI clients for transcribe/genimage MUST reject anything
that isn't loopback — but "loopback" covers:
  - literal `localhost` (DNS-resolves to 127.0.0.1 or ::1)
  - 127.0.0.0/8
  - ::1 (IPv6 loopback)
  - `localhost.localdomain` on some systems

And MUST reject:
  - AWS IMDS: 169.254.169.254
  - Any public hostname (api.telegram.org, etc.)
  - Private LAN (192.168.x.x, 10.x.x.x, 172.16-31.x.x)

This spike verifies the behavior against `assistant.bridge.net.classify_url`.
CLI layer then wraps with a narrow "loopback-only" check:
  the classify_url returns None (public OK) for public, and a reason
  for private/reserved. We want the CLI to reject BOTH:
  - public (not loopback) → "endpoint must be loopback"
  - private non-loopback (10.x, 192.168.x) → also not loopback

So the CLI rule is stricter: require parsed hostname to classify as
loopback specifically. The `is_private_address` helper groups them
all — we need a narrower `is_loopback_only` wrapper.

Run:  uv run python spikes/phase7_s1_endpoint_ssrf.py
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s1_report.json"
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from assistant.bridge.net import classify_url, is_private_address  # noqa: E402


async def _is_loopback_only(url: str) -> tuple[bool, str]:
    """Narrower than classify_url: require resolved hostname to be loopback.

    classify_url denies non-public → returns a reason string.
    For a transcribe endpoint, we want the opposite: ALLOW only if
    the host is loopback specifically; everything else denied.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "malformed URL"
    if parsed.scheme not in {"http", "https"}:
        return False, f"scheme must be http(s), got {parsed.scheme!r}"
    host = (parsed.hostname or "").strip()
    if not host:
        return False, "URL has no hostname"

    # Direct IP literal?
    try:
        addr: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr is not None:
        if addr.is_loopback:
            return True, "ip literal is loopback"
        return False, f"ip literal {addr} is not loopback"

    # Hostname: resolve and require ALL addresses to be loopback.
    import socket

    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, f"DNS resolve failed: {exc}"

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _st, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if family == socket.AF_INET6:
            ip_str = ip_str.split("%", 1)[0]
        addrs.append(ipaddress.ip_address(ip_str))

    if not addrs:
        return False, "DNS returned no addresses"
    non_loopback = [a for a in addrs if not a.is_loopback]
    if non_loopback:
        return False, f"hostname {host!r} resolves to non-loopback: {non_loopback}"
    return True, f"{host} loopback-only"


CASES: list[dict[str, object]] = [
    # (url, expected_loopback_only_bool, description)
    {"url": "http://localhost:9100/transcribe", "expect_loopback": True},
    {"url": "http://127.0.0.1:9100/transcribe", "expect_loopback": True},
    {"url": "http://127.0.0.2:9100/transcribe", "expect_loopback": True},
    {"url": "http://[::1]:9100/transcribe", "expect_loopback": True},
    # NOTE: localhost.localdomain may not resolve on all systems; record outcome
    {"url": "http://localhost.localdomain:9100/transcribe", "expect_loopback": None},
    {"url": "http://169.254.169.254/", "expect_loopback": False},
    {"url": "http://10.0.0.1:9100/", "expect_loopback": False},
    {"url": "http://192.168.1.1:9100/", "expect_loopback": False},
    {"url": "https://api.telegram.org/", "expect_loopback": False},
    {"url": "ftp://localhost/", "expect_loopback": False},
    {"url": "http://:9100/", "expect_loopback": False},
]


async def main() -> None:
    results: list[dict[str, object]] = []
    for case in CASES:
        url = case["url"]
        expect = case["expect_loopback"]
        ok, reason = await _is_loopback_only(str(url))
        # Also call classify_url for comparison (phase-3 behaviour).
        try:
            classify_reason = await classify_url(str(url))
        except Exception as exc:  # noqa: BLE001
            classify_reason = f"classify_url raised: {exc!r}"
        results.append(
            {
                "url": url,
                "expected_loopback_only": expect,
                "loopback_only_ok": ok,
                "loopback_only_reason": reason,
                "classify_url_reason": classify_reason,
                "match_expectation": (expect is None) or (expect == ok),
            }
        )
        mark = "OK" if (expect is None or expect == ok) else "MISMATCH"
        print(f"[{mark}] {url:55s} loopback_ok={ok}  reason={reason}")

    all_ok = all(r["match_expectation"] for r in results)
    REPORT.write_text(
        json.dumps(
            {
                "verdict": "PASS" if all_ok else "PARTIAL",
                "cases": results,
                "decision": (
                    "CLI `_validate_endpoint(url)` should delegate to a "
                    "narrow `is_loopback_only(url)` wrapper — NOT to "
                    "`classify_url` which permits ALL non-public "
                    "(loopback OR 10.x OR 192.168.x OR link-local). "
                    "Private-LAN endpoints are disallowed too: host machine "
                    "in phase-7 architecture is reached via SSH reverse "
                    "tunnel on 127.0.0.1:<port>, so rejecting non-loopback "
                    "is strictly correct."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print(f"\nReport -> {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
