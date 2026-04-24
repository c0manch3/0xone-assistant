"""RQ3 — zoneinfo DST semantics for Europe/Moscow + Europe/Berlin.

Moscow stopped DST in 2011 (constant UTC+3). Berlin observes DST.
We probe:
  - Spring skip: 2026-03-29 02:30 Berlin is non-existent (clocks jump 02→03).
  - Fall fold: 2026-10-25 02:30 Berlin is ambiguous (02:30 happens twice).
  - Moscow 2026: no DST transitions; every minute exists exactly once.

Expected helpers:
  is_existing_local_minute(naked, tz) -> bool
  is_ambiguous_local_minute(naked, tz) -> bool

Policy decision: at ambiguous fold, fire fold=0 only (skip fold=1 duplicate).
At spring-skip, the non-existent minute is treated as "fires at the next
existing minute" OR "silently skipped". We recommend SKIP (no catch-up)
because 02:30 Berlin on DST Sunday is unlikely to be a schedule point users
care about.

Run:
  uv run python plan/phase5/spikes/rq3_dst.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def is_existing_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """True iff `naked` (tz-naive wall clock) exists in `tz` exactly.

    DST spring-skip: 02:30 → 03:30 never happens; result is False.

    Technique: attach tz, round-trip through UTC and back. If the
    wall clock changes, the original was non-existent.
    """
    aware = naked.replace(tzinfo=tz)
    as_utc = aware.astimezone(ZoneInfo("UTC"))
    back = as_utc.astimezone(tz)
    return back.replace(tzinfo=None) == naked


def is_ambiguous_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """True iff `naked` exists twice in `tz` (fall-back fold).

    Technique: build fold=0 and fold=1 variants; if they map to
    different UTC instants, the wall clock is ambiguous.
    """
    a = naked.replace(tzinfo=tz, fold=0)
    b = naked.replace(tzinfo=tz, fold=1)
    return a.astimezone(ZoneInfo("UTC")) != b.astimezone(ZoneInfo("UTC"))


def _probe(name: str, tz_name: str, naked: datetime) -> None:
    tz = ZoneInfo(tz_name)
    exists = is_existing_local_minute(naked, tz)
    ambig = is_ambiguous_local_minute(naked, tz)
    a = naked.replace(tzinfo=tz, fold=0)
    b = naked.replace(tzinfo=tz, fold=1)
    a_utc = a.astimezone(ZoneInfo("UTC"))
    b_utc = b.astimezone(ZoneInfo("UTC"))
    print(f"\n[{name}] {tz_name}  wall={naked.isoformat()}")
    print(f"  exists={exists}  ambiguous={ambig}")
    print(f"  fold=0 -> UTC {a_utc.isoformat()}  offset {a.utcoffset()}")
    print(f"  fold=1 -> UTC {b_utc.isoformat()}  offset {b.utcoffset()}")


def main() -> int:
    # --- Berlin 2026-03-29 spring skip (02:00 CET -> 03:00 CEST).
    _probe("Berlin spring 02:30 (skip)", "Europe/Berlin", datetime(2026, 3, 29, 2, 30))
    _probe("Berlin spring 01:59 (exists)", "Europe/Berlin", datetime(2026, 3, 29, 1, 59))
    _probe("Berlin spring 03:00 (exists)", "Europe/Berlin", datetime(2026, 3, 29, 3, 0))

    # --- Berlin 2026-10-25 fall fold (03:00 CEST -> 02:00 CET — 02:xx ambiguous).
    _probe("Berlin fall 02:30 (ambig)", "Europe/Berlin", datetime(2026, 10, 25, 2, 30))
    _probe("Berlin fall 01:59 (unambig)", "Europe/Berlin", datetime(2026, 10, 25, 1, 59))
    _probe("Berlin fall 03:00 (unambig)", "Europe/Berlin", datetime(2026, 10, 25, 3, 0))

    # --- Moscow 2026-10-25 02:30 — Moscow has no DST since 2011 → unambiguous.
    _probe("Moscow fall 02:30 (no DST)", "Europe/Moscow", datetime(2026, 10, 25, 2, 30))
    _probe("Moscow spring 02:30 (no DST)", "Europe/Moscow", datetime(2026, 3, 29, 2, 30))

    # --- Summary matrix.
    print("\n" + "=" * 70)
    print("SUMMARY MATRIX")
    print("=" * 70)
    cases = [
        ("Europe/Berlin", datetime(2026, 3, 29, 2, 30), "spring-skip 02:30"),
        ("Europe/Berlin", datetime(2026, 10, 25, 2, 30), "fall-fold 02:30"),
        ("Europe/Berlin", datetime(2026, 7, 15, 9, 0), "summer noon"),
        ("Europe/Moscow", datetime(2026, 3, 29, 2, 30), "Moscow spring 02:30"),
        ("Europe/Moscow", datetime(2026, 10, 25, 2, 30), "Moscow fall 02:30"),
        ("UTC", datetime(2026, 3, 29, 2, 30), "UTC spring 02:30"),
    ]
    print(f"{'tz':<18} {'wall':<20} {'exists':<7} {'ambig':<7} note")
    for tz_name, naked, note in cases:
        tz = ZoneInfo(tz_name)
        exists = is_existing_local_minute(naked, tz)
        ambig = is_ambiguous_local_minute(naked, tz)
        print(f"{tz_name:<18} {naked.isoformat():<20} {str(exists):<7} {str(ambig):<7} {note}")

    # Assertions — turn the spike into a self-check.
    assert not is_existing_local_minute(datetime(2026, 3, 29, 2, 30), ZoneInfo("Europe/Berlin"))
    assert is_existing_local_minute(datetime(2026, 3, 29, 3, 0), ZoneInfo("Europe/Berlin"))
    assert is_ambiguous_local_minute(datetime(2026, 10, 25, 2, 30), ZoneInfo("Europe/Berlin"))
    assert not is_ambiguous_local_minute(datetime(2026, 10, 25, 2, 30), ZoneInfo("Europe/Moscow"))
    assert not is_ambiguous_local_minute(datetime(2026, 10, 25, 2, 30), ZoneInfo("UTC"))
    print("\nALL ASSERTIONS PASSED.")

    # Extra: verify fold=0 in Berlin fall-back happens at +02:00 (DST) and
    # fold=1 at +01:00 (standard). Dispatcher policy: fire fold=0 only.
    naked = datetime(2026, 10, 25, 2, 30)
    tz = ZoneInfo("Europe/Berlin")
    f0 = naked.replace(tzinfo=tz, fold=0)
    f1 = naked.replace(tzinfo=tz, fold=1)
    assert f0.utcoffset() == timedelta(hours=2), f"fold=0 offset {f0.utcoffset()}"
    assert f1.utcoffset() == timedelta(hours=1), f"fold=1 offset {f1.utcoffset()}"
    print("Fold=0 = DST (pre-transition); fold=1 = standard (post-transition).")
    print("Policy: fire fold=0 only. Skip fold=1 to prevent double-fire.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
