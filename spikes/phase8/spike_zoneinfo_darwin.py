#!/usr/bin/env python3
"""Phase 8 R-10 / B4: confirm `zoneinfo` renders commit-message `{date}` correctly on macOS Darwin 24.

Checks:
  * zoneinfo importable.
  * `datetime.now(ZoneInfo('Europe/Moscow'))` at a known UTC instant
    that differs from UTC calendar day by +3h → verified.
  * `datetime.now(ZoneInfo('UTC'))` at same instant.
  * DST transition for Europe/Berlin (CET → CEST on 2026-03-29 01:00 UTC
    moves clocks from 02:00 → 03:00 local). Render before/after and
    verify %Y-%m-%d advances correctly.
  * Unknown zone raises `ZoneInfoNotFoundError`.
  * Fallback: if macOS lacks tzdata, there is a `tzdata` package on
    PyPI. Verify our environment does not need it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    report: dict[str, object] = {
        "platform": sys.platform,
        "python_version": sys.version,
    }
    zoneinfo_spec = importlib.util.find_spec("zoneinfo")
    report["zoneinfo_available"] = zoneinfo_spec is not None
    if not report["zoneinfo_available"]:
        out_path = Path(__file__).with_name("spike_zoneinfo_darwin_report.json")
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return 1

    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    # Probe known anchor: 2026-04-18 23:30 UTC.
    # MSK = UTC+3 → 2026-04-19 02:30 MSK → %Y-%m-%d = 2026-04-19
    # UTC                              → %Y-%m-%d = 2026-04-18
    anchor = datetime(2026, 4, 18, 23, 30, tzinfo=timezone.utc)
    msk = anchor.astimezone(ZoneInfo("Europe/Moscow"))
    utc = anchor.astimezone(ZoneInfo("UTC"))
    report["anchor_iso"] = anchor.isoformat()
    report["msk_render"] = {
        "iso": msk.isoformat(),
        "date": msk.strftime("%Y-%m-%d"),
    }
    report["utc_render"] = {
        "iso": utc.isoformat(),
        "date": utc.strftime("%Y-%m-%d"),
    }
    report["msk_utc_cross_midnight"] = (
        report["msk_render"]["date"] != report["utc_render"]["date"]
    )

    # Berlin DST transition (CET → CEST) happens in Europe every last
    # Sunday of March at 01:00 UTC. 2026-03-29 01:00 UTC.
    berlin_before = (datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc)).astimezone(
        ZoneInfo("Europe/Berlin")
    )
    berlin_after = (datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)).astimezone(
        ZoneInfo("Europe/Berlin")
    )
    report["berlin_dst"] = {
        "before_iso": berlin_before.isoformat(),
        "after_iso": berlin_after.isoformat(),
        "before_offset": str(berlin_before.utcoffset()),
        "after_offset": str(berlin_after.utcoffset()),
        "jumps_one_hour": (berlin_after.utcoffset() or timedelta(0))
        - (berlin_before.utcoffset() or timedelta(0))
        == timedelta(hours=1),
    }

    # Invalid zone.
    try:
        ZoneInfo("Xyz/Nowhere")
        report["invalid_zone"] = "UNEXPECTED_OK"
    except ZoneInfoNotFoundError as exc:
        report["invalid_zone"] = f"raises ZoneInfoNotFoundError: {exc}"

    # tzdata presence is optional on macOS; report explicitly.
    tzdata_spec = importlib.util.find_spec("tzdata")
    report["tzdata_pypi_installed"] = tzdata_spec is not None

    # Realistic "live" render for the commit-message template.
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    report["now_msk"] = {
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
    }

    out_path = Path(__file__).with_name("spike_zoneinfo_darwin_report.json")
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
