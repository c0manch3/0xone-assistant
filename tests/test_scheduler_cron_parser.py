"""Phase-5 cron parser — 22 valid + 5 invalid fixtures per plan §E/H.

These tests MUST be pure-logic / no DB / no asyncio (they're sync tests).
"""

from __future__ import annotations

import pytest

from assistant.scheduler.cron import (
    CronParseError,
    parse_cron,
)

VALID_FIXTURES: list[tuple[str, str]] = [
    ("every minute", "* * * * *"),
    ("top of every hour", "0 * * * *"),
    ("daily 9am", "0 9 * * *"),
    ("weekdays 9am", "0 9 * * 1-5"),
    ("every 15 min", "*/15 * * * *"),
    ("every 5 min", "*/5 * * * *"),
    ("two specific hours", "0 9,18 * * *"),
    ("weekend 9pm Sun+Sat", "0 21 * * 0,6"),
    ("weekend 9pm Sun alias=7", "0 21 * * 6,7"),
    ("first of month 14:30", "30 14 1 * *"),
    ("last valid dom=31", "0 0 31 * *"),
    ("feb leap", "0 0 29 2 *"),
    ("range steps", "2-10/2 * * * *"),
    ("full range hour", "0 0-23 * * *"),
    ("every 7 minutes", "*/7 * * * *"),
    ("weekdays 9-17 every 15m", "0-45/15 9-17 * * 1-5"),
    ("noon Mondays", "0 12 * * 1"),
    ("month range", "0 0 1 1-3 *"),
    ("dom list", "0 0 1,15 * *"),
    ("daily midnight", "0 0 * * *"),
    ("5am every other day", "0 5 */2 * *"),
    ("new year's day noon", "0 12 1 1 *"),
]


INVALID_FIXTURES: list[tuple[str, str]] = [
    ("minute out of range", "60 * * * *"),
    ("alias MON", "0 9 * * MON"),
    ("@daily", "@daily"),
    ("Quartz L", "0 0 L * *"),
    ("wrong field count", "0 9 *"),
]


@pytest.mark.parametrize("label,expr", VALID_FIXTURES)
def test_parse_cron_valid(label: str, expr: str) -> None:
    del label
    got = parse_cron(expr)
    # Smoke: parser produced a non-empty minute field.
    assert got.minute


@pytest.mark.parametrize("label,expr", INVALID_FIXTURES)
def test_parse_cron_invalid(label: str, expr: str) -> None:
    del label
    with pytest.raises(CronParseError):
        parse_cron(expr)


def test_parse_cron_dow_7_normalised_to_0() -> None:
    expr = parse_cron("0 0 * * 7")
    assert expr.dow == frozenset({0})


def test_parse_cron_raw_star_preserved() -> None:
    both_star = parse_cron("0 9 * * *")
    assert both_star.raw_dom_star and both_star.raw_dow_star
    dom_restricted = parse_cron("0 9 1-5 * *")
    assert not dom_restricted.raw_dom_star and dom_restricted.raw_dow_star


def test_parse_cron_empty_raises() -> None:
    with pytest.raises(CronParseError):
        parse_cron("")


def test_parse_cron_non_string_raises() -> None:
    with pytest.raises(CronParseError):
        parse_cron(123)  # type: ignore[arg-type]
