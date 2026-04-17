"""Phase 5 / commit 2 — `is_due` + DST semantics + S-9 fixture replay.

Three blocks:
  1. Direct semantics: first fire, minute-boundary, catchup window edge.
  2. DST: spring-skip (Europe/Berlin 2026-03-29), fall-back fold-0 policy.
  3. Fixture replay: load `spikes/phase5_s9_cron_fixtures.json` and assert
     that for each expression, walking UTC minute by minute across the
     fixture window produces the recorded fire-set.

The fixture replay uses `matches_local` directly (one minute at a time)
to exercise the matcher on the canonical S-9 data. `is_due` gets its own
targeted coverage in the direct-semantics block, where we can control
`last_fire_at` and `catchup_window_s` precisely.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from assistant.scheduler.cron import is_due, matches_local, parse_cron

_FIXTURES_PATH = Path(__file__).resolve().parents[1] / "spikes" / "phase5_s9_cron_fixtures.json"


# ---------------------------------------------------------------- is_due


_UTC = ZoneInfo("UTC")


def test_is_due_first_fire_at_minute_boundary() -> None:
    expr = parse_cron("0 9 * * *")
    now = datetime(2026, 4, 15, 9, 0, 0, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now, tz=_UTC, catchup_window_s=3600)
    assert t == datetime(2026, 4, 15, 9, 0, tzinfo=UTC)


def test_is_due_mid_minute_tick_still_fires() -> None:
    """Scheduler ticks every 15s; a tick at 09:00:15 with last_fire_at=None
    must still return the 09:00 boundary so the first tick of the minute
    materialises the trigger."""
    expr = parse_cron("0 9 * * *")
    now = datetime(2026, 4, 15, 9, 0, 15, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now, tz=_UTC, catchup_window_s=3600)
    assert t == datetime(2026, 4, 15, 9, 0, tzinfo=UTC)


def test_is_due_same_minute_already_fired_returns_none() -> None:
    expr = parse_cron("0 9 * * *")
    last = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    # Another tick 30s later.
    now = datetime(2026, 4, 15, 9, 0, 30, tzinfo=UTC)
    t = is_due(expr, last_fire_at=last, now=now, tz=_UTC, catchup_window_s=3600)
    assert t is None


def test_is_due_next_day_fires_once() -> None:
    expr = parse_cron("0 9 * * *")
    last = datetime(2026, 4, 15, 9, 0, tzinfo=UTC)
    now = datetime(2026, 4, 16, 9, 1, tzinfo=UTC)
    t = is_due(expr, last_fire_at=last, now=now, tz=_UTC, catchup_window_s=3600)
    assert t == datetime(2026, 4, 16, 9, 0, tzinfo=UTC)


def test_is_due_long_gap_returns_latest_match() -> None:
    """Multi-hour gap with `*/5 * * * *`: within the catchup window we must
    return the LATEST match, not flood every missed minute."""
    expr = parse_cron("*/5 * * * *")
    last = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)
    now = datetime(2026, 4, 15, 9, 2, tzinfo=UTC)  # 62 min after last
    # catchup_window=3600 → lower bound = 08:02; latest match ≤ 09:02 = 09:00.
    t = is_due(expr, last_fire_at=last, now=now, tz=_UTC, catchup_window_s=3600)
    assert t == datetime(2026, 4, 15, 9, 0, tzinfo=UTC)


def test_is_due_outside_catchup_window_returns_none() -> None:
    """A schedule suspended for > catchup_window_s drops missed fires."""
    expr = parse_cron("0 9 * * *")  # daily 09:00
    last = datetime(2026, 4, 14, 9, 0, tzinfo=UTC)
    # 24 h 30 m later; catchup=1h; 09:00 was ~23h30m ago → drop.
    now = datetime(2026, 4, 15, 8, 30, tzinfo=UTC)
    t = is_due(expr, last_fire_at=last, now=now, tz=_UTC, catchup_window_s=3600)
    assert t is None


def test_is_due_catchup_edge_just_inside() -> None:
    expr = parse_cron("0 9 * * *")
    # t = 09:00; now = 09:59 → 59 min ago, inside 3600s window.
    now = datetime(2026, 4, 15, 9, 59, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now, tz=_UTC, catchup_window_s=3600)
    assert t == datetime(2026, 4, 15, 9, 0, tzinfo=UTC)


def test_is_due_catchup_edge_just_outside() -> None:
    expr = parse_cron("0 9 * * *")
    # t = 09:00; now = 10:01 → 61 min ago, outside 3600s window.
    now = datetime(2026, 4, 15, 10, 1, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now, tz=_UTC, catchup_window_s=3600)
    assert t is None


def test_is_due_tz_local_match_not_utc() -> None:
    """`0 9 * * *` with tz=Europe/Berlin should fire at 07:00 UTC in summer
    (UTC+2) and 08:00 UTC in winter (UTC+1)."""
    expr = parse_cron("0 9 * * *")
    berlin = ZoneInfo("Europe/Berlin")
    # Summer — April 15 is DST.
    now_summer = datetime(2026, 4, 15, 8, 0, tzinfo=UTC)  # 10:00 Berlin
    t = is_due(expr, last_fire_at=None, now=now_summer, tz=berlin, catchup_window_s=3600)
    assert t == datetime(2026, 4, 15, 7, 0, tzinfo=UTC)  # 09:00 Berlin summer


def test_is_due_tz_winter_fire() -> None:
    expr = parse_cron("0 9 * * *")
    berlin = ZoneInfo("Europe/Berlin")
    # Winter — January 15 is CET (UTC+1). 09:00 Berlin = 08:00 UTC.
    now_winter = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now_winter, tz=berlin, catchup_window_s=3600)
    assert t == datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


def test_is_due_last_fire_exclusive_boundary() -> None:
    """`last_fire_at` is exclusive: a tick at exactly last_fire_at must not
    fire the same instant again."""
    expr = parse_cron("*/5 * * * *")
    last = datetime(2026, 4, 15, 9, 5, tzinfo=UTC)
    now = datetime(2026, 4, 15, 9, 5, 30, tzinfo=UTC)
    t = is_due(expr, last_fire_at=last, now=now, tz=_UTC, catchup_window_s=3600)
    assert t is None


def test_is_due_naive_now_treated_as_utc() -> None:
    """Defensive: a caller passing a naive datetime should still produce
    a useful answer (we normalise internally)."""
    expr = parse_cron("0 * * * *")
    now_naive = datetime(2026, 4, 15, 9, 0)
    t = is_due(expr, last_fire_at=None, now=now_naive, tz=_UTC, catchup_window_s=3600)
    assert t is not None
    assert t.tzinfo is not None


# ---------------------------------------------------------------- DST

_BERLIN = ZoneInfo("Europe/Berlin")


def test_is_due_dst_spring_skip_non_existent_local_minute() -> None:
    """`30 2 * * *` in Europe/Berlin on 2026-03-29: local 02:30 does NOT
    exist (jump 02:00 → 03:00). Producer must skip that day's fire."""
    expr = parse_cron("30 2 * * *")
    # Now = 2026-03-29 04:00 Berlin = 02:00 UTC.
    now = datetime(2026, 3, 29, 2, 0, tzinfo=UTC)
    # last_fire_at at previous day's fire (2026-03-28 02:30 Berlin =
    # 01:30 UTC winter).
    last = datetime(2026, 3, 28, 1, 30, tzinfo=UTC)
    t = is_due(expr, last_fire_at=last, now=now, tz=_BERLIN, catchup_window_s=3600)
    # Within the catchup window there is no existing UTC minute that
    # projects to local 02:30 on 2026-03-29 → None.
    assert t is None


