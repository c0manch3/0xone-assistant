"""Phase 5 / commit 2 — cron parser unit matrix.

Covers the supported subset (see `scheduler/cron.py` docstring) plus the
error surface. 30+ cases, organised into allow/reject blocks so a single
regression is easy to locate.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from assistant.scheduler.cron import (
    CronExpr,
    CronParseError,
    matches_local,
    parse_cron,
)

# ---------------------------------------------------------------- ALLOW

ALLOW_CASES: list[tuple[str, int, int]] = [
    # (expr, expected_minute_set_size, expected_hour_set_size)
    ("* * * * *", 60, 24),
    ("0 * * * *", 1, 24),
    ("0 9 * * *", 1, 1),
    ("*/15 * * * *", 4, 24),
    ("*/5 * * * *", 12, 24),
    ("0,30 * * * *", 2, 24),
    ("1,2,3 * * * *", 3, 24),
    ("1-5 * * * *", 5, 24),
    ("0-5 0 * * *", 6, 1),
    ("0 9-17 * * *", 1, 9),
    ("0 */4 * * *", 1, 6),  # hours 0,4,8,12,16,20
    ("*/7 * * * *", 9, 24),  # 0,7,14,21,28,35,42,49,56
    ("0 9 1,15 * *", 1, 1),
    ("0 9 1-5 * *", 1, 1),
    ("0 9 * * 1-5", 1, 1),
    ("0 9 * * 0", 1, 1),  # Sunday
    ("0 9 * * 6", 1, 1),  # Saturday
    ("15,45 * * * 6", 2, 24),
    ("0 0 * * 0", 1, 1),
    ("0 0 1 1 *", 1, 1),
    ("59 23 31 12 *", 1, 1),
    ("*/15 9-17 * * 1-5", 4, 9),
    ("0 9 * * 1,3,5", 1, 1),
    ("0,30 9-17 1,15 * *", 2, 9),
]


@pytest.mark.parametrize("expr,n_minute,n_hour", ALLOW_CASES)
def test_cron_parse_allow_matrix(expr: str, n_minute: int, n_hour: int) -> None:
    ce = parse_cron(expr)
    assert isinstance(ce, CronExpr)
    assert len(ce.minute) == n_minute, f"{expr!r}: got minute={sorted(ce.minute)}"
    assert len(ce.hour) == n_hour, f"{expr!r}: got hour={sorted(ce.hour)}"


def test_cron_parse_preserves_raw() -> None:
    ce = parse_cron("  */5 * * * *  ")
    assert ce.raw == "*/5 * * * *"


def test_cron_dow_star_is_full_range() -> None:
    ce = parse_cron("0 * * * *")
    assert ce.day_of_week == frozenset({0, 1, 2, 3, 4, 5, 6})


# ---------------------------------------------------------------- REJECT


REJECT_CASES: list[tuple[str, str]] = [
    ("0 9 * * 7", "day_of_week"),  # cron uses 0..6; 7 not allowed
    ("60 * * * *", "minute"),
    ("* 24 * * *", "hour"),
    ("* * 0 * *", "day_of_month"),  # DOM starts at 1
    ("* * 32 * *", "day_of_month"),
    ("* * * 0 *", "month"),
    ("* * * 13 *", "month"),
    ("* * * * MON", "day_of_week"),  # letter names not supported
    ("* * * * *  extra", "5 fields"),
    ("0 9 * *", "5 fields"),  # only 4 fields
    ("", "empty"),
    ("   ", "empty"),
    ("*/0 * * * *", "step"),
    ("*/-1 * * * *", "step"),
    ("5-1 * * * *", "inverted"),
    ("1,,2 * * * *", "empty atom"),
    ("5/3 * * * *", "step"),  # step on single literal not allowed
    ("a * * * *", "integer"),
    ("1-b * * * *", "integer"),
    ("@daily", "5 fields"),  # shortcuts rejected
]


@pytest.mark.parametrize("expr,needle", REJECT_CASES)
def test_cron_parse_reject_matrix(expr: str, needle: str) -> None:
    with pytest.raises(CronParseError) as exc_info:
        parse_cron(expr)
    # The error message mentions which field / why — cheap regression guard.
    assert needle.lower() in str(exc_info.value).lower(), (
        f"{expr!r}: expected {needle!r} in {exc_info.value!r}"
    )


# ---------------------------------------------------------------- MATCH SEMANTICS


def test_matches_local_minute_hour_month_dom_and() -> None:
    ce = parse_cron("0 9 15 * *")
    # 2026-04-15 09:00 (any DOW)
    assert matches_local(ce, datetime(2026, 4, 15, 9, 0)) is True
    assert matches_local(ce, datetime(2026, 4, 15, 10, 0)) is False
    assert matches_local(ce, datetime(2026, 4, 16, 9, 0)) is False


def test_matches_local_dom_dow_both_restricted_or() -> None:
    """Vixie rule: when BOTH dom and dow are non-star, match on OR."""
    ce = parse_cron("0 9 1 * 1")  # 1st of month OR Monday at 09:00
    # 2026-04-01 was a Wednesday (not Monday), but DOM matches.
    assert matches_local(ce, datetime(2026, 4, 1, 9, 0)) is True
    # 2026-04-06 is Monday, DOW match.
    assert matches_local(ce, datetime(2026, 4, 6, 9, 0)) is True
    # 2026-04-15 matches neither.
    assert matches_local(ce, datetime(2026, 4, 15, 9, 0)) is False


def test_matches_local_dow_python_to_cron_conversion() -> None:
    # 2026-04-19 is Sunday. Python weekday()==6; cron DOW=0.
    ce = parse_cron("0 0 * * 0")
    assert matches_local(ce, datetime(2026, 4, 19, 0, 0)) is True
    # 2026-04-18 is Saturday. Python weekday()==5; cron DOW=6.
    ce_sat = parse_cron("0 0 * * 6")
    assert matches_local(ce_sat, datetime(2026, 4, 18, 0, 0)) is True
    # Monday-Friday range.
    ce_wd = parse_cron("0 9 * * 1-5")
    assert matches_local(ce_wd, datetime(2026, 4, 15, 9, 0)) is True  # Wed
    assert matches_local(ce_wd, datetime(2026, 4, 18, 9, 0)) is False  # Sat
    assert matches_local(ce_wd, datetime(2026, 4, 19, 9, 0)) is False  # Sun
