"""Phase 7 / commit 6 — `_DedupLedger` TTL + LRU semantics.

Per plan §4.1 H-12 the dedup-ledger tests are SPLIT into two
variants:

  1. **Mock-clock (authoritative)** — injects a deterministic float
     `now` into `mark_and_check`. This is the contract we verify;
     it MUST pass strictly.

  2. **Real-clock (smoke, xfail-if-flaky)** — uses `time.monotonic()`
     across real `time.sleep` calls with tight (≤20 ms) windows. It
     is marked `xfail(strict=False)` because kernel scheduling and
     CI jitter can flip the outcome. An XPASS is welcomed; a FAIL
     does not fail the gate.

Coverage on top of TTL:
  * Fresh key → False (not seen).
  * Second call within TTL → True (caller SKIPS send).
  * Second call past TTL → False (record afresh).
  * LRU eviction at `max_entries` boundary.
  * `_evict_expired` removes stale keys (freeing LRU budget).
  * Init rejects non-positive ``ttl_s`` / ``max_entries``.
"""

from __future__ import annotations

import time

import pytest

from assistant.adapters.dispatch_reply import _DedupLedger


# --- Variant 1: authoritative mock-clock contract -------------------


def test_dedup_ttl_mock_clock() -> None:
    """Fresh key → False; repeat within TTL → True; repeat past TTL → False."""
    ledger = _DedupLedger(ttl_s=10.0)

    # First mark: fresh → False (send proceeds).
    assert ledger.mark_and_check(("p", 1), now=0.0) is False
    # Within TTL → True (skip send).
    assert ledger.mark_and_check(("p", 1), now=5.0) is True
    # At exactly TTL: the ledger treats >= TTL as expired.
    assert ledger.mark_and_check(("p", 1), now=15.0) is False


def test_dedup_distinct_keys_independent() -> None:
    ledger = _DedupLedger(ttl_s=100.0)
    assert ledger.mark_and_check(("a", 1), now=0.0) is False
    assert ledger.mark_and_check(("b", 1), now=1.0) is False
    # Same path but different chat_id — distinct key.
    assert ledger.mark_and_check(("a", 2), now=2.0) is False
    # Repeat of first key: within TTL → True.
    assert ledger.mark_and_check(("a", 1), now=3.0) is True


def test_dedup_lru_eviction_at_capacity() -> None:
    """Oldest entry evicted when `max_entries` is exceeded."""
    ledger = _DedupLedger(ttl_s=1_000.0, max_entries=3)
    ledger.mark_and_check(("a", 1), now=0.0)
    ledger.mark_and_check(("b", 1), now=1.0)
    ledger.mark_and_check(("c", 1), now=2.0)
    # Insert a 4th key — "a" should be evicted (LRU front).
    ledger.mark_and_check(("d", 1), now=3.0)

    # Check the RETAINED keys first so we don't perturb LRU by
    # re-inserting "a". ("b", "c", "d" are still in window → True.)
    assert ledger.mark_and_check(("b", 1), now=4.0) is True
    assert ledger.mark_and_check(("c", 1), now=4.1) is True
    assert ledger.mark_and_check(("d", 1), now=4.2) is True
    # "a" was evicted by the "d" insertion — re-checking returns
    # False (fresh). NOTE: this call re-inserts "a" and therefore
    # evicts the new LRU ("b" at this point), but we've already
    # observed "b" above so the side-effect is harmless.
    assert ledger.mark_and_check(("a", 1), now=4.3) is False


def test_dedup_lru_refresh_on_hit() -> None:
    """A hit (repeat within TTL) refreshes LRU position. After a hit,
    an insertion at capacity should evict the OTHER, older entry —
    not the one we just refreshed."""
    ledger = _DedupLedger(ttl_s=1_000.0, max_entries=2)
    ledger.mark_and_check(("old", 1), now=0.0)
    ledger.mark_and_check(("new", 1), now=1.0)

    # Hit on "old" — refreshes its LRU position.
    assert ledger.mark_and_check(("old", 1), now=2.0) is True

    # Insert a third key — "new" should now be evicted (it's LRU
    # because "old" was refreshed).
    ledger.mark_and_check(("third", 1), now=3.0)

    # Check "old" FIRST (retained via refresh). A repeated hit here
    # just refreshes LRU to [third, old]; nothing else is evicted.
    assert ledger.mark_and_check(("old", 1), now=4.0) is True
    # "new" evicted → fresh on next call. (This call re-inserts
    # "new" and evicts the new LRU ("third") — harmless because
    # we've already observed the refresh evidence on "old" above.)
    assert ledger.mark_and_check(("new", 1), now=4.1) is False


def test_dedup_evict_expired_frees_lru_budget() -> None:
    """Expired entries are swept on every call, making room for new
    keys even when the nominal `max_entries` is small."""
    ledger = _DedupLedger(ttl_s=10.0, max_entries=2)
    ledger.mark_and_check(("a", 1), now=0.0)
    ledger.mark_and_check(("b", 1), now=1.0)

    # Fast-forward past TTL — both "a" and "b" should be swept on
    # next call.
    assert ledger.mark_and_check(("c", 1), now=100.0) is False
    # The ledger should now hold only "c"; re-checking "a" / "b"
    # returns False (fresh).
    assert ledger.mark_and_check(("a", 1), now=101.0) is False
    assert ledger.mark_and_check(("b", 1), now=102.0) is False


def test_dedup_repeat_after_ttl_returns_false_and_records() -> None:
    """After TTL, a repeated key is treated as fresh AND recorded
    afresh. A follow-up within the new TTL should see True."""
    ledger = _DedupLedger(ttl_s=10.0)
    ledger.mark_and_check(("p", 1), now=0.0)
    # Past TTL — fresh.
    assert ledger.mark_and_check(("p", 1), now=20.0) is False
    # Within new TTL window starting at now=20 — hit.
    assert ledger.mark_and_check(("p", 1), now=25.0) is True


def test_dedup_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_s"):
        _DedupLedger(ttl_s=0.0)
    with pytest.raises(ValueError, match="ttl_s"):
        _DedupLedger(ttl_s=-1.0)


def test_dedup_rejects_non_positive_max_entries() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        _DedupLedger(max_entries=0)
    with pytest.raises(ValueError, match="max_entries"):
        _DedupLedger(max_entries=-5)


def test_dedup_default_constants_match_plan_spec() -> None:
    """Guard against drift: plan §2.6 pins TTL=300 s, cap=256."""
    ledger = _DedupLedger()
    # Private fields — we read them once here to pin the public
    # default. If someone changes these, this test fails loudly.
    assert ledger._ttl_s == 300.0  # noqa: SLF001
    assert ledger._max_entries == 256  # noqa: SLF001


# --- Variant 2: real-clock smoke (xfail-if-flaky) -------------------


@pytest.mark.xfail(
    reason=(
        "real clock dependent; Variant 1 (test_dedup_ttl_mock_clock) "
        "is authoritative. CI jitter at the 10 ms TTL can flip this."
    ),
    strict=False,
)
def test_dedup_ttl_real_clock() -> None:
    ledger = _DedupLedger(ttl_s=0.010)
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is False
    time.sleep(0.001)
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is True
    time.sleep(0.020)
    now = time.monotonic()
    assert ledger.mark_and_check(("p", 1), now=now) is False
