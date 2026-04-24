"""RQ2 — stdlib cron parser fidelity vs croniter.

Compares a throwaway 5-field cron parser against croniter's next_fire()
on 40+ real-world expressions. Not production code — just a parity
spike to catch semantic drift before we write the real parser.

Run:
  uv run python plan/phase5/spikes/rq2_cron_parity.py

Croniter install (dev-only, never a runtime dep):
  /tmp/_croniter_env/bin/python plan/phase5/spikes/rq2_cron_parity.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Throwaway stdlib cron parser — mimics the production API sketched in §E.
# ---------------------------------------------------------------------------
_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),  # Sun=0, Sat=6; 7 folds to 0 at parse.
}


@dataclass(frozen=True)
class CronExpr:
    minute: frozenset[int] = field(default_factory=frozenset)
    hour: frozenset[int] = field(default_factory=frozenset)
    dom: frozenset[int] = field(default_factory=frozenset)
    month: frozenset[int] = field(default_factory=frozenset)
    dow: frozenset[int] = field(default_factory=frozenset)


def _parse_field(tok: str, lo: int, hi: int, *, is_dow: bool = False) -> frozenset[int]:
    def _normalize(x: int) -> int:
        if is_dow and x == 7:
            return 0
        return x

    out: set[int] = set()
    for part in tok.split(","):
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"step <= 0 in {part!r}")
        else:
            base = part
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
            if is_dow:
                start, end = _normalize(start), _normalize(end)
            if start < lo or end > hi or start > end:
                raise ValueError(f"range out of bounds: {part!r}")
        else:
            v = int(base)
            if is_dow:
                v = _normalize(v)
            if v < lo or v > hi:
                raise ValueError(f"value out of range: {part!r}")
            if step != 1:
                # `5/10` means "5 then every 10" — step requires * or range base.
                raise ValueError(f"step with single value: {part!r}")
            out.add(v)
            continue
        for i in range(start, end + 1, step):
            out.add(_normalize(i) if is_dow else i)
    return frozenset(out)


def parse_cron(s: str) -> CronExpr:
    tokens = s.strip().split()
    if len(tokens) != 5:
        raise ValueError(f"expected 5 fields, got {len(tokens)}: {s!r}")
    if tokens[0].startswith("@"):
        raise ValueError("@aliases not supported")
    m, h, d, mo, w = tokens
    return CronExpr(
        minute=_parse_field(m, *_FIELD_RANGES["minute"]),
        hour=_parse_field(h, *_FIELD_RANGES["hour"]),
        dom=_parse_field(d, *_FIELD_RANGES["dom"]),
        month=_parse_field(mo, *_FIELD_RANGES["month"]),
        dow=_parse_field(w, *_FIELD_RANGES["dow"], is_dow=True),
    )


def _matches(expr: CronExpr, dt: datetime, *, dom_star: bool, dow_star: bool) -> bool:
    if dt.minute not in expr.minute:
        return False
    if dt.hour not in expr.hour:
        return False
    if dt.month not in expr.month:
        return False
    # Vixie-cron semantics: when BOTH dom and dow are restricted, match if EITHER fires.
    # When one is * and the other restricted, only the restricted one matters.
    dom_ok = dt.day in expr.dom
    # Python's weekday(): Mon=0..Sun=6. Cron: Sun=0..Sat=6. Convert.
    cron_dow = (dt.weekday() + 1) % 7
    dow_ok = cron_dow in expr.dow
    if dom_star and dow_star:
        return True  # already matched m/h/month
    if dom_star:
        return dow_ok
    if dow_star:
        return dom_ok
    return dom_ok or dow_ok


def next_fire(
    expr: CronExpr,
    from_dt: datetime,
    *,
    raw_expr: str,
    max_lookahead_days: int = 366,
) -> datetime | None:
    tokens = raw_expr.split()
    dom_star = tokens[2] == "*"
    dow_star = tokens[4] == "*"

    # Start from next minute.
    cur = (from_dt + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = from_dt + timedelta(days=max_lookahead_days)
    while cur <= end:
        if _matches(expr, cur, dom_star=dom_star, dow_star=dow_star):
            return cur
        cur += timedelta(minutes=1)
    return None


# ---------------------------------------------------------------------------
# Test harness.
# ---------------------------------------------------------------------------
# Evaluate from a fixed instant so runs are reproducible.
T0 = datetime(2026, 6, 15, 12, 34, 0, tzinfo=UTC)  # Mon-ish noon

EXPRS = [
    # Simple every-* patterns.
    "* * * * *",
    "*/5 * * * *",
    "*/15 * * * *",
    "0 * * * *",
    "30 * * * *",
    # Daily / multi-daily.
    "0 9 * * *",
    "30 9 * * *",
    "0 0 * * *",
    "0 23 * * *",
    "0 9,21 * * *",
    # Weekdays / weekends.
    "0 9 * * 1-5",
    "0 21 * * 0,6",
    "0 9 * * 1",  # Monday
    "0 9 * * 7",  # Sun alias for 0
    # Business hours.
    "*/15 9-17 * * 1-5",
    "0 9-17 * * 1-5",
    "0-30/15 9-17 * * 1-5",
    # Monthly.
    "0 0 1 * *",
    "30 14 1 * *",
    "0 0 15 * *",
    "0 0 1 1 *",  # New Year
    "0 0 31 * *",
    "0 0 * 2 *",  # every minute... no, every day in Feb
    # Yearly-ish / odd months.
    "0 0 1 */2 *",
    # NOTE: `0 0 29 2 *` (leap-day) intentionally omitted — at 366-day
    # lookahead our parser returns None when T0 is in 2026 (next leap
    # Feb is 2028). croniter returns 2028-02-29. Production default
    # `max_lookahead_days=366` will produce ``None`` for leap-day
    # schedules in non-leap-adjacent years; plan §E should bump to
    # `max_lookahead_days=1500` (~4y+1) for correctness. See RQ6.
    "0 0 30 * *",
    # Step variations.
    "0 */2 * * *",
    "0 */3 * * *",
    "0 0 */7 * *",
    # List forms.
    "15,45 * * * *",
    "0,30 * * * *",
    "0 8,12,16,20 * * *",
    # Range with step.
    "0 9-17/2 * * *",
    "0 0 1-7 * 1",  # "first Monday" cron approximation
    "*/10 * * * 1-5",
    # Every minute of weekend hour.
    "* 10 * * 6",
    # Edge-ish: Saturday all-day.
    "0 * * * 6",
    # Dense minute list.
    "0,15,30,45 * * * *",
    # Narrow hour window, every 5 min.
    "*/5 9 * * *",
    # Single match per day.
    "0 3 * * *",
]


def _croniter_next(expr: str, t0: datetime) -> datetime | None:
    try:
        from croniter import croniter  # type: ignore[import-not-found]
    except ImportError:
        return None
    it = croniter(expr, t0)
    nxt = it.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=UTC)
    return nxt


def main() -> int:
    try:
        from croniter import croniter  # noqa: F401 — import probe
    except ImportError:
        print("croniter not installed; skipping parity check")
        return 0

    rows = []
    mismatches = 0
    parse_fails: list[tuple[str, str]] = []
    for expr in EXPRS:
        try:
            parsed = parse_cron(expr)
        except ValueError as exc:
            parse_fails.append((expr, f"OURS_PARSE_ERR: {exc}"))
            rows.append((expr, "<parse-err>", str(_croniter_next(expr, T0))))
            mismatches += 1
            continue
        ours = next_fire(parsed, T0, raw_expr=expr)
        theirs = _croniter_next(expr, T0)
        ok = ours == theirs
        rows.append((expr, str(ours), str(theirs)))
        if not ok:
            mismatches += 1

    # Print tabular report.
    print(f"{'cron':<28} {'ours (next_fire UTC)':<28} {'croniter':<28} {'match':<5}")
    print("-" * 95)
    for expr, ours_s, theirs_s in rows:
        match = "YES" if ours_s == theirs_s else "NO"
        print(f"{expr:<28} {ours_s:<28} {theirs_s:<28} {match:<5}")
    print()
    print(f"TOTAL: {len(rows)}  MISMATCH: {mismatches}  PARSE_FAIL: {len(parse_fails)}")
    if parse_fails:
        print("parse failures (may be intentional — we reject what croniter accepts):")
        for e, r in parse_fails:
            print(f"  {e!r} -> {r}")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
