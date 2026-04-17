"""POSIX 5-field cron parser + `is_due` with DST-aware semantics (phase 5).

Scope — intentionally narrow:
  * 5 fields: `minute hour day_of_month month day_of_week`.
  * Syntax: `*`, integer literal, comma list, range `a-b`, step `*/n`, `a-b/n`.
  * NO letter names (`JAN`, `MON`), NO shortcuts (`@daily`, `@reboot`).
  * `day_of_week` uses the cron convention `0=Sun..6=Sat`; **`7` is REJECTED**
    at parse time (unlike some extended cron dialects that accept both `0`
    and `7` for Sunday).

DST handling (spike S-3):
  * Producer iterates UTC minute boundaries. A UTC instant either maps to
    exactly one existing local minute or NO local minute (spring-forward
    skip). Fall-back ambiguous local minutes map to TWO distinct UTC
    instants; we match only the first (`fold=0`) — consistent with cron's
    historical "fire once per day" contract.

`is_due` iterates backward from `now` to the last-fire anchor, returning
the newest candidate that passes all four gates (match, ordering,
catch-up window, fold=0). That makes "long suspend" semantics correct:
we fire on the most recent intended boundary instead of spamming every
missed minute.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_MINUTE = (0, 59)
_HOUR = (0, 23)
_DOM = (1, 31)
_MONTH = (1, 12)
_DOW = (0, 6)  # cron convention: 0=Sun..6=Sat. 7 is NOT accepted.


class CronParseError(ValueError):
    """Malformed 5-field cron expression."""


@dataclass(frozen=True, slots=True)
class CronExpr:
    """Parsed 5-field cron expression.

    Each field is a frozenset of accepted integers in its domain. `raw`
    preserves the input string verbatim for structured-log attribution.
    """

    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    raw: str


def _parse_field(raw: str, lo: int, hi: int, *, field_name: str) -> frozenset[int]:
    """Parse one cron field into the concrete set of accepted values.

    Each comma-separated atom is one of:
        `*`             → full range
        `N`             → single literal in [lo, hi]
        `A-B`           → inclusive range
        `*/S`           → every S-th value across full range
        `A-B/S`         → every S-th value within [A, B]

    Any other shape (letter names, negative numbers, whitespace inside
    the atom) raises `CronParseError`.
    """
    if not raw:
        raise CronParseError(f"{field_name}: empty field")
    values: set[int] = set()
    for atom in raw.split(","):
        if not atom:
            raise CronParseError(f"{field_name}: empty atom in {raw!r}")
        values.update(_parse_atom(atom, lo, hi, field_name=field_name))
    if not values:
        # Defence in depth — _parse_atom is expected to have raised.
        raise CronParseError(f"{field_name}: no values parsed from {raw!r}")
    return frozenset(values)


def _parse_atom(atom: str, lo: int, hi: int, *, field_name: str) -> set[int]:
    step = 1
    if "/" in atom:
        body, step_s = atom.split("/", 1)
        try:
            step = int(step_s)
        except ValueError as exc:
            raise CronParseError(f"{field_name}: step not an integer in {atom!r}") from exc
        if step <= 0:
            raise CronParseError(f"{field_name}: step must be > 0 in {atom!r}")
    else:
        body = atom

    if body == "*":
        start, end = lo, hi
    elif "-" in body:
        try:
            a_s, b_s = body.split("-", 1)
            a, b = int(a_s), int(b_s)
        except ValueError as exc:
            raise CronParseError(f"{field_name}: range not two integers in {atom!r}") from exc
        if not (lo <= a <= hi and lo <= b <= hi):
            raise CronParseError(f"{field_name}: range {a}-{b} outside [{lo}, {hi}] in {atom!r}")
        if a > b:
            raise CronParseError(f"{field_name}: inverted range in {atom!r}")
        start, end = a, b
    else:
        try:
            v = int(body)
        except ValueError as exc:
            raise CronParseError(f"{field_name}: not an integer in {atom!r}") from exc
        if not (lo <= v <= hi):
            raise CronParseError(f"{field_name}: {v} outside [{lo}, {hi}] in {atom!r}")
        # Single literal with no step is just {v}; step on a single literal
        # (e.g. `5/3`) is non-standard — reject.
        if "/" in atom:
            raise CronParseError(f"{field_name}: step only allowed on `*` or range in {atom!r}")
        return {v}

    return {i for i in range(start, end + 1) if (i - start) % step == 0}


def parse_cron(expr: str) -> CronExpr:
    """Parse a 5-field POSIX cron expression.

    Raises `CronParseError` on any deviation from the supported grammar;
    see module docstring for the exact subset.
    """
    stripped = expr.strip()
    if not stripped:
        raise CronParseError("empty expression")
    fields = stripped.split()
    if len(fields) != 5:
        raise CronParseError(f"expected 5 fields, got {len(fields)} in {expr!r}")
    minute = _parse_field(fields[0], *_MINUTE, field_name="minute")
    hour = _parse_field(fields[1], *_HOUR, field_name="hour")
    dom = _parse_field(fields[2], *_DOM, field_name="day_of_month")
    month = _parse_field(fields[3], *_MONTH, field_name="month")
    dow = _parse_field(fields[4], *_DOW, field_name="day_of_week")
    return CronExpr(
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month=month,
        day_of_week=dow,
        raw=stripped,
    )


def _is_star(field: frozenset[int], domain: tuple[int, int]) -> bool:
    """True iff `field` covers the entire domain — i.e. was written as `*`.

    Round-trip detection is fine because `_parse_field` guarantees `field`
    is a strict subset of `[lo, hi]`.
    """
    lo, hi = domain
    return len(field) == (hi - lo + 1)


def matches_local(expr: CronExpr, dt_local: datetime) -> bool:
    """POSIX match semantics for `dt_local` in cron's wall-clock view.

    * minute / hour / month must all match literally.
    * day-of-month and day-of-week are OR-joined when BOTH are non-`*`,
      AND-joined otherwise — this mirrors Vixie cron's historical rule
      (``man 5 crontab``).
    * Python `datetime.weekday()` returns Mon=0..Sun=6; we convert to
      cron Sun=0..Sat=6 via `(weekday + 1) % 7`.
    """
    if dt_local.minute not in expr.minute:
        return False
    if dt_local.hour not in expr.hour:
        return False
    if dt_local.month not in expr.month:
        return False
    cron_dow = (dt_local.weekday() + 1) % 7
    dom_star = _is_star(expr.day_of_month, _DOM)
    dow_star = _is_star(expr.day_of_week, _DOW)
    dom_ok = dt_local.day in expr.day_of_month
    dow_ok = cron_dow in expr.day_of_week
    if dom_star and dow_star:
        return True
    if not dom_star and not dow_star:
        # Vixie: either-or match when both are restricted.
        return dom_ok or dow_ok
    if dom_star:
        return dow_ok
    return dom_ok


def is_existing_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """DST spring-skip detector (spike S-3).

    A non-existent local minute (e.g. 02:30 on Europe/Berlin 2026-03-29)
    fails the round-trip: building a tz-aware datetime from the naked
    wall-clock representation, converting to UTC, and back yields a
    DIFFERENT naked datetime (Python interprets the skipped minute as the
    post-transition offset, so it moves forward by the DST delta).
    """
    for fold in (0, 1):
        aware = naked.replace(tzinfo=tz, fold=fold)
        back = aware.astimezone(ZoneInfo("UTC")).astimezone(tz).replace(tzinfo=None)
        if back == naked.replace(fold=0):
            return True
    return False


def is_ambiguous_local_minute(naked: datetime, tz: ZoneInfo) -> bool:
    """DST fall-back detector (spike S-3).

    A fall-back ambiguous minute (e.g. 02:30 on Europe/Berlin 2026-10-25)
    exists TWICE; the two folds have different UTC offsets.
    """
    a = naked.replace(tzinfo=tz, fold=0)
    b = naked.replace(tzinfo=tz, fold=1)
    return a.utcoffset() != b.utcoffset() and is_existing_local_minute(naked, tz)


def is_due(
    expr: CronExpr,
    last_fire_at: datetime | None,
    now: datetime,
    tz: ZoneInfo,
    catchup_window_s: int = 3600,
) -> datetime | None:
    """Return the newest UTC minute-boundary `t` that is "due to fire", or None.

    Gates (ALL must hold):
      (a) `last_fire_at < t <= now`
      (b) `t` converted into `tz` matches `expr` on all five fields.
      (c) The local minute for `t` is NOT skipped by spring-forward and
          uses `fold == 0` (fall-back ambiguity fires only on the first
          occurrence — plan §2.2).
      (d) `now - t <= catchup_window_s`.

    Iteration: walks UTC minutes backward from `now` (floored to the minute)
    to the lower bound `max(last_fire_at + 1min, now - catchup_window_s)`.
    Newest-wins semantics: the first candidate that passes all gates is
    returned. This makes "we just woke from suspend, last fire was 1h ago,
    cron says every 5 min" collapse to a single fire at the latest match.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) != timedelta(0):
        # Defensive — callers already pass `datetime.now(UTC)`.
        now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)

    now_floor = now.replace(second=0, microsecond=0)

    lower_bound = now_floor - timedelta(seconds=catchup_window_s)
    if last_fire_at is not None:
        anchor = last_fire_at.replace(tzinfo=UTC) if last_fire_at.tzinfo is None else last_fire_at
        anchor = anchor.astimezone(UTC)
        candidate_lower = anchor + timedelta(minutes=1)
        if candidate_lower > lower_bound:
            lower_bound = candidate_lower

    # Walk newest first.
    t = now_floor
    while t >= lower_bound:
        if t > now:
            t -= timedelta(minutes=1)
            continue
        naked_local = t.astimezone(tz).replace(tzinfo=None)
        # fold-0 policy: skip UTC minutes that correspond to fall-back's
        # second occurrence. The pre-transition UTC minute maps to fold-0.
        local_aware = t.astimezone(tz)
        if local_aware.fold != 0:
            t -= timedelta(minutes=1)
            continue
        if not is_existing_local_minute(naked_local, tz):
            # Spring-forward skipped minute; a UTC minute will never actually
            # map here (Python normalises forward), but guard defensively.
            t -= timedelta(minutes=1)
            continue
        if matches_local(expr, naked_local):
            return t
        t -= timedelta(minutes=1)
    return None
