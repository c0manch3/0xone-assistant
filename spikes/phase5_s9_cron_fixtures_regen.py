"""Regenerate `phase5_s9_cron_fixtures.json` with 20+ expressions.

Closes G-W2-3 (wave-2). Computes expected-fires using a minimal, self-contained
cron matcher implemented right here — NO import from src/assistant — so the
fixture file is a source of truth INDEPENDENT of the production parser. The
production parser will be tested against this file.

The matcher implements 5-field POSIX cron with:
  * `*` wildcard
  * integer literal
  * comma list
  * range (a-b)
  * step (*/n, a-b/n)
  * POSIX OR semantics for dom/dow when both are non-*
  * DOW: 0=Sun..6=Sat; 7 is INVALID (reject)
  * DOM values > days-in-month simply never match (e.g. Feb 31)

Run:
    uv run python spikes/phase5_s9_cron_fixtures_regen.py
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CronExpr:
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_star: bool
    dow_star: bool


_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),  # Sun=0..Sat=6 — 7 is REJECTED (convention)
}


def _parse_field(token: str, field: str) -> frozenset[int]:
    lo, hi = _RANGES[field]

    def expand_range(s: str) -> list[int]:
        step = 1
        if "/" in s:
            s, step_s = s.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"bad step /{step_s} in {field}")
        if s == "*":
            a, b = lo, hi
        elif "-" in s:
            a_s, b_s = s.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(s)
        if a < lo or b > hi:
            raise ValueError(f"{field} range {a}-{b} out of {lo}-{hi}")
        return list(range(a, b + 1, step))

    values: set[int] = set()
    for piece in token.split(","):
        values.update(expand_range(piece))
    return frozenset(values)


def parse_cron(expr: str) -> CronExpr:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"expected 5 fields, got {len(parts)}: {expr!r}")
    m, h, dom, mon, dow = parts
    return CronExpr(
        minute=_parse_field(m, "minute"),
        hour=_parse_field(h, "hour"),
        dom=_parse_field(dom, "dom"),
        month=_parse_field(mon, "month"),
        dow=_parse_field(dow, "dow"),
        dom_star=dom == "*",
        dow_star=dow == "*",
    )


def matches(expr: CronExpr, dt: datetime) -> bool:
    """POSIX rules: if dom or dow is `*`, the other field's match is required;
    if both non-`*`, match on OR."""
    if dt.minute not in expr.minute:
        return False
    if dt.hour not in expr.hour:
        return False
    if dt.month not in expr.month:
        return False
    # day check
    dom_ok = dt.day in expr.dom
    # Python weekday(): Mon=0..Sun=6 → convert to cron Sun=0..Sat=6
    cron_dow = (dt.weekday() + 1) % 7
    dow_ok = cron_dow in expr.dow
    if expr.dom_star and expr.dow_star:
        return True
    if expr.dom_star:
        return dow_ok
    if expr.dow_star:
        return dom_ok
    return dom_ok or dow_ok


def all_fires(expr: str, start: datetime, end: datetime) -> list[str]:
    ce = parse_cron(expr)
    out: list[str] = []
    cur = start
    step = timedelta(minutes=1)
    while cur < end:
        if matches(ce, cur):
            out.append(cur.strftime("%Y-%m-%dT%H:%MZ"))
        cur += step
    return out


# Default 24-hour window (preserved from wave-1 fixtures, Wednesday).
W1_START = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
W1_END = W1_START + timedelta(hours=24)

# Extra windows for weekday / leap-year / DOM-impossible probes.
SUN_START = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)   # Sunday
SUN_END = SUN_START + timedelta(hours=24)
SAT_START = datetime(2026, 4, 18, 0, 0, tzinfo=UTC)   # Saturday
SAT_END = SAT_START + timedelta(hours=24)
FEB_LEAP_START = datetime(2024, 2, 1, 0, 0, tzinfo=UTC)
FEB_LEAP_END = datetime(2024, 3, 1, 0, 0, tzinfo=UTC)
FEB_NOLEAP_START = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
FEB_NOLEAP_END = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)


def compact(fires: list[str], head: int = 15, tail: int = 5) -> list[str]:
    if len(fires) <= head + tail + 1:
        return fires
    return fires[:head] + ["..."] + fires[-tail:]


CASES: list[dict] = [
    # Existing 8 (re-regenerated to confirm parity).
    {"expr": "0 9 * * *", "label": "daily at 09:00",
     "window": (W1_START, W1_END)},
    {"expr": "*/15 * * * *", "label": "every 15 min",
     "window": (W1_START, W1_END)},
    {"expr": "0 9 * * 1-5", "label": "weekdays 09:00",
     "window": (W1_START, W1_END)},
    {"expr": "30 0 1,15 * *", "label": "days 1+15, 00:30",
     "window": (W1_START, W1_END)},
    {"expr": "0 9 1 * *", "label": "day-1 09:00",
     "window": (W1_START, W1_END)},
    {"expr": "*/5 * * * *", "label": "every 5 min",
     "window": (W1_START, W1_END)},
    {"expr": "0 0 * * 0", "label": "sundays midnight (no Sunday in window)",
     "window": (W1_START, W1_END)},
    {"expr": "15,45 * * * 6", "label": "saturdays :15 and :45 (no Saturday in window)",
     "window": (W1_START, W1_END)},

    # ── NEW wave-2 fixtures ──────────────────────────────────────────
    {"expr": "0 0 * * 0", "label": "sundays midnight — real Sunday window",
     "window": (SUN_START, SUN_END)},
    {"expr": "15,45 * * * 6", "label": "saturdays :15+:45 — real Saturday",
     "window": (SAT_START, SAT_END)},
    {"expr": "0 9 1,15 * *", "label": "two DOM values (1 and 15) at 09:00",
     "window": (datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 30, 0, 0, tzinfo=UTC))},
    {"expr": "*/7 * * * *", "label": "step 7 — non-even division",
     "window": (W1_START, W1_START + timedelta(hours=1))},
    {"expr": "*/15 9-17 * * 1-5", "label": "step + range + list combo (biz hours)",
     "window": (W1_START, W1_END)},
    {"expr": "* * 31 2 *", "label": "Feb 31 — impossible DOM (zero fires)",
     "window": (FEB_NOLEAP_START, FEB_NOLEAP_END)},
    {"expr": "0 9 29 2 *", "label": "Feb 29 09:00 — LEAP year 2024",
     "window": (FEB_LEAP_START, FEB_LEAP_END)},
    {"expr": "0 9 29 2 *", "label": "Feb 29 09:00 — NON-leap 2026 (zero fires)",
     "window": (FEB_NOLEAP_START, FEB_NOLEAP_END)},
    {"expr": "0 12 1 1 *", "label": "Jan 1 noon — new-year anchor",
     "window": (datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                datetime(2026, 1, 2, 0, 0, tzinfo=UTC))},
    {"expr": "0,30 * * * *", "label": "on the hour + half-hour",
     "window": (W1_START, W1_START + timedelta(hours=3))},
    {"expr": "0-5 0 * * *", "label": "first six minutes after midnight",
     "window": (W1_START - timedelta(hours=8),
                W1_START - timedelta(hours=8) + timedelta(days=1))},
    {"expr": "59 23 31 12 *", "label": "new-year-eve last minute",
     "window": (datetime(2026, 12, 31, 23, 0, tzinfo=UTC),
                datetime(2027, 1, 1, 0, 0, tzinfo=UTC))},
    {"expr": "0 9 * * 1,3,5", "label": "Mon/Wed/Fri 09:00",
     "window": (datetime(2026, 4, 13, 0, 0, tzinfo=UTC),
                datetime(2026, 4, 20, 0, 0, tzinfo=UTC))},
    {"expr": "0 */4 * * *", "label": "every 4 hours on the hour",
     "window": (W1_START, W1_END)},
]

# Invalid expressions — parser MUST reject (fixture asserts 'reject': True).
INVALID = [
    {"expr": "0 9 * * 7", "label": "DOW 7 — cron convention 0-6 only"},
    {"expr": "60 * * * *", "label": "minute 60 out of range"},
    {"expr": "* * * * MON", "label": "letter name unsupported"},
    {"expr": "0 9 * *", "label": "only 4 fields"},
    {"expr": "*/0 * * * *", "label": "step /0 invalid"},
]


def main() -> None:
    fixtures: dict[str, dict] = {}
    for idx, case in enumerate(CASES):
        expr = case["expr"]
        start, end = case["window"]
        fires = all_fires(expr, start, end)
        key = f"{idx:02d}_{expr}_{case['label']}"
        fixtures[key] = {
            "expr": expr,
            "label": case["label"],
            "window_start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fires_in_window": compact(fires),
            "total_fires": len(fires),
        }
    fixtures["_invalid_cases"] = INVALID

    out = Path(__file__).with_name("phase5_s9_cron_fixtures.json")
    out.write_text(json.dumps(fixtures, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out} with {len(CASES)} valid + {len(INVALID)} invalid cases")


if __name__ == "__main__":
    main()