def test_is_due_dst_fall_fires_once_fold_zero() -> None:
    """`30 2 * * *` in Europe/Berlin on 2026-10-25: local 02:30 exists TWICE
    (summer 02:30 = 00:30 UTC, winter 02:30 = 01:30 UTC). Policy: fire only
    on the first occurrence (fold=0 = summer offset UTC+2)."""
    expr = parse_cron("30 2 * * *")
    # Now = 2026-10-25 03:00 UTC — past both local 02:30s.
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    t = is_due(expr, last_fire_at=None, now=now, tz=_BERLIN, catchup_window_s=10800)
    assert t == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_is_due_returns_none_when_no_match_in_window() -> None:
    """Cron that never matches the iterated UTC minutes in the window → None."""
    expr = parse_cron("0 9 * * 0")  # sundays only
    now = datetime(2026, 4, 15, 10, 0, tzinfo=UTC)  # Wednesday
    t = is_due(expr, last_fire_at=None, now=now, tz=_UTC, catchup_window_s=3600)
    assert t is None


# ---------------------------------------------------------------- S-9 FIXTURES


def _load_fixtures() -> dict[str, dict[str, object]]:
    with _FIXTURES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _fires_by_matcher(expr_str: str, window_start: datetime, window_end: datetime) -> list[str]:
    """Walk UTC minutes across [start, end) and collect `expr_str` matches.

    Returned in the same `YYYY-MM-DDTHH:MMZ` shape as the fixture file.
    """
    ce = parse_cron(expr_str)
    fires: list[str] = []
    t = window_start
    step = timedelta(minutes=1)
    while t < window_end:
        naked_local = t.astimezone(_UTC).replace(tzinfo=None)
        if matches_local(ce, naked_local):
            fires.append(t.strftime("%Y-%m-%dT%H:%MZ"))
        t += step
    return fires


