"""RQ6 — cron edge cases (bonus).

Tests our throwaway parser + croniter on:
  - Feb 30 / Feb 31 (impossible dates) → never fire.
  - `* * 31 */2 *` (31st of some months only).
  - `0 0 * * 7` (Sunday alias for 0).
  - Out-of-range values (60 * * * *, 25 * * * *, 0 -1 * * *) → reject.

Run:
  /tmp/_croniter_env/bin/python plan/phase5/spikes/rq6_cron_edge.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from rq2_cron_parity import next_fire, parse_cron  # noqa: E402


UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _try_parse(expr: str) -> tuple[bool, str]:
    try:
        parse_cron(expr)
        return True, "ok"
    except ValueError as exc:
        return False, str(exc)


def _try_cron_next(expr: str, t0: datetime) -> tuple[bool, str]:
    try:
        from croniter import croniter  # type: ignore[import-not-found]
        c = croniter(expr, t0)
        nxt = c.get_next(datetime)
        return True, nxt.isoformat()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def main() -> int:
    # Impossible / tricky expressions.
    cases = [
        ("* * 30 2 *", "Feb 30 — impossible"),
        ("0 0 31 2 *", "Feb 31 — impossible"),
        ("0 0 31 */2 *", "31st of every other month — some months skip"),
        ("0 0 * * 7", "Sunday alias for 0"),
        ("0 0 * * 0", "Sunday explicit"),
        ("0 0 29 2 *", "leap-day — only fires in leap years"),
    ]
    print("=== Tricky expressions ===")
    for expr, note in cases:
        ours_parsed, ours_msg = _try_parse(expr)
        their_ok, their_msg = _try_cron_next(expr, T0)
        ours_next = "N/A"
        if ours_parsed:
            try:
                parsed = parse_cron(expr)
                r = next_fire(parsed, T0, raw_expr=expr, max_lookahead_days=400 * 4)
                ours_next = r.isoformat() if r else "None"
            except Exception as exc:
                ours_next = f"ERR {exc}"
        print(f"{expr!r:<28} {note}")
        print(f"   ours.parse={ours_parsed} ours.next={ours_next}")
        print(f"   croniter: ok={their_ok} msg={their_msg}")

    # Out-of-range — we expect ours to REJECT at parse time.
    print("\n=== Out-of-range — should reject ===")
    bad = [
        "60 * * * *",  # minute max is 59
        "25 * * * *",  # minute 25 is fine; included as sanity check
        "* 24 * * *",  # hour max is 23
        "0 -1 * * *",  # negative
        "0 0 32 * *",  # day 32 impossible
        "0 0 0 * *",  # day 0 invalid (min 1)
        "0 0 * 13 *",  # month 13
        "0 0 * * 8",  # dow 8 invalid
        "@daily",  # alias — we reject per plan
        "* * * *",  # 4 fields
        "* * * * * *",  # 6 fields
    ]
    for expr in bad:
        ok, msg = _try_parse(expr)
        verdict = "ACCEPT" if ok else "REJECT"
        print(f"{expr!r:<22} ours={verdict}  ({msg})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
