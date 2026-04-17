"""Spike S-3: zoneinfo DST spring-skip + fall-ambiguity semantics.

Question: Plan §2.2 claims:
  * `local_dt.replace(fold=1) != local_dt.replace(fold=0)` detects
    non-existent minutes (DST spring skip);
  * fall-back ambiguous minute: match only fold=0.

Is this the real behaviour of zoneinfo.ZoneInfo on Python 3.12? We verify
for Europe/Berlin 2026-03-29 (spring: 02:00 → 03:00) and 2026-10-25
(fall: 03:00 → 02:00).

Pass criterion: is_existing_local_minute correctly reports 2:30 AM as
non-existent on the spring day; the fall day marks it as existing+ambiguous.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def is_existing_local_minute(local_dt: datetime, tz: ZoneInfo) -> bool:
    """A local wall-clock time "exists" iff fold=0 and fold=1 utcoffsets agree.

    During spring-forward the "skipped" hour produces different offsets for
    fold=0 and fold=1 (zoneinfo coerces by PEP 495 — both map to something,
    but NEITHER corresponds to real wall time). We detect that as "does not
    exist". During fall-back the utcoffset differs between fold=0 and
    fold=1 — both are valid, and we choose fold=0 per spec §2.2.
    """
    a = local_dt.replace(fold=0, tzinfo=tz)
    b = local_dt.replace(fold=1, tzinfo=tz)
    # Spring-skip: a.utcoffset() != b.utcoffset() AND a_as_utc + offset != local
    # Simpler heuristic: convert both to UTC then back to local; if the
    # round-trip mismatches the original, the minute is synthetic.
    a_utc = a.astimezone(ZoneInfo("UTC"))
    a_local_round = a_utc.astimezone(tz).replace(tzinfo=None)
    b_utc = b.astimezone(ZoneInfo("UTC"))
    b_local_round = b_utc.astimezone(tz).replace(tzinfo=None)
    naked = local_dt.replace(tzinfo=None, fold=0)
    return a_local_round == naked or b_local_round == naked


def is_ambiguous_local_minute(local_dt: datetime, tz: ZoneInfo) -> bool:
    """True iff fold=0 and fold=1 resolve to DIFFERENT utcoffsets (both valid)."""
    a = local_dt.replace(fold=0, tzinfo=tz)
    b = local_dt.replace(fold=1, tzinfo=tz)
    return a.utcoffset() != b.utcoffset() and is_existing_local_minute(local_dt, tz)


def probe_day(tz_name: str, year: int, month: int, day: int) -> dict:
    tz = ZoneInfo(tz_name)
    observations = []
    for hour in range(0, 5):
        for minute in (0, 15, 30, 45):
            naked = datetime(year, month, day, hour, minute)
            a = naked.replace(fold=0, tzinfo=tz)
            b = naked.replace(fold=1, tzinfo=tz)
            observations.append(
                {
                    "local": naked.isoformat(),
                    "fold0_offset": str(a.utcoffset()),
                    "fold1_offset": str(b.utcoffset()),
                    "fold0_utc": a.astimezone(ZoneInfo("UTC")).isoformat(),
                    "fold1_utc": b.astimezone(ZoneInfo("UTC")).isoformat(),
                    "exists": is_existing_local_minute(naked, tz),
                    "ambiguous": is_ambiguous_local_minute(naked, tz),
                }
            )
    return {"tz": tz_name, "date": f"{year:04d}-{month:02d}-{day:02d}", "minutes": observations}


def minute_by_minute(tz_name: str, start_utc: datetime, n_minutes: int) -> list[dict]:
    """Sanity: from UTC midnight onward, cross the transition minute-by-minute."""
    tz = ZoneInfo(tz_name)
    out = []
    for i in range(n_minutes):
        ut = start_utc + timedelta(minutes=i)
        local = ut.astimezone(tz)
        out.append(
            {
                "utc": ut.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local": local.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "offset_min": int(local.utcoffset().total_seconds() / 60),
            }
        )
    return out


def verify_assertions() -> dict:
    tz = ZoneInfo("Europe/Berlin")
    results = {}

    # Spring: 2026-03-29 02:30 local — should NOT exist.
    spring_2_30 = datetime(2026, 3, 29, 2, 30)
    results["spring_2_30_exists"] = is_existing_local_minute(spring_2_30, tz)
    results["spring_2_30_ambiguous"] = is_ambiguous_local_minute(spring_2_30, tz)

    # Spring: 2026-03-29 01:30 local — should exist (before the skip).
    spring_1_30 = datetime(2026, 3, 29, 1, 30)
    results["spring_1_30_exists"] = is_existing_local_minute(spring_1_30, tz)

    # Spring: 2026-03-29 03:30 local — should exist (after the skip).
    spring_3_30 = datetime(2026, 3, 29, 3, 30)
    results["spring_3_30_exists"] = is_existing_local_minute(spring_3_30, tz)

    # Fall: 2026-10-25 02:30 local — should exist AND be ambiguous.
    fall_2_30 = datetime(2026, 10, 25, 2, 30)
    results["fall_2_30_exists"] = is_existing_local_minute(fall_2_30, tz)
    results["fall_2_30_ambiguous"] = is_ambiguous_local_minute(fall_2_30, tz)

    # Fall: 2026-10-25 01:30 local — exists, unambiguous.
    fall_1_30 = datetime(2026, 10, 25, 1, 30)
    results["fall_1_30_exists"] = is_existing_local_minute(fall_1_30, tz)
    results["fall_1_30_ambiguous"] = is_ambiguous_local_minute(fall_1_30, tz)

    # Test the fold=0 fire policy: during fall, cron `30 2 * * *` should
    # produce exactly ONE UTC instant even though local 02:30 occurs twice.
    # We iterate UTC minutes and count those whose local-representation
    # equals 02:30 AND fold is 0.
    out_utc = []
    start = datetime(2026, 10, 25, 0, 0, tzinfo=ZoneInfo("UTC"))
    for i in range(24 * 60):
        ut = start + timedelta(minutes=i)
        local = ut.astimezone(tz)
        if local.hour == 2 and local.minute == 30 and local.fold == 0:
            out_utc.append(ut.isoformat())
    results["fall_2_30_utc_matches_fold0"] = out_utc

    return results


def main() -> None:
    report = {
        "spring_day_observations": probe_day("Europe/Berlin", 2026, 3, 29),
        "fall_day_observations": probe_day("Europe/Berlin", 2026, 10, 25),
        "spring_minute_by_minute": minute_by_minute(
            "Europe/Berlin", datetime(2026, 3, 29, 0, 45, tzinfo=ZoneInfo("UTC")), 180
        ),
        "fall_minute_by_minute": minute_by_minute(
            "Europe/Berlin", datetime(2026, 10, 25, 0, 45, tzinfo=ZoneInfo("UTC")), 180
        ),
        "assertions": verify_assertions(),
    }
    # Collapse the minute_by_minute arrays for brevity on stdout; still
    # include the 5 minutes around each transition.
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
