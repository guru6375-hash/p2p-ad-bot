"""A small, dependency-free 5-field cron parser.

Fields are ``minute hour day-of-month month day-of-week``.  Supported syntax: ``*``,
lists (``1,5``), ranges (``6-18``), steps (``*/25``, ``0-30/10``, ``5/10``) and month/day
names (``JAN`` … ``DEC``, ``MON`` … ``SUN``, case-insensitive).  Day-of-week accepts
``0``-``6`` with both ``0`` and ``7`` meaning Sunday (``MON`` is ``1`` … ``SAT`` is ``6``).

Semantics follow Vixie cron: when *both* day-of-month and day-of-week are restricted they
are OR-ed, otherwise they are AND-ed.  :meth:`CronExpression.next_after` is strictly after
the supplied moment and raises :class:`CronError` for expressions that can never fire
(``0 0 30 2 *``); the search is bounded and never loops forever.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .errors import CronError

__all__ = ["CronExpression", "describe", "parse_interval"]

_MONTH_NAMES: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

#: Cron weekdays: 0 = Sunday … 6 = Saturday (7 is accepted as Sunday too).
_WEEKDAY_NAMES: dict[str, int] = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}

_FIELD_LABELS: tuple[str, ...] = (
    "minute",
    "hour",
    "day-of-month",
    "month",
    "day-of-week",
)

#: How far ahead :meth:`CronExpression.next_after` looks before declaring "never fires".
#: Five years comfortably covers leap-day schedules (``0 0 29 2 *``).
_MAX_SEARCH_DAYS = 366 * 5


@dataclass(frozen=True)
class CronExpression:
    """A parsed 5-field cron expression.

    The parsed field values are exposed as frozensets (:attr:`minutes`, :attr:`hours`,
    :attr:`days`, :attr:`months`, :attr:`weekdays`, or as a tuple through :attr:`fields`).
    """

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool = False
    weekday_restricted: bool = False

    # -- construction --------------------------------------------------------------
    @classmethod
    def parse(cls, expr: str) -> CronExpression:
        """Parse ``expr``, raising :class:`CronError` for anything malformed."""
        if not isinstance(expr, str):
            raise CronError(f"cron expression must be a string, got {type(expr).__name__}")
        text = expr.strip()
        if not text:
            raise CronError("cron expression must not be empty")
        if text.startswith("@"):
            raise CronError(
                f"cron macros are not supported: {expr!r}; use 5 fields, e.g. '*/25 * * * *'"
            )
        fields = text.split()
        if len(fields) != 5:
            raise CronError(
                "cron expression must have exactly 5 fields "
                f"(minute hour day-of-month month day-of-week), got {len(fields)}: {expr!r}"
            )
        minute, hour, day, month, weekday = fields
        raw_weekdays = _parse_field(weekday, "day-of-week", 0, 7, _WEEKDAY_NAMES, text)
        return cls(
            expression=text,
            minutes=_parse_field(minute, "minute", 0, 59, {}, text),
            hours=_parse_field(hour, "hour", 0, 23, {}, text),
            days=_parse_field(day, "day-of-month", 1, 31, {}, text),
            months=_parse_field(month, "month", 1, 12, _MONTH_NAMES, text),
            weekdays=frozenset(0 if value == 7 else value for value in raw_weekdays),
            day_restricted=day.strip() != "*",
            weekday_restricted=weekday.strip() != "*",
        )

    # -- accessors -----------------------------------------------------------------
    @property
    def fields(self) -> tuple[frozenset[int], ...]:
        """Parsed field sets in cron order: minute, hour, day, month, weekday."""
        return (self.minutes, self.hours, self.days, self.months, self.weekdays)

    @property
    def interval_minutes(self) -> int | None:
        """``N`` when the expression is ``*/N * * * *``, else ``None``."""
        minute, hour, day, month, weekday = self.expression.split()
        if minute.startswith("*/") and (hour, day, month, weekday) == ("*", "*", "*", "*"):
            remainder = minute[2:]
            if remainder.isdigit():
                return int(remainder)
        return None

    def describe(self) -> str:
        """Human summary of this expression (see :func:`describe`)."""
        return describe(self)

    # -- evaluation ----------------------------------------------------------------
    def matches(self, moment: datetime) -> bool:
        """``True`` when ``moment`` (to the minute) satisfies this expression."""
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        return self._day_matches(moment.date())

    def next_after(self, moment: datetime) -> datetime:
        """The first matching moment strictly after ``moment``.

        Naive input yields naive UTC output; an aware input is interpreted as UTC and the
        result keeps UTC tzinfo, so callers can chain the value straight back in.

        Invariant: the returned moment is strictly after ``moment`` and satisfies
        :meth:`matches`.  Months outside the ``month`` field are skipped wholesale, and an
        expression with no match inside the bounded window raises :class:`CronError`
        (``0 0 30 2 *`` can never fire).
        """
        aware = moment.tzinfo is not None
        reference = moment.astimezone(timezone.utc).replace(tzinfo=None) if aware else moment
        candidate = reference.replace(second=0, microsecond=0) + timedelta(minutes=1)
        last_day = candidate.date() + timedelta(days=_MAX_SEARCH_DAYS)
        day = candidate.date()
        while day <= last_day:
            if day.month not in self.months:
                day = _first_of_next_month(day)
                continue
            if self._day_matches(day):
                clock = self._first_time(candidate.time() if day == candidate.date() else None)
                if clock is not None:
                    found = datetime.combine(day, clock)
                    return found.replace(tzinfo=timezone.utc) if aware else found
            day += timedelta(days=1)
        raise CronError(
            f"cron expression {self.expression!r} never fires "
            f"(no match within {_MAX_SEARCH_DAYS} days after {reference.isoformat()})"
        )

    # -- internals -----------------------------------------------------------------
    def _day_matches(self, day: date) -> bool:
        """Date-level match: the month field is always AND-ed, dom/dow follow cron rules."""
        if day.month not in self.months:
            return False
        day_of_month_ok = day.day in self.days
        day_of_week_ok = _cron_weekday(day.weekday()) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_of_month_ok or day_of_week_ok
        return day_of_month_ok and day_of_week_ok

    def _first_time(self, after: time | None) -> time | None:
        for hour in sorted(self.hours):
            if after is not None and hour < after.hour:
                continue
            for minute in sorted(self.minutes):
                if after is not None and hour == after.hour and minute < after.minute:
                    continue
                return time(hour, minute)
        return None


def _cron_weekday(python_weekday: int) -> int:
    """Convert :meth:`datetime.weekday` (Mon = 0) to the cron numbering (Sun = 0)."""
    return (python_weekday + 1) % 7


def _first_of_next_month(day: date) -> date:
    """First day of the following month (lets ``next_after`` skip constrained-out months)."""
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _parse_field(
    text: str,
    label: str,
    low: int,
    high: int,
    names: Mapping[str, int],
    expression: str,
) -> frozenset[int]:
    stripped = text.strip()
    if not stripped:
        raise CronError(f"empty {label} field in cron expression {expression!r}")
    values: set[int] = set()
    for token in stripped.split(","):
        token = token.strip()
        if not token:
            raise CronError(
                f"empty list entry in {label} field {text!r} of cron expression {expression!r}"
            )
        body = token
        step = 1
        if "/" in token:
            body, _, step_text = token.partition("/")
            body = body.strip()
            step = _parse_step(step_text, label, expression)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            start_text, _, end_text = body.partition("-")
            start = _parse_value(start_text, label, low, high, names, expression)
            end = _parse_value(end_text, label, low, high, names, expression)
            if start > end:
                raise CronError(
                    f"{label} field {text!r} has an inverted range {body!r} "
                    f"in cron expression {expression!r}"
                )
        else:
            start = _parse_value(body, label, low, high, names, expression)
            end = high if "/" in token else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _parse_step(text: str, label: str, expression: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit():
        raise CronError(
            f"{label} field has an invalid step {stripped!r} in cron expression {expression!r}"
        )
    step = int(stripped)
    if step <= 0:
        raise CronError(
            f"{label} field has a non-positive step {stripped!r} in cron expression {expression!r}"
        )
    return step


def _parse_value(
    text: str,
    label: str,
    low: int,
    high: int,
    names: Mapping[str, int],
    expression: str,
) -> int:
    key = text.strip()
    if key.lstrip("+").isdigit():
        value = int(key)
    else:
        upper = key.upper()
        if upper not in names:
            raise CronError(
                f"{label} field has an invalid value {key!r} in cron expression {expression!r}"
            )
        value = names[upper]
    if not low <= value <= high:
        raise CronError(
            f"{label} value {key!r} is out of range {low}-{high} in cron expression {expression!r}"
        )
    return value


def parse_interval(minutes: int) -> CronExpression:
    """The ``*/N * * * *`` expression for a parser cadence in whole minutes."""
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise CronError(f"interval must be a whole number of minutes, got {minutes!r}")
    if minutes <= 0 or minutes > 59:
        raise CronError(f"interval must be between 1 and 59 minutes, got {minutes}")
    return CronExpression.parse(f"*/{minutes} * * * *")


def describe(expr: CronExpression | str) -> str:
    """Human-readable summary of ``expr`` (common cadences get a friendly wording)."""
    parsed = CronExpression.parse(expr) if isinstance(expr, str) else expr
    if not isinstance(parsed, CronExpression):  # pragma: no cover - defensive
        raise CronError(f"describe() needs a cron expression or string, got {type(expr).__name__}")
    minute, hour, day, month, weekday = parsed.expression.split()
    interval = parsed.interval_minutes
    if interval is not None:
        return f"every {interval} minutes"
    if parsed.expression == "* * * * *":
        return "every minute"
    if minute == "0" and hour == "*" and (day, month, weekday) == ("*", "*", "*"):
        return "every hour"
    if minute.isdigit() and hour.isdigit() and (day, month, weekday) == ("*", "*", "*"):
        return f"every day at {int(hour):02d}:{int(minute):02d}"
    return (
        f"minute {minute} hour {hour} day-of-month {day} "
        f"month {month} day-of-week {weekday}"
    )
