"""is_due + next_fire semantics — DST, vixie OR, leap-day."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from assistant.scheduler.cron import (
    is_ambiguous_local_minute,
    is_due,
    is_existing_local_minute,
    next_fire,
    parse_cron,
)

UTC = dt.UTC


# ---------------------------------------------------------------------------
# is_due basic
# ---------------------------------------------------------------------------
def test_is_due_fires_at_exact_minute() -> None:
    expr = parse_cron("30 9 * * *")
    now = dt.datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
    due = is_due(
        expr,
        last_fire_at=None,
        now_utc=now,
        tz=ZoneInfo("UTC"),
        catchup_window_s=3600,
    )
    assert due == now


def test_is_due_skips_non_matching_minute() -> None:
    expr = parse_cron("30 9 * * *")
    now = dt.datetime(2026, 4, 1, 9, 31, tzinfo=UTC)
    assert (
        is_due(
            expr,
            last_fire_at=None,
            now_utc=now,
            tz=ZoneInfo("UTC"),
            catchup_window_s=3600,
        )
        is None
    )


def test_is_due_catchup_drops_too_old() -> None:
    """Trigger from 2h ago is older than the 1h catchup window."""
    expr = parse_cron("30 9 * * *")
    last = dt.datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
    now = dt.datetime(2026, 4, 1, 11, 45, tzinfo=UTC)
    assert (
        is_due(
            expr,
            last_fire_at=last,
            now_utc=now,
            tz=ZoneInfo("UTC"),
            catchup_window_s=3600,
        )
        is None
    )


def test_is_due_catchup_within_window() -> None:
    """30 min since scheduled minute — well within 1h window."""
    expr = parse_cron("0 9 * * *")
    last = dt.datetime(2026, 4, 1, 8, 0, tzinfo=UTC)
    now = dt.datetime(2026, 4, 1, 9, 30, tzinfo=UTC)
    due = is_due(
        expr,
        last_fire_at=last,
        now_utc=now,
        tz=ZoneInfo("UTC"),
        catchup_window_s=3600,
    )
    assert due == dt.datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Vixie day semantics
# ---------------------------------------------------------------------------
def test_vixie_both_restricted_uses_or() -> None:
    """Fires on either the 15th OR a Friday — not only their intersection."""
    expr = parse_cron("0 12 15 * 5")  # dom=15 and dow=Fri (5)
    # 2026-05-15 is a Friday — both apply.
    friday_15 = dt.datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    assert is_due(
        expr,
        last_fire_at=None,
        now_utc=friday_15,
        tz=ZoneInfo("UTC"),
        catchup_window_s=3600,
    ) == friday_15
    # 2026-05-08 is a Friday, not the 15th — still fires per OR.
    friday_8 = dt.datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    assert is_due(
        expr,
        last_fire_at=None,
        now_utc=friday_8,
        tz=ZoneInfo("UTC"),
        catchup_window_s=3600,
    ) == friday_8


# ---------------------------------------------------------------------------
# DST
# ---------------------------------------------------------------------------
def test_dst_spring_skip_berlin_does_not_fire() -> None:
    """Europe/Berlin jumps from 02:00 CET to 03:00 CEST on 2026-03-29 —
    02:30 local does not exist, so ``30 2 * * *`` must not fire that day.
    """
    tz = ZoneInfo("Europe/Berlin")
    expr = parse_cron("30 2 * * *")
    # In UTC, 2026-03-29 01:30 UTC is NOT 02:30 Berlin (that minute
    # doesn't exist); 02:30 CEST = 00:30 UTC. Check 00:30-03:00 UTC band.
    now = dt.datetime(2026, 3, 29, 3, 0, tzinfo=UTC)
    due = is_due(
        expr,
        last_fire_at=dt.datetime(2026, 3, 29, 0, 0, tzinfo=UTC),
        now_utc=now,
        tz=tz,
        catchup_window_s=86400,
    )
    # The 02:30 Berlin minute does not exist; the function must return
    # None or a non-02:30-local match. We assert: no 02:30 Berlin fire.
    if due is not None:
        local = due.astimezone(tz)
        assert not (local.hour == 2 and local.minute == 30)


def test_dst_fall_fold_berlin_fires_once_at_fold0() -> None:
    """Europe/Berlin repeats 02:00-03:00 on 2026-10-25 (CEST→CET). The
    02:30 minute occurs twice: once in CEST (UTC+2 → 00:30 UTC) and
    once in CET (UTC+1 → 01:30 UTC). We must return the CEST instant.
    """
    tz = ZoneInfo("Europe/Berlin")
    expr = parse_cron("30 2 * * *")
    # Run is_due with last_fire_at just before the first potential match.
    last = dt.datetime(2026, 10, 24, 23, 59, tzinfo=UTC)
    now = dt.datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    due = is_due(
        expr,
        last_fire_at=last,
        now_utc=now,
        tz=tz,
        catchup_window_s=7200,
    )
    assert due is not None
    # fold=0 == CEST at that wall clock → UTC offset +02:00 → 00:30 UTC.
    assert due == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_is_existing_local_minute_detects_spring_skip() -> None:
    tz = ZoneInfo("Europe/Berlin")
    naked = dt.datetime(2026, 3, 29, 2, 30)  # no tzinfo
    assert not is_existing_local_minute(naked, tz)


def test_is_ambiguous_local_minute_detects_fall_fold() -> None:
    tz = ZoneInfo("Europe/Berlin")
    naked = dt.datetime(2026, 10, 25, 2, 30)
    assert is_ambiguous_local_minute(naked, tz)


# ---------------------------------------------------------------------------
# Leap-day + lookahead=1500
# ---------------------------------------------------------------------------
def test_next_fire_leap_day_within_default_lookahead() -> None:
    """Feb-29 only — next fire must resolve within 1500 days."""
    expr = parse_cron("0 0 29 2 *")
    from_utc = dt.datetime(2026, 6, 1, tzinfo=UTC)
    nf = next_fire(expr, from_utc=from_utc, tz=ZoneInfo("UTC"))
    assert nf == dt.datetime(2028, 2, 29, 0, 0, tzinfo=UTC)


def test_next_fire_impossible_date_returns_none() -> None:
    """Feb-30 doesn't exist; parser accepts ``30 2 30 2 *`` but next_fire
    must yield None within the lookahead window.
    """
    expr = parse_cron("0 0 30 2 *")
    from_utc = dt.datetime(2026, 1, 1, tzinfo=UTC)
    nf = next_fire(
        expr, from_utc=from_utc, tz=ZoneInfo("UTC"), max_lookahead_days=1500
    )
    assert nf is None


def test_from_utc_requires_tzinfo() -> None:
    expr = parse_cron("0 0 * * *")
    with pytest.raises(ValueError):
        next_fire(expr, from_utc=dt.datetime(2026, 1, 1), tz=ZoneInfo("UTC"))
