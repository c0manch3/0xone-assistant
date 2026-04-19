"""Integration test — both `is_loopback_only` mirrors must agree.

Phase-7 S-1 finding (see `spikes/phase7_s1_endpoint_ssrf.py`) mandates
two stdlib-only "loopback-only" guards that deliberately duplicate the
acceptance rule rather than share code:

  - `tools/transcribe/_net_mirror.py::is_loopback_only`  (async)
  - `tools/genimage/_net_mirror.py::is_loopback_only`    (sync)

They are **not** byte-identical — the transcribe copy is async to fit
the aiohttp-shaped pipeline, the genimage copy is sync because that CLI
is stdlib-only. The two implementations are independently evolved, so
silent drift between them would re-open the SSRF hole that S-1 closed.

This test pins parity. For each of the 11 S-1 corpus URLs we invoke
both mirrors and assert:

  1. both return the same boolean verdict (`ok` must match);
  2. both return a non-empty human-readable `reason` string;
  3. the shared verdict matches the S-1 expectation (deterministic
     cases only — see notes on case 11 below).

Case 11 (`http://2130706433:80` — 127.0.0.1 encoded as a 32-bit
decimal) is a known platform-variant corner: stdlib
`ipaddress.ip_address` rejects the bare integer form, which forces
both mirrors into the DNS fallback. On macOS/Linux with a typical libc
`getaddrinfo` accepts it and returns 127.0.0.1 → both mirrors ALLOW.
On systems where `getaddrinfo` rejects numeric-only hostnames the
result is DENY for both. Either outcome is acceptable so long as the
two mirrors **agree** — the test asserts agreement unconditionally and
only asserts the expected verdict when the environment matches the
common case.

Running the test offline (no DNS) will correctly deny case 7
(`example.com`) because `getaddrinfo` will fail → deny-with-reason.
Parity still holds. Public DNS failure does not invalidate the test.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from tools.genimage._net_mirror import is_loopback_only as _genimage_is_loopback_only
from tools.transcribe._net_mirror import is_loopback_only as _transcribe_is_loopback_only

# ---------------------------------------------------------------------------
# 11-case S-1 corpus — identical to the task spec.
#
# `expected` encodes the verdict that both mirrors MUST produce when the
# environment has a standard libc + usable DNS. `None` means "platform
# variance is acceptable; only assert parity, not the verdict" and is
# used exclusively for case 11 (decimal-encoded IPv4).
# ---------------------------------------------------------------------------
_CASES: list[tuple[str, Optional[bool], str]] = [
    ("http://127.0.0.1:8080", True, "IPv4 loopback literal"),
    ("http://localhost:8080", True, "hostname localhost -> 127.0.0.1 / ::1"),
    ("http://[::1]:8080", True, "IPv6 loopback literal"),
    ("http://10.0.0.1:8080", False, "RFC1918 10/8 is not loopback"),
    ("http://192.168.1.1:8080", False, "RFC1918 192.168/16 is not loopback"),
    ("http://169.254.169.254/", False, "AWS IMDS link-local"),
    ("http://example.com/", False, "public hostname"),
    ("http://[fe80::1]:8080", False, "IPv6 link-local fe80::/10"),
    ("http://[::ffff:8.8.8.8]:8080", False, "IPv4-mapped IPv6, public target"),
    ("https://127.0.0.1:443", True, "https scheme + loopback"),
    ("http://2130706433:80", None, "decimal 127.0.0.1 — platform-dependent"),
]


def _run_transcribe(url: str) -> tuple[bool, str]:
    """Adapter: the transcribe mirror is `async`; the genimage mirror is `sync`.

    Use a per-call event loop so pytest's parametrize cases stay fully
    isolated — `asyncio.run` always creates and closes a fresh loop,
    mirroring `is_loopback_only_sync` in the transcribe module itself.
    """
    return asyncio.run(_transcribe_is_loopback_only(url))


@pytest.mark.parametrize(
    "url, expected, description",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_mirrors_agree(url: str, expected: Optional[bool], description: str) -> None:
    """Both S-1 mirrors must agree on the verdict for every corpus case.

    Silent drift between the transcribe (async) and genimage (sync)
    copies would re-open the SSRF hole that S-1 closed, so parity is
    the load-bearing assertion. We *also* assert the expected verdict
    for deterministic cases to catch regressions that affect both
    mirrors symmetrically (e.g. someone edits the shared acceptance
    rule in both files at once).
    """
    t_ok, t_reason = _run_transcribe(url)
    g_ok, g_reason = _genimage_is_loopback_only(url)

    # Non-empty reason strings are part of the contract — callers log
    # them on deny. A blank reason would silently erase diagnostic
    # information.
    assert isinstance(t_reason, str) and t_reason, (
        f"transcribe mirror returned empty reason for {url!r}"
    )
    assert isinstance(g_reason, str) and g_reason, (
        f"genimage mirror returned empty reason for {url!r}"
    )

    # ---- Load-bearing invariant: both mirrors agree. --------------------
    assert t_ok == g_ok, (
        f"MIRROR DRIFT for {url!r} ({description}): "
        f"transcribe={t_ok!r} ({t_reason!r}) vs genimage={g_ok!r} ({g_reason!r})"
    )

    # ---- Verdict check for deterministic cases. -------------------------
    if expected is None:
        # Case 11 is platform-variant; parity was already verified above.
        return
    assert t_ok is expected, (
        f"{url!r} ({description}): expected both mirrors to return "
        f"ok={expected}, both returned ok={t_ok} "
        f"(transcribe reason={t_reason!r}, genimage reason={g_reason!r})"
    )
