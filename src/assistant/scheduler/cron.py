"""Stdlib 5-field POSIX cron parser + DST-aware ``is_due`` / ``next_fire``.

Scope (phase 5 minimum viable):
  - 5 fields only: minute, hour, day-of-month, month, day-of-week.
  - Syntax: ``*``, lists ``1,2,3``, ranges ``1-5``, steps ``*/5``, ``2-10/2``.
  - Day-of-week: ``0`` or ``7`` == Sunday.
  - Rejects: aliases (``MON``, ``JAN``), @-shortcuts (``@daily``, ``@reboot``),
    Quartz extensions (``L``, ``W``, ``?``, ``#``), out-of-range values, wrong
    field count, non-string input.

DST policy (RQ3 verified):
  - ``is_existing_local_minute`` FIRST — a spring-skipped wall clock would
    otherwise be misclassified as ambiguous by Python's fold=0/fold=1
    round-trip. Existence check silently drops the minute (NO retro-fire).
  - ``is_ambiguous_local_minute`` SECOND — on fall-back duplication we fire
    exactly once (fold=0) and drop the fold=1 rerun.

Lookahead policy (RQ2+RQ6): ``next_fire`` default ``max_lookahead_days=1500``
covers leap-day-only schedules (``0 0 29 2 *``) from an arbitrary start
date — 1500 days ≈ 4y+1d, which spans any quadrennial leap window.

Vixie day semantics: when BOTH ``dom`` and ``dow`` are restricted (neither
is a raw ``*``), the day predicate is ``dom OR dow``. If exactly one is a
raw ``*``, only the OTHER restriction applies. We preserve raw-star flags
on :class:`CronExpr` because the expanded set does not distinguish
``*`` from a user-specified full range.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# ``_CRON_FIELD_RE`` blocks Quartz extensions (L/W/?/#), alphabetic aliases
# (MON/JAN/...), and any other non-numeric syntax BEFORE we split on ``,``.
# Whitespace inside a field (``"1, 2"``) is rejected here too — cron fields
# are whitespace-separated at the expression level only.
_CRON_FIELD_RE = re.compile(r"^[\d,\-/\*]+$")


class CronParseError(ValueError):
    """Raised by :func:`parse_cron` on any malformed input.

    Subclasses ``ValueError`` per plan §E so callers can pattern-match on
    the base type when bubbling up to tool responses.
    """


@dataclass(frozen=True)
class CronExpr:
    """Normalised cron expression.

    Fields are ``frozenset[int]`` so equality + hashing are cheap and the
    struct can be cached by schedule id. ``raw_dom_star`` / ``raw_dow_star``
    preserve the original literal-``*`` status of the day fields — required
    for vixie OR semantics (§E).
    """

    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    raw_dom_star: bool
    raw_dow_star: bool


# (inclusive_lo, inclusive_hi) bounds per field. DoW accepts 7 (Sunday
# alias) which is normalised to 0 post-expansion.
_RANGES: dict[str, tuple[int, int]] = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),
}


def _expand_field(field: str, kind: str) -> frozenset[int]:
    """Expand a single cron field into its integer set.

    ``kind`` selects the range table and the post-expansion DoW-7
    normalisation. Raises :class:`CronParseError` on any out-of-range or
    malformed input.
    """
    lo, hi = _RANGES[kind]
    if not _CRON_FIELD_RE.match(field):
        raise CronParseError(f"invalid chars in {kind}: {field!r}")
    out: set[int] = set()
    for part in field.split(","):
        if not part:
            raise CronParseError(f"empty {kind} term: {field!r}")
        step = 1
        if "/" in part:
            body, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as exc:
                raise CronParseError(f"invalid step in {kind}: {part!r}") from exc
            if step <= 0:
                raise CronParseError(f"step must be > 0 in {kind}: {field!r}")
        else:
            body = part
        if body == "*":
            start, end = lo, hi
        elif "-" in body:
            start_s, end_s = body.split("-", 1)
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise CronParseError(f"invalid range in {kind}: {part!r}") from exc
        else:
            try:
                start = end = int(body)
            except ValueError as exc:
                raise CronParseError(f"invalid value in {kind}: {part!r}") from exc
        if start < lo or end > hi or start > end:
            raise CronParseError(f"{kind} out of range {lo}-{hi}: {field!r}")
        out.update(range(start, end + 1, step))
    if kind == "dow":
        # RFC-compat: 7 is a Sunday alias; collapse into 0.
        out = {0 if x == 7 else x for x in out}
    return frozenset(out)


def parse_cron(expr: str) -> CronExpr:
    """Parse a 5-field cron expression.

    Raises :class:`CronParseError` on any of: non-string input, wrong field
    count, ``@``-shortcuts, alphabetic aliases, Quartz extensions,
    out-of-range values, or bad step/range syntax.
    """
    if not isinstance(expr, str):
        raise CronParseError("cron must be a string")
    stripped = expr.strip()
    if not stripped:
        raise CronParseError("cron is empty")
    if stripped.startswith("@"):
        # Plan §E: reject @daily / @weekly / @yearly / @reboot. Owner's
        # scheduler expects explicit 5-field expressions so the model's
        # output is auditable at a glance.
        raise CronParseError("@-aliases (@daily/@weekly/...) not supported")
    fields = stripped.split()
    if len(fields) != 5:
        raise CronParseError(f"expected 5 fields, got {len(fields)}")
    m, h, dom, mo, dow = fields
    return CronExpr(
        minute=_expand_field(m, "minute"),
        hour=_expand_field(h, "hour"),
        dom=_expand_field(dom, "dom"),
        month=_expand_field(mo, "month"),
        dow=_expand_field(dow, "dow"),
        raw_dom_star=(dom == "*"),
        raw_dow_star=(dow == "*"),
    )


def is_existing_local_minute(naked: dt.datetime, tz: ZoneInfo) -> bool:
    """Return True iff ``naked`` (tz-naive wall clock) exists in ``tz``.

    DST spring-forward silently drops a wall-clock minute (e.g.
    ``2026-03-29 02:30`` in ``Europe/Berlin`` — clocks leap from
    ``02:00 CET`` straight to ``03:00 CEST``). Python's
    :meth:`datetime.astimezone` round-trip lands ``fold=0`` on the
    nearest valid instant, shifting the wall clock by one hour. We
    detect that shift and treat it as non-existence.
    """
    aware = naked.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(dt.UTC)
    back = utc.astimezone(tz)
    return (back.year, back.month, back.day, back.hour, back.minute) == (
        naked.year,
        naked.month,
        naked.day,
        naked.hour,
        naked.minute,
    )


def is_ambiguous_local_minute(naked: dt.datetime, tz: ZoneInfo) -> bool:
    """Return True iff ``naked`` matches two distinct UTC instants in ``tz``.

    Fall-back duplicates one wall-clock hour. Firing on both ``fold=0``
    and ``fold=1`` would double-deliver; the loop drops ``fold=1``.
    """
    a = naked.replace(tzinfo=tz, fold=0).astimezone(dt.UTC)
    b = naked.replace(tzinfo=tz, fold=1).astimezone(dt.UTC)
    return a != b


def _cron_weekday(local: dt.datetime) -> int:
    """Convert Python ``weekday()`` (Mon=0..Sun=6) to cron (Sun=0..Sat=6)."""
    return (local.weekday() + 1) % 7


def _matches(expr: CronExpr, local: dt.datetime) -> bool:
    """Return True iff ``local`` (naive wall clock in the schedule's tz)
    matches ``expr``. Applies vixie OR/AND day semantics.
    """
    dom_ok = local.day in expr.dom
    dow_ok = _cron_weekday(local) in expr.dow
    if not expr.raw_dom_star and not expr.raw_dow_star:
        day_ok = dom_ok or dow_ok
    elif expr.raw_dom_star:
        day_ok = dow_ok
    else:
        day_ok = dom_ok
    return (
        local.minute in expr.minute
        and local.hour in expr.hour
        and local.month in expr.month
        and day_ok
    )


def next_fire(
    expr: CronExpr,
    *,
    from_utc: dt.datetime,
    tz: ZoneInfo,
    max_lookahead_days: int = 1500,
) -> dt.datetime | None:
    """Return the first UTC minute strictly after ``from_utc`` where
    ``expr`` matches the wall clock in ``tz``.

    ``max_lookahead_days`` defaults to 1500 (~4y+1d) per RQ2+RQ6 so leap-day
    schedules (``0 0 29 2 *``) resolve at any start date. Returns ``None``
    if no match occurs within the window.

    DST policy: spring-skipped minutes are invisible (existence check first);
    ambiguous minutes collapse to ``fold=0`` (pre-transition UTC instant).
    """
    if from_utc.tzinfo is None:
        raise ValueError("from_utc must be timezone-aware (UTC)")
    start_utc = from_utc.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    end_utc = start_utc + dt.timedelta(days=max_lookahead_days)
    cursor = start_utc
    while cursor < end_utc:
        local = cursor.astimezone(tz).replace(tzinfo=None)
        if is_existing_local_minute(local, tz) and _matches(expr, local):
            if is_ambiguous_local_minute(local, tz):
                aware = local.replace(tzinfo=tz, fold=0)
                return aware.astimezone(dt.UTC)
            return cursor
        cursor += dt.timedelta(minutes=1)
    return None


def is_due(
    expr: CronExpr,
    *,
    last_fire_at: dt.datetime | None,
    now_utc: dt.datetime,
    tz: ZoneInfo,
    catchup_window_s: int,
) -> dt.datetime | None:
    """Return the due UTC minute if ``expr`` should fire at-or-before
    ``now_utc``, else ``None``.

    Semantics:
      - ``last_fire_at is None``: inspect the current floor-minute only.
      - ``last_fire_at`` set: walk forward from ``last_fire_at + 1 min``
        through ``now_utc`` looking for the first matching minute that is
        still within ``catchup_window_s`` of now. Minutes older than the
        window are skipped (one recap message on boot handles the gap).
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware (UTC)")
    floor_now = now_utc.replace(second=0, microsecond=0)
    if last_fire_at is None:
        cursor = floor_now
    else:
        if last_fire_at.tzinfo is None:
            raise ValueError("last_fire_at must be timezone-aware")
        cursor = (
            last_fire_at.replace(second=0, microsecond=0)
            + dt.timedelta(minutes=1)
        )
    while cursor <= floor_now:
        local = cursor.astimezone(tz).replace(tzinfo=None)
        if is_existing_local_minute(local, tz) and _matches(expr, local):
            if (floor_now - cursor).total_seconds() > catchup_window_s:
                cursor += dt.timedelta(minutes=1)
                continue
            if is_ambiguous_local_minute(local, tz):
                aware = local.replace(tzinfo=tz, fold=0)
                return aware.astimezone(dt.UTC)
            return cursor
        cursor += dt.timedelta(minutes=1)
    return None
