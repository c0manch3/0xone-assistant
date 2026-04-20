"""R9 — WebFetch SSRF guard: DNS rebinding consideration.

Goal: Demonstrate that a DNS-resolution-aware guard blocks more than a
string-match guard. Document residual TOCTOU DNS-rebinding risk.

Approach:
- `_guard_string_only(url)` — the v2-as-designed guard (URL prefix / hostname
  string match against private-range heuristics).
- `_guard_with_dns(url)` — adds socket.getaddrinfo() and rejects if any
  resolved IP is in private/loopback/link-local/reserved.
- Test matrix of hosts, including a synthetic DNS mock that returns 127.0.0.1
  for a public-looking hostname (simulates DNS rebinding / attacker-controlled
  DNS).
"""
from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch
from urllib.parse import urlparse


_BLOCKED_HOST_SUBSTRINGS: tuple[str, ...] = (
    "localhost", "127.", "0.0.0.0", "169.254.", "10.",
    "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.",
    "[::1]", "[fc", "[fd",
)


def _guard_string_only(url: str) -> tuple[bool, str]:
    """Return (blocked, reason)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return (True, "malformed URL")
    raw = url.lower()
    for needle in _BLOCKED_HOST_SUBSTRINGS:
        if host.startswith(needle.rstrip(".").rstrip("]")) or needle in raw:
            return (True, f"string match: {needle}")
    return (False, "ok (string)")


def _ip_is_blocked(ip_str: str) -> str | None:
    """Return reason if IP is in private ranges, else None."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return f"invalid IP {ip_str!r}"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link_local"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    # AWS IMDS is 169.254.169.254 (link_local already catches it)
    return None


def _guard_with_dns(url: str) -> tuple[bool, str]:
    """String-guard plus DNS resolution check.

    Caveat: TOCTOU — the IP resolved here may differ from the IP CLI actually
    fetches milliseconds later. That is DNS rebinding; mitigation is OS-level
    egress ACL.
    """
    blocked, reason = _guard_string_only(url)
    if blocked:
        return (True, reason)
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return (True, "malformed URL")
    if not host:
        return (True, "empty host")
    try:
        # best-effort: getaddrinfo returns list of (family, type, proto, canon, sockaddr)
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        # NXDOMAIN → allow? or block? Conservative: allow (might be network issue),
        # but in production we should log. For the guard we'll allow to avoid
        # breaking legitimate fetches on transient DNS failures.
        return (False, f"dns resolution failed: {e}")
    for _, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        reason = _ip_is_blocked(ip)
        if reason:
            return (True, f"DNS → {ip} ({reason})")
    return (False, "ok (DNS)")


# --- test cases ---

CASES = [
    # (url, expected_blocked_string_only, expected_blocked_with_dns, label)
    ("https://example.com/", False, False, "public host"),
    ("http://169.254.169.254/latest/meta-data/", True, True, "AWS IMDS"),
    ("http://127.0.0.1:8080/admin", True, True, "localhost IP"),
    ("http://localhost/", True, True, "localhost hostname"),
    ("http://192.168.1.1/router", True, True, "RFC1918 literal"),
    ("http://[::1]/", True, True, "IPv6 loopback"),
    ("http://[fc00::1]/", True, True, "IPv6 ULA"),
    ("http://10.0.0.5/", True, True, "10/8 RFC1918 literal"),
    ("http://172.16.0.1/", True, True, "172.16/12 literal"),
    ("https://internal.corp/", False, False,  # DNS likely NXDOMAIN
     "internal corp (NXDOMAIN → allow)"),
]


def test_string_only() -> None:
    print("=== guard_string_only ===")
    for url, want_block, _want_dns, label in CASES:
        got_block, reason = _guard_string_only(url)
        mark = "✓" if got_block == want_block else "✗"
        print(f"  {mark} want_block={want_block}  got_block={got_block:<5} "
              f"[{reason}] — {label} {url}")


def test_with_dns() -> None:
    print("\n=== guard_with_dns (live DNS) ===")
    for url, _want_s, want_block, label in CASES:
        if "internal.corp" in url:
            # DNS failure → guard allows; expect False unless the machine happens to resolve
            got_block, reason = _guard_with_dns(url)
            print(f"  [info] NXDOMAIN path: got_block={got_block} reason={reason} "
                  f"— {label}")
            continue
        got_block, reason = _guard_with_dns(url)
        mark = "✓" if got_block == want_block else "✗"
        print(f"  {mark} want_block={want_block}  got_block={got_block:<5} "
              f"[{reason}] — {label} {url}")


def test_dns_rebinding_sim() -> None:
    """Simulate a public-looking hostname resolving to 127.0.0.1."""
    print("\n=== DNS-rebinding simulation (mocked getaddrinfo) ===")
    public_url = "https://totally-innocent.example/"

    # Without DNS: string guard lets it through
    blocked_s, reason_s = _guard_string_only(public_url)
    print(f"  string-only: blocked={blocked_s} — {reason_s}")

    # With DNS but mocked to return 127.0.0.1
    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    with patch("socket.getaddrinfo", fake_getaddrinfo):
        blocked_d, reason_d = _guard_with_dns(public_url)
    print(f"  with-DNS (mocked 127.0.0.1): blocked={blocked_d} — {reason_d}")

    # With DNS mocked to return a legitimate public IP
    def fake_getaddrinfo_public(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
    with patch("socket.getaddrinfo", fake_getaddrinfo_public):
        blocked_p, reason_p = _guard_with_dns(public_url)
    print(f"  with-DNS (mocked public 93.184.216.34): blocked={blocked_p} — {reason_p}")


def main() -> None:
    test_string_only()
    test_with_dns()
    test_dns_rebinding_sim()
    print("\n=== TOCTOU residual risk ===")
    print("Even `guard_with_dns` re-resolves the name; CLI/SDK then does its own")
    print("resolution ~ms later. Attacker DNS that alternates RR-cycle could return")
    print("public IP to us and private IP to the fetch. Mitigation: OS-level egress")
    print("ACL (iptables / firewall) blocking private ranges outbound. Guard is")
    print("best-effort defence-in-depth.")


if __name__ == "__main__":
    main()