def _parse_window_dt(s: str) -> datetime:
    """Parse `2026-04-15T08:00:00Z` to tz-aware UTC."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(UTC)


VALID_FIXTURE_IDS = [k for k in _load_fixtures() if not k.startswith("_")]


@pytest.mark.parametrize("fixture_id", VALID_FIXTURE_IDS)
def test_cron_fixture_matches_recorded_fires(fixture_id: str) -> None:
    """For each S-9 valid fixture, walk the window and verify matches.

    Fixture `fires_in_window` may be abbreviated with a `...` marker when
    `total_fires` is large. We verify:
      * `total_fires` exactly.
      * Explicit entries (non-`...`) appear in the computed fire-set.
    """
    fixture = _load_fixtures()[fixture_id]
    expr = str(fixture["expr"])
    start = _parse_window_dt(str(fixture["window_start_utc"]))
    end = _parse_window_dt(str(fixture["window_end_utc"]))
    fires = _fires_by_matcher(expr, start, end)
    recorded = [str(e) for e in fixture.get("fires_in_window", [])]
    total = int(fixture["total_fires"])  # type: ignore[arg-type]
    assert len(fires) == total, (
        f"{fixture_id}: expected {total} fires, got {len(fires)} "
        f"({fires[:3]}...{fires[-3:] if len(fires) > 6 else ''})"
    )
    for entry in recorded:
        if entry == "...":
            continue
        assert entry in fires, f"{fixture_id}: missing recorded fire {entry}"


def test_invalid_fixtures_all_reject() -> None:
    """Every expression in the `_invalid_cases` block must raise
    CronParseError — keeps parser defence in step with the spike table."""
    invalids = _load_fixtures()["_invalid_cases"]
    assert isinstance(invalids, list)
    for entry in invalids:
        expr = str(entry["expr"])  # type: ignore[index]
        with pytest.raises(Exception) as exc_info:
            parse_cron(expr)
        assert "cron" in type(exc_info.value).__name__.lower() or isinstance(
            exc_info.value, ValueError
        ), f"{expr!r}: unexpected exception {exc_info.value!r}"
