"""Cron parsing and the clock-injectable scheduler (SPEC section 8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from p2pbot.cron import CronExpression, describe, parse_interval
from p2pbot.errors import CronError
from p2pbot.scheduler import Job, Scheduler

UTC = timezone.utc


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, tz: timezone | None = None) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)


# -- parsing ---------------------------------------------------------------------------
def test_parse_step_expression() -> None:
    expr = CronExpression.parse("*/25 * * * *")
    assert expr.minutes == frozenset({0, 25, 50})
    assert len(expr.hours) == 24
    assert expr.interval_minutes == 25
    assert expr.day_restricted is False
    assert expr.weekday_restricted is False
    assert expr.fields == (expr.minutes, expr.hours, expr.days, expr.months, expr.weekdays)


def test_parse_lists_ranges_and_steps() -> None:
    assert CronExpression.parse("1,5 * * * *").minutes == frozenset({1, 5})
    assert CronExpression.parse("6-18 * * * *").minutes == frozenset(range(6, 19))
    assert CronExpression.parse("0-30/10 * * * *").minutes == frozenset({0, 10, 20, 30})
    assert CronExpression.parse("5/10 * * * *").minutes == frozenset({5, 15, 25, 35, 45, 55})
    assert CronExpression.parse("* * * * *").interval_minutes is None


def test_month_and_weekday_names() -> None:
    expr = CronExpression.parse("0 9 1 JAN,MAR MON-FRI")
    assert expr.months == frozenset({1, 3})
    assert expr.weekdays == frozenset({1, 2, 3, 4, 5})
    assert expr.day_restricted is True
    assert expr.weekday_restricted is True
    assert CronExpression.parse("0 0 * * 7").weekdays == frozenset({0})


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "@daily",
        "*/25 * * *",
        "*/25 * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * 0 *",
        "* * * * 8",
        "*/0 * * * *",
        "*/x * * * *",
        "5-1 * * * *",
        "1,,2 * * * *",
        "JAN * * * *",
        "-5 * * * *",
    ],
)
def test_invalid_cron_expressions(expression: str) -> None:
    with pytest.raises(CronError):
        CronExpression.parse(expression)


def test_non_string_expression_is_rejected() -> None:
    with pytest.raises(CronError, match="must be a string"):
        CronExpression.parse(5)  # type: ignore[arg-type]


def test_parse_interval_builds_the_equivalent_expression() -> None:
    expr = parse_interval(25)
    assert expr.expression == "*/25 * * * *"
    assert expr.interval_minutes == 25
    assert parse_interval(1).expression == "*/1 * * * *"


@pytest.mark.parametrize("bad", [0, -1, 60, 90, 2.5, True, "25", None])
def test_parse_interval_rejects_invalid_cadences(bad: object) -> None:
    with pytest.raises(CronError):
        parse_interval(bad)  # type: ignore[arg-type]


# -- matching --------------------------------------------------------------------------
def test_matches_to_the_minute() -> None:
    expr = CronExpression.parse("*/25 * * * *")
    assert expr.matches(_dt(2026, 1, 1, 0, 25)) is True
    assert expr.matches(_dt(2026, 1, 1, 0, 26)) is False
    assert expr.matches(_dt(2026, 1, 1, 13, 50)) is True


def test_dom_and_dow_are_ored_when_both_are_restricted() -> None:
    """Vixie semantics: ``0 0 13 * MON`` fires on the 13th *or* on any Monday."""
    expr = CronExpression.parse("0 0 13 * MON")
    assert expr.matches(_dt(2026, 3, 13)) is True  # the 13th (a Friday)
    assert expr.matches(_dt(2026, 3, 16)) is True  # a Monday
    assert expr.matches(_dt(2026, 3, 17)) is False  # Tuesday the 17th


def test_single_restricted_day_field_uses_and_semantics() -> None:
    assert CronExpression.parse("0 0 13 * *").matches(_dt(2026, 3, 13)) is True
    assert CronExpression.parse("0 0 13 * *").matches(_dt(2026, 3, 14)) is False
    assert CronExpression.parse("0 0 * * MON").matches(_dt(2026, 3, 16)) is True
    assert CronExpression.parse("0 0 * * MON").matches(_dt(2026, 3, 17)) is False


def test_month_field_always_ands() -> None:
    expr = CronExpression.parse("0 0 * JAN *")
    assert expr.matches(_dt(2026, 1, 15)) is True
    assert expr.matches(_dt(2026, 2, 15)) is False


# -- next_after ------------------------------------------------------------------------
def test_next_after_is_strictly_after_and_naive_by_default() -> None:
    expr = CronExpression.parse("*/25 * * * *")
    assert expr.next_after(_dt(2026, 1, 1, 0, 0)) == _dt(2026, 1, 1, 0, 25)
    assert expr.next_after(_dt(2026, 1, 1, 0, 25)) == _dt(2026, 1, 1, 0, 50)
    assert expr.next_after(_dt(2026, 1, 1, 0, 50)) == _dt(2026, 1, 1, 1, 0)


def test_next_after_rolls_over_days_and_months() -> None:
    expr = CronExpression.parse("0 9 * * *")
    assert expr.next_after(_dt(2026, 1, 31, 10, 0)) == _dt(2026, 2, 1, 9, 0)
    monthly = CronExpression.parse("0 0 1 * *")
    assert monthly.next_after(_dt(2026, 1, 15)) == _dt(2026, 2, 1)


def test_next_after_preserves_utc_awareness() -> None:
    expr = CronExpression.parse("*/25 * * * *")
    result = expr.next_after(_dt(2026, 1, 1, 0, 0, tz=UTC))
    assert result == _dt(2026, 1, 1, 0, 25, tz=UTC)
    assert result.tzinfo is UTC


def test_next_after_handles_non_utc_aware_input() -> None:
    expr = CronExpression.parse("*/25 * * * *")
    tz = timezone(timedelta(hours=2))
    result = expr.next_after(_dt(2026, 1, 1, 2, 0, tz=tz))  # 00:00 UTC
    assert result == _dt(2026, 1, 1, 0, 25, tz=UTC)


def test_next_after_skips_unmatched_months_and_weekdays() -> None:
    expr = CronExpression.parse("0 12 1 12 *")
    assert expr.next_after(_dt(2026, 1, 1)) == _dt(2026, 12, 1, 12, 0)
    weekly = CronExpression.parse("0 9 * * MON")
    assert weekly.next_after(_dt(2026, 3, 13, 12, 0)) == _dt(2026, 3, 16, 9, 0)


def test_next_after_raises_for_an_expression_that_never_fires() -> None:
    expr = CronExpression.parse("0 0 30 2 *")
    with pytest.raises(CronError, match="never fires"):
        expr.next_after(_dt(2026, 1, 1))


def test_leap_day_expression_fires() -> None:
    expr = CronExpression.parse("0 0 29 2 *")
    assert expr.next_after(_dt(2026, 1, 1)) == _dt(2028, 2, 29)


# -- describe --------------------------------------------------------------------------
def test_describe_friendly_wording() -> None:
    assert describe("*/25 * * * *") == "every 25 minutes"
    assert describe("* * * * *") == "every minute"
    assert describe("0 * * * *") == "every hour"
    assert describe("30 7 * * *") == "every day at 07:30"
    assert describe("0 9 1 * *") == "minute 0 hour 9 day-of-month 1 month * day-of-week *"
    expr = CronExpression.parse("*/10 * * * *")
    assert expr.describe() == "every 10 minutes"


# -- Job -------------------------------------------------------------------------------
def test_job_requires_exactly_one_schedule() -> None:
    with pytest.raises(CronError, match="exactly one of interval_minutes or cron"):
        Job(name="none", func=lambda: None)
    with pytest.raises(CronError, match="exactly one of interval_minutes or cron"):
        Job(name="both", func=lambda: None, interval_minutes=5, cron=CronExpression.parse("* * * * *"))


def test_job_validation_of_name_callable_and_cadence() -> None:
    with pytest.raises(CronError, match="job name must be a non-empty string"):
        Job(name="   ", func=lambda: None, interval_minutes=5)
    with pytest.raises(CronError, match="needs a callable"):
        Job(name="x", func="not-callable", interval_minutes=5)  # type: ignore[arg-type]
    with pytest.raises(CronError, match="interval_minutes must be a positive whole number"):
        Job.every("x", lambda: None, 0)
    with pytest.raises(CronError, match="interval_minutes must be a positive whole number"):
        Job.every("x", lambda: None, True)
    with pytest.raises(CronError, match="cron must be a CronExpression"):
        Job(name="x", func=lambda: None, cron="* * * * *")  # type: ignore[arg-type]


def test_job_constructors_and_schedule_summary() -> None:
    every = Job.every("parser", lambda: None, 25)
    assert every.interval_minutes == 25
    assert every.schedule == "every 25 minutes"
    parsed = Job.cron_job("parser", lambda: None, "*/25 * * * *")
    assert parsed.cron is not None
    assert parsed.schedule == "every 25 minutes"
    from_string = CronExpression.parse("0 9 * * *")
    assert Job.cron_job("daily", lambda: None, from_string).cron is from_string


# -- Scheduler -------------------------------------------------------------------------
def test_scheduler_first_run_for_interval_and_cron_jobs(clock) -> None:
    calls: list[str] = []
    scheduler = Scheduler(clock)
    interval = scheduler.add(Job.every("parser", lambda: calls.append("parser"), 25))
    assert interval.next_run_at == clock() + timedelta(minutes=25)
    cron_job = scheduler.add(Job.cron_job("daily", lambda: calls.append("daily"), "0 9 * * *"))
    assert cron_job.next_run_at == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    assert calls == []


def test_scheduler_registry_operations(clock) -> None:
    scheduler = Scheduler(clock)
    first = scheduler.add(Job.every("b", lambda: None, 5))
    second = scheduler.add(Job.every("a", lambda: None, 5))
    assert [job.name for job in scheduler.jobs()] == ["a", "b"]
    assert scheduler.get("b") is first
    assert scheduler.next_run("a") == second.next_run_at
    assert scheduler.next_run() == min(first.next_run_at, second.next_run_at)
    with pytest.raises(KeyError):
        scheduler.get("missing")
    replacement = Job.every("b", lambda: None, 10)
    assert scheduler.add(replacement) is replacement
    assert scheduler.get("b") is replacement
    assert scheduler.remove("b") is replacement
    assert scheduler.remove("b") is None
    assert [job.name for job in scheduler.jobs()] == ["a"]


def test_due_and_run_due_use_the_injected_clock(clock) -> None:
    calls: list[str] = []
    scheduler = Scheduler(clock)
    scheduler.add(Job.every("parser", lambda: calls.append("parser"), 25))
    assert scheduler.due() == ()
    assert scheduler.run_due() == ()

    clock.advance(minutes=25)
    ran = scheduler.run_due()
    assert [job.name for job in ran] == ["parser"]
    assert calls == ["parser"]


def test_run_due_executes_each_due_job_exactly_once_and_reschedules(clock) -> None:
    calls: list[str] = []
    scheduler = Scheduler(clock)
    scheduler.add(Job.every("parser", lambda: calls.append("parser"), 25))
    now = clock() + timedelta(minutes=25)
    scheduler.run_due(now)
    scheduler.run_due(now)
    assert calls == ["parser"]
    assert scheduler.next_run("parser") == clock() + timedelta(minutes=50)
    ran = scheduler.run_due(clock() + timedelta(minutes=50))
    assert len(ran) == 1
    assert calls == ["parser", "parser"]


def test_interval_reschedule_catches_up_without_running_late_more_than_once(clock) -> None:
    """A sleeping process must not replay every missed interval."""
    calls: list[str] = []
    scheduler = Scheduler(clock)
    job = scheduler.add(Job.every("parser", lambda: calls.append("parser"), 25))
    late = clock() + timedelta(minutes=75)
    scheduler.run_due(late)
    assert calls == ["parser"]
    assert job.next_run_at == clock() + timedelta(minutes=100)
    assert job.next_run_at > late


def test_cron_job_reschedules_from_cron(clock) -> None:
    calls: list[str] = []
    scheduler = Scheduler(clock)
    job = scheduler.add(Job.cron_job("daily", lambda: calls.append("daily"), "0 9 * * *"))
    scheduler.run_due(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    assert calls == ["daily"]
    assert job.next_run_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)


def test_due_jobs_are_ordered_oldest_first(clock) -> None:
    scheduler = Scheduler(clock)
    later = scheduler.add(Job.every("later", lambda: None, 30))
    sooner = scheduler.add(Job.every("sooner", lambda: None, 5))
    ran = scheduler.run_due(clock() + timedelta(minutes=30))
    assert [job.name for job in ran] == ["sooner", "later"]
    assert later.last_run_at == sooner.last_run_at == clock() + timedelta(minutes=30)


def test_a_failing_job_is_recorded_and_does_not_stop_the_pass(clock) -> None:
    calls: list[str] = []

    def boom() -> None:
        raise ValueError("venue down")

    scheduler = Scheduler(clock)
    failing = scheduler.add(Job.every("parser", boom, 25))
    healthy = scheduler.add(Job.every("prices", lambda: calls.append("prices"), 25))
    ran = scheduler.run_due(clock() + timedelta(minutes=25))
    assert [job.name for job in ran] == ["parser", "prices"]
    assert failing.last_error == "ValueError: venue down"
    assert failing.last_result is None
    assert failing.next_run_at == clock() + timedelta(minutes=50)
    assert healthy.last_error is None
    assert calls == ["prices"]


def test_last_error_is_cleared_after_a_successful_run(clock) -> None:
    state = {"fail": True}

    def flaky() -> str:
        if state["fail"]:
            raise RuntimeError("transient")
        return "ok"

    scheduler = Scheduler(clock)
    job = scheduler.add(Job.every("job", flaky, 5))
    scheduler.run_due(clock() + timedelta(minutes=5))
    assert job.last_error == "RuntimeError: transient"
    state["fail"] = False
    scheduler.run_due(clock() + timedelta(minutes=10))
    assert job.last_error is None
    assert job.last_result == "ok"


def test_describe_lists_jobs_and_errors(clock) -> None:
    scheduler = Scheduler(clock)
    assert scheduler.describe() == "no jobs scheduled"
    scheduler.add(Job.every("parser", lambda: None, 25))
    line = scheduler.describe()
    assert "parser: every 25 minutes" in line
    assert "next run" in line
    scheduler.add(Job.cron_job("broken", lambda: 1 / 0, "0 9 * * *"))
    scheduler.run_due(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
    assert "broken: " in scheduler.describe()
    assert "last error: ZeroDivisionError" in scheduler.describe()


def test_scheduler_defaults_to_the_wall_clock() -> None:
    scheduler = Scheduler()
    assert scheduler.now().tzinfo is not None
    scheduler.add(Job.every("x", lambda: None, 1))
    assert scheduler.next_run("x") is not None


def test_job_with_preset_next_run_is_respected(clock) -> None:
    moment = clock() + timedelta(minutes=3)
    job = Job.every("x", lambda: None, 10)
    job.next_run_at = moment
    scheduler = Scheduler(clock)
    scheduler.add(job)
    assert scheduler.next_run("x") == moment
