"""Composition root and the façade the Telegram layer talks to.

This is the only module that instantiates concrete components; everything else receives
its collaborators. ``BotServices`` is intentionally duck-typed (see ``docs/SPEC.md``
§11.5) so the Telegram layer can be developed and tested against a stub.

Responsibilities
----------------
* ``ScenarioManager`` — knows which blueprint is active and persists that choice.
* ``BotServices`` — rates, computed prices, parser runs and status.
* ``build_services`` — wires adapters, stores, engine, publisher and scheduler, and
  registers the ``parser`` job and the ``pln-edits`` cron job (``cron_config.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .blueprint import Blueprint, load_blueprint_by_name
from .config import Settings
from .constants import DEFAULT_PARSER_INTERVAL_MINUTES, VERSION
from .cron import CronExpression, parse_interval
from .cron_config import CronConfig, load_cron_config
from .edit_queue import UAH_USDC, UAH_USDT, EditQueueReport, run_pln_edit_queue, run_uah_rate_queue
from .engine import RateEngine
from .errors import ConfigError, EngineError
from .exchanges import build_adapters
from .exchanges.base import Transport
from .logging_setup import get_logger
from .market import MARKET_MIDDLE_SOURCE, MarketFetchResult, MarketParser, MarketStore
from .models import Account, ComputedAd, OwnAdsResult, Pair, PublishResult, utcnow
from .publisher import AdPublisher, AdStore
from .rates import RateStore
from .scheduler import Job, Scheduler
from .uah_config import load_uah_steps

__all__ = [
    "PublishResult",
    "RateRow",
    "MarketRow",
    "JobRow",
    "StatusSnapshot",
    "ScenarioManager",
    "BotServices",
    "build_services",
    "validate_scenario_accounts",
]

_log = get_logger(__name__)


@dataclass(frozen=True)
class RateRow:
    """One row of the rates table (base and cap for a pair)."""

    pair: str
    base: Decimal | None
    cap: Decimal | None


@dataclass(frozen=True)
class MarketRow:
    """One row of the competitor-market table."""

    platform: str
    pair: str
    middle: Decimal | None
    filtered: int
    fetched_at: datetime | None


@dataclass(frozen=True)
class JobRow:
    """One row of the scheduler table."""

    name: str
    next_run_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything ``/status`` renders, pre-collected so handlers stay trivial."""

    version: str
    scenario: str | None
    fiat: str | None
    strategy: str | None
    rates: tuple[RateRow, ...] = ()
    prices: tuple[ComputedAd, ...] = ()
    market: tuple[MarketRow, ...] = ()
    jobs: tuple[JobRow, ...] = ()
    engine_error: str | None = None
    engine_problems: tuple[str, ...] = ()


def validate_scenario_accounts(blueprint: Blueprint, accounts: Mapping[str, Account]) -> None:
    """Raise :class:`ConfigError` when a blueprint references an unknown account."""
    missing: list[str] = []
    for plan in blueprint.pairs:
        for account_id in plan.accounts:
            if account_id not in accounts:
                missing.append(f"{plan.pair.symbol}:{account_id}")
    if missing:
        raise ConfigError(
            "scenario references accounts missing from .env: " + ", ".join(sorted(missing))
        )


