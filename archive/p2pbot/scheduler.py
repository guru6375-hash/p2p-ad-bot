"""A cooperative, clock-injectable job scheduler.

The scheduler is deliberately tiny and thread-free: it owns named :class:`Job` objects and
executes the ones that are due whenever :meth:`Scheduler.run_due` is called by the bot poll
loop or the CLI.  No signals, threads or POSIX-only primitives are used, which keeps the
bot Windows-friendly and the scheduling logic trivially testable through an injected clock.

A job reschedules itself after every run: interval jobs advance their ``next_run_at`` in
whole intervals until it is strictly in the future, cron jobs ask
:meth:`CronExpression.next_after`.  A job that raises is recorded (``last_error``), logged
and rescheduled; it never stops the rest of the pass.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .cron import CronExpression, describe
from .errors import CronError

__all__ = ["Job", "Scheduler"]

_log = logging.getLogger(__name__)


@dataclass
class Job:
    """A named unit of work with exactly one schedule (interval *or* cron).

    Mutable by design: the scheduler stamps ``next_run_at``/``last_run_at``/``last_result``/
    ``last_error`` on the very object the caller registered.
    """

    name: str
    func: Callable[[], Any]
    interval_minutes: int | None = None
    cron: CronExpression | None = None
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_result: Any = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise CronError("job name must be a non-empty string")
        if not callable(self.func):
            raise CronError(f"job {self.name!r} needs a callable to run")
        if (self.interval_minutes is None) == (self.cron is None):
            raise CronError(
                f"job {self.name!r} must define exactly one of interval_minutes or cron"
            )
        if self.interval_minutes is not None:
            _check_interval(self.name, self.interval_minutes)
        if self.cron is not None and not isinstance(self.cron, CronExpression):
            raise CronError(f"job {self.name!r} cron must be a CronExpression")

    @classmethod
    def every(cls, name: str, func: Callable[[], Any], minutes: int) -> Job:
        """A job that runs every ``minutes`` minutes."""
        return cls(name=name, func=func, interval_minutes=minutes)

    @classmethod
    def cron_job(cls, name: str, func: Callable[[], Any], expr: CronExpression | str) -> Job:
        """A job that runs on the 5-field cron ``expr``."""
        cron = CronExpression.parse(expr) if isinstance(expr, str) else expr
        return cls(name=name, func=func, cron=cron)

    @property
    def schedule(self) -> str:
        """Human summary of this job's cadence."""
        if self.interval_minutes is not None:
            return f"every {self.interval_minutes} minutes"
        return describe(self.cron)


def _check_interval(name: str, minutes: Any) -> None:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise CronError(
            f"job {name!r} interval_minutes must be a positive whole number, got {minutes!r}"
        )


class Scheduler:
    """Holds named jobs and runs the ones that are due."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._now = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._jobs: dict[str, Job] = {}

    # -- registry ------------------------------------------------------------------
    def add(self, job: Job) -> Job:
        """Register ``job`` (replacing a job of the same name) and schedule its first run."""
        if not isinstance(job, Job):  # pragma: no cover - defensive
            raise CronError(f"scheduler accepts Job instances, got {type(job).__name__}")
        if job.next_run_at is None:
            job.next_run_at = self._first_run(job, self._now())
        previous = self._jobs.get(job.name)
        if previous is not None and previous is not job:
            _log.debug("scheduler replaced job %s", job.name)
        self._jobs[job.name] = job
        return job

    def remove(self, name: str) -> Job | None:
        """Unregister the job called ``name``; ``None`` when it was not registered."""
        return self._jobs.pop(name, None)

    def get(self, name: str) -> Job:
        """The job called ``name``; raises :class:`KeyError` when unknown."""
        return self._jobs[name]

    def jobs(self) -> tuple[Job, ...]:
        """Every registered job, ordered by name."""
        return tuple(self._jobs[name] for name in sorted(self._jobs))

    # -- execution -----------------------------------------------------------------
    def due(self, now: datetime | None = None) -> tuple[Job, ...]:
        """Jobs whose ``next_run_at`` is at or before ``now``, earliest first."""
        moment = self.now() if now is None else now
        pending = [
            job
            for job in self._jobs.values()
            if job.next_run_at is not None and job.next_run_at <= moment
        ]
        pending.sort(key=lambda job: (job.next_run_at, job.name))
        return tuple(pending)

    def run_due(self, now: datetime | None = None) -> tuple[Job, ...]:
        """Run every due job exactly once and reschedule it; returns the jobs that ran."""
        moment = self.now() if now is None else now
        ran: list[Job] = []
        for job in self.due(moment):
            self._run(job, moment)
            ran.append(job)
        return tuple(ran)

    def next_run(self, name: str | None = None) -> datetime | None:
        """Next run time of ``name``, or of the whole scheduler when ``name`` is omitted."""
        if name is None:
            upcoming = [job.next_run_at for job in self._jobs.values() if job.next_run_at]
            return min(upcoming) if upcoming else None
        return self.get(name).next_run_at

    def describe(self) -> str:
        """One line per job: cadence, next run (ISO) and last error, if any."""
        if not self._jobs:
            return "no jobs scheduled"
        lines: list[str] = []
        for job in self.jobs():
            next_run = job.next_run_at.isoformat() if job.next_run_at else "never"
            line = f"{job.name}: {job.schedule} (next run {next_run})"
            if job.last_error:
                line += f" [last error: {job.last_error}]"
            lines.append(line)
        return "\n".join(lines)

    def now(self) -> datetime:
        """Current time according to the injected clock."""
        return self._now()

    # -- internals -----------------------------------------------------------------
    def _first_run(self, job: Job, moment: datetime) -> datetime:
        if job.interval_minutes is not None:
            return moment + timedelta(minutes=job.interval_minutes)
        return job.cron.next_after(moment)

    def _run(self, job: Job, moment: datetime) -> None:
        job.last_run_at = moment
        try:
            job.last_result = job.func()
        except Exception as exc:  # noqa: BLE001 - a failing job must not abort the pass
            job.last_result = None
            job.last_error = f"{type(exc).__name__}: {exc}"
            _log.error("job %s failed: %s", job.name, job.last_error)
        else:
            job.last_error = None
        job.next_run_at = self._reschedule(job, moment)

    def _reschedule(self, job: Job, moment: datetime) -> datetime:
        if job.interval_minutes is not None:
            step = timedelta(minutes=job.interval_minutes)
            upcoming = (job.next_run_at or moment) + step
            while upcoming <= moment:
                upcoming += step
            return upcoming
        return job.cron.next_after(moment)
