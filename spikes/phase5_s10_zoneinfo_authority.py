"""S-10 (phase 5 wave-2 fix-pack): ZoneInfo authority probe.

Closes B-W2-4: drop `_TZ_RE` regex in `tools/schedule/main.py`; rely on
`zoneinfo.ZoneInfo(args.tz)` + `ZoneInfoNotFoundError` as the sole tz validator.

Verifies that stdlib `zoneinfo` accepts legitimate IANA names that the old
regex `^[A-Za-z_]+(/[A-Za-z_]+(/[A-Za-z_]+)?)?$` REJECTS (e.g. `Etc/GMT+3`
contains `+` which regex disallows), while raising `ZoneInfoNotFoundError`
on malformed / injection-shaped names.

Run:
    python spikes/phase5_s10_zoneinfo_authority.py

Writes `spikes/phase5_s10_report.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CANDIDATES: list[str] = [
    "UTC",                 # trivial accept
    "Europe/Berlin",       # common IANA accept
    "Etc/GMT+3",           # legit IANA w/ '+': REGEX would reject (has '+')
    "CST6CDT",             # legit IANA w/o '/': REGEX accepts
    "America/Argentina/Buenos_Aires",  # 3-level IANA: REGEX accepts
    "../../etc/passwd",    # path injection: must raise
    "Etc/GMT+99",          # out-of-range: must raise
    "",                    # empty: must raise
    "Europe/NotACity",     # plausible but non-existent: must raise
]


def probe_one(name: str) -> dict[str, object]:
    try:
        zi = ZoneInfo(name)
        return {
            "name": name,
            "accepted": True,
            "repr": repr(zi),
        }
    except ZoneInfoNotFoundError as exc:
        return {
            "name": name,
            "accepted": False,
            "error": f"ZoneInfoNotFoundError: {exc}",
        }
    except Exception as exc:  # pragma: no cover — report weird edge cases
        return {
            "name": name,
            "accepted": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> None:
    results = [probe_one(n) for n in CANDIDATES]
    out = {
        "platform": __import__("platform").platform(),
        "python": __import__("sys").version.split()[0],
        "candidates": results,
        "regex_pattern": r"^[A-Za-z_]+(/[A-Za-z_]+(/[A-Za-z_]+)?)?$",
        "regex_would_reject": ["Etc/GMT+3"],  # manual — regex disallows '+'
        "verdict": {
            "stdlib_is_authoritative": all(
                r["accepted"] for r in results if r["name"] in
                {"UTC", "Europe/Berlin", "Etc/GMT+3", "CST6CDT",
                 "America/Argentina/Buenos_Aires"}
            ) and all(
                not r["accepted"] for r in results if r["name"] in
                {"../../etc/passwd", "Etc/GMT+99", "",
                 "Europe/NotACity"}
            ),
            "regex_misses_valid_names": True,
        },
    }
    report_path = Path(__file__).with_name("phase5_s10_report.json")
    report_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