class ScenarioManager:
    """Loads blueprint files and remembers which one is active."""

    def __init__(
        self,
        scenarios_dir: str | Path,
        state_path: str | Path | None = None,
        active: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.scenarios_dir = Path(scenarios_dir)
        self.state_path = Path(state_path) if state_path is not None else None
        self.settings = settings
        self._active = active or self._load_active()
        self._cache: Blueprint | None = None

    # -- discovery -----------------------------------------------------------------
    def available(self) -> tuple[str, ...]:
        if not self.scenarios_dir.is_dir():
            return ()
        return tuple(sorted(path.stem for path in self.scenarios_dir.glob("*.json")))

    def active_name(self) -> str | None:
        return self._active

    def activate(self, name: str) -> Blueprint:
        """Load and validate ``name``, then persist it as the active scenario."""
        blueprint = load_blueprint_by_name(name, self.scenarios_dir)
        if self.settings is not None:
            validate_scenario_accounts(blueprint, self.settings.accounts)
        self._active = blueprint.name
        self._cache = blueprint
        self._persist()
        _log.info("scenario activated: %s (%s)", blueprint.name, blueprint.strategy)
        return blueprint

    def blueprint(self) -> Blueprint:
        """The active blueprint; raises :class:`ConfigError` when none is active."""
        if self._cache is not None:
            return self._cache
        if not self._active:
            available = ", ".join(self.available()) or "none"
            raise ConfigError(f"no active scenario; available: {available}")
        self._cache = load_blueprint_by_name(self._active, self.scenarios_dir)
        return self._cache

    def reload(self) -> Blueprint:
        self._cache = None
        return self.blueprint()

    # -- persistence ---------------------------------------------------------------
    def _load_active(self) -> str | None:
        if self.state_path is None or not self.state_path.exists():
            return None
        try:
            import json

            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # corrupt state must not brick the bot
            _log.warning("could not read %s: %s", self.state_path, exc)
            return None
        name = payload.get("active_scenario") if isinstance(payload, dict) else None
        return str(name) if name else None

    def _persist(self) -> None:
        if self.state_path is None:
            return
        import json
        import os

        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps({"active_scenario": self._active}, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


class BotServices:
    """The single façade handed to the Telegram layer."""

    def __init__(
        self,
        settings: Settings,
        scenarios: ScenarioManager,
        rates: RateStore,
        market: MarketStore,
        scheduler: Scheduler,
        adapters: Mapping[str, Any],
        parser: MarketParser,
        publisher: AdPublisher,
        engine_factory: Callable[[Blueprint], RateEngine],
        *,
        clock: Callable[[], datetime] = utcnow,
        version: str = VERSION,
        cron: CronConfig | None = None,
        uah_steps: Mapping[str, Decimal] | None = None,
    ) -> None:
        self.settings = settings
        self.scenarios = scenarios
        self.rates = rates
        self.market = market
        self.scheduler = scheduler
        self.adapters = adapters
        self.parser = parser
        self.publisher = publisher
        self.engine_factory = engine_factory
        self.clock = clock
        self.version = version
        #: validated ``cron_config.py`` settings (the ``pln-edits`` job interval and pairs)
        self.cron = cron if cron is not None else load_cron_config()
        #: validated ``uah_config.STEP``: ``/setrate`` prices UAH/USDC at rate - step
        self.uah_steps = dict(uah_steps) if uah_steps is not None else load_uah_steps()

    # -- engine --------------------------------------------------------------------
    def engine(self) -> RateEngine:
        return self.engine_factory(self.scenarios.blueprint())

    def compute_with_problems(self) -> tuple[tuple[ComputedAd, ...], tuple[str, ...]]:
        """Every price the engine could compute, plus one line per skipped entry.

        Isolation is per (pair, platform): a venue with no competitor data for one pair
        must not stop the other advertisements from being corrected (SPEC §7.1).
        """
        return self.engine().compute_with_problems()

    def compute(self) -> tuple[ComputedAd, ...]:
        return self.engine().compute()

    # -- façade operations ---------------------------------------------------------
    def get_own_ads(self) -> tuple[OwnAdsResult, ...]:
        """``/getads``: every configured account's live ads (online and offline, not closed)."""
        return self.publisher.fetch_own_ads()

    def set_uah_rate(self, rate: Decimal | str, *, dry_run: bool = False) -> EditQueueReport:
        """``/setrate``: per account, the online buy UAH/USDT ads become ``rate``,
        ``rate - STEP``, ``rate - 2*STEP``, ... and the UAH/USDC ads the same ladder one STEP
        lower. A cap is optional for UAH; one stored with ``/setcap`` still limits (read at
        call time)."""
        caps = {pair.symbol: self.rates.cap(pair) for pair in (UAH_USDT, UAH_USDC)}
        return run_uah_rate_queue(
            rate, self.publisher, steps=self.uah_steps, caps=caps, dry_run=dry_run
        )

    def run_parser(self, pairs: Iterable[str] | None = None) -> tuple[MarketFetchResult, ...]:
        """Run one competitor-parser pass and persist the fresh snapshots.

        The write matters *across processes*: the CLI ``parser`` command and a later
        ``rates`` run are separate processes, so without it the second process would price
        ``market_middle`` scenarios from an empty store. ``MarketStore.save()``
        is a no-op when no path is configured, and the CLI ``--dry-run`` path deliberately
        parses into a scratch store so it never reaches this method.
        """
        results = self.parser.run_once(self.scenarios.blueprint(), pairs=pairs)
        self.market.save()
        return results

    # -- status --------------------------------------------------------------------
    def snapshot(self) -> StatusSnapshot:
        blueprint: Blueprint | None
        engine_error: str | None = None
        try:
            blueprint = self.scenarios.blueprint()
        except ConfigError as exc:
            blueprint = None
            engine_error = str(exc)

        prices: tuple[ComputedAd, ...] = ()
        problems: tuple[str, ...] = ()
        if blueprint is not None:
            try:
                prices, problems = self.compute_with_problems()
            except EngineError as exc:
                engine_error = str(exc)
        if problems and engine_error is None:
            engine_error = problems[0]

        rate_rows: list[RateRow] = []
        pairs: list[Pair] = []
        if blueprint is not None:
            pairs = [plan.pair for plan in blueprint.pairs if plan.enabled]
        for pair in pairs:
            rate_rows.append(
                RateRow(pair=pair.symbol, base=self.rates.base(pair), cap=self.rates.cap(pair))
            )

        market_rows: list[MarketRow] = []
        for snapshot in self.market.items():
            market_rows.append(
                MarketRow(
                    platform=snapshot.platform,
                    pair=snapshot.pair.symbol,
                    middle=snapshot.middle,
                    filtered=len(snapshot.filtered),
                    fetched_at=snapshot.fetched_at,
                )
            )

        job_rows = tuple(
            JobRow(name=job.name, next_run_at=job.next_run_at, last_error=job.last_error)
            for job in self.scheduler.jobs()
        )

        return StatusSnapshot(
            version=self.version,
            scenario=self.scenarios.active_name() if blueprint is None else blueprint.name,
            fiat=None if blueprint is None else blueprint.fiat,
            strategy=None if blueprint is None else blueprint.strategy,
            rates=tuple(rate_rows),
            prices=tuple(prices),
            market=tuple(market_rows),
            jobs=job_rows,
            engine_error=engine_error,
            engine_problems=problems,
        )


def build_services(
    settings: Settings,
    transport: Transport | None = None,
    *,
    clock: Callable[[], datetime] = utcnow,
    adapters: Mapping[str, Any] | None = None,
    cron: CronConfig | None = None,
    uah_steps: Mapping[str, Decimal] | None = None,
) -> BotServices:
    """Wire every component from ``settings``.

    ``cron`` and ``uah_steps`` default to the validated ``cron_config.py`` /
    ``uah_config.py`` settings; a bad setting raises :class:`ConfigError` here, before
    anything runs.
    """
    resolved_adapters = dict(adapters) if adapters is not None else build_adapters(transport)

    rates = RateStore.load(settings.state_path)
    market = MarketStore.load(settings.market_path)
    ad_store = AdStore.load(settings.ads_path)

    scenarios = ScenarioManager(
        settings.scenarios_dir,
        state_path=settings.state_path.with_name("scenario.json"),
        active=settings.raw.get("SCENARIO"),
        settings=settings,
    )
    parser = MarketParser(resolved_adapters, market, clock)
    publisher = AdPublisher(resolved_adapters, settings, ad_store, rates)

    def engine_factory(blueprint: Blueprint) -> RateEngine:
        return RateEngine(blueprint, rates, market)

    scheduler = Scheduler(clock)
    services = BotServices(
        settings,
        scenarios,
        rates,
        market,
        scheduler,
        resolved_adapters,
        parser,
        publisher,
        engine_factory,
        clock=clock,
        cron=cron if cron is not None else load_cron_config(),
        uah_steps=uah_steps if uah_steps is not None else load_uah_steps(),
    )
    _register_jobs(services)
    return services


def _register_jobs(services: BotServices) -> None:
    """Register the competitor-parser pass and the ``pln-edits`` cron job."""
    services.scheduler.add(
        Job.every("parser", lambda: _parse_market(services), DEFAULT_PARSER_INTERVAL_MINUTES)
    )
    services.scheduler.add(
        Job.every("pln-edits", lambda: run_cron_edits(services), services.cron.interval_minutes)
    )


def run_cron_edits(services: BotServices, *, dry_run: bool = False) -> str:
    """One edit-queue pass over ``cron_config.PAIRS``; returns a one-line summary."""
    blueprint = services.scenarios.blueprint()
    report = run_pln_edit_queue(
        blueprint,
        services.engine_factory(blueprint),
        services.publisher,
        parser=services.parser,
        dry_run=dry_run,
        pairs=services.cron.pairs,
    )
    edited = sum(1 for result in report.results if result.status in ("updated", "dry_run"))
    failed = sum(1 for result in report.results if result.status == "error")
    note = (
        f"pln-edits: {len(report.queued)} queued, {edited} edited, {failed} failed, "
        f"{len(report.unchanged)} unchanged"
    )
    if report.problems:
        note += f" | {len(report.problems)} problem(s): " + "; ".join(report.problems)
    return note


def _parse_market(services: BotServices) -> str:
    """One parser pass; returns a one-line summary."""
    results = services.run_parser()
    failures = [f"{row.platform} {row.pair.symbol}: {row.error}" for row in results if row.error]
    note = f"parser: {len(results)} fetches, {len(failures)} failed"
    if failures:
        note += " | " + "; ".join(failures)
    return note


def _parser_job(services: BotServices) -> Job | None:
    """The registered ``parser`` job, or ``None`` when it is not scheduled.

    ``Scheduler.get``/``remove`` raise for an unknown name, and the job legitimately
    disappears when the active scenario has the parser disabled, so both helpers are
    used only after this lookup confirms presence.
    """
    for job in services.scheduler.jobs():
        if job.name == "parser":
            return job
    return None


def configure_parser_job(services: BotServices, blueprint: Blueprint) -> Job | None:
    """Align the ``parser`` job with the active scenario (interval or cron, SPEC §8)."""
    scheduler = services.scheduler
    job = _parser_job(services)
    if not blueprint.parser.enabled:
        if job is not None:
            scheduler.remove("parser")
        return None

    cron = CronExpression.parse(blueprint.parser.cron) if blueprint.parser.cron else None
    interval = None if cron is not None else blueprint.parser.interval_minutes
    if job is None:
        # A Job validates its own schedule, so build it with the scenario's cadence.
        job = Job(
            "parser", lambda: _parse_market(services), interval_minutes=interval, cron=cron
        )
        scheduler.add(job)
    elif cron is not None:
        job.interval_minutes = None
        job.cron = cron
    else:
        job.cron = None
        job.interval_minutes = interval
    _schedule_parser_job(job, services, blueprint)
    return job


def _schedule_parser_job(job: Job, services: BotServices, blueprint: Blueprint) -> None:
    """Point ``job`` at its next run.

    ``next_run_at`` must stay a real timestamp: the scheduler ignores jobs without one, so
    clearing it while realigning the cadence would silently kill the parser for the rest of
    the process. A scenario whose market snapshots are missing (fresh state, or a pair added
    since the last pass) is due immediately instead of waiting a whole interval.
    """
    now = services.clock()
    if _market_data_missing(services, blueprint):
        job.next_run_at = now
    elif job.cron is not None:
        job.next_run_at = job.cron.next_after(now)
    elif job.interval_minutes:
        job.next_run_at = now + timedelta(minutes=job.interval_minutes)
    else:  # pragma: no cover - Job validates that exactly one cadence is set
        job.next_run_at = None


def _market_data_missing(services: BotServices, blueprint: Blueprint) -> bool:
    """True when an enabled ``market_middle`` target has no stored snapshot."""
    for plan in blueprint.enabled_pairs():
        for platform, source in plan.sources.items():
            if source == MARKET_MIDDLE_SOURCE and services.market.get(platform, plan.pair) is None:
                return True
    return False
