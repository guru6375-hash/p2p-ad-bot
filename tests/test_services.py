"""Composition root: ScenarioManager, BotServices façade, job wiring (SPEC 11.5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot.blueprint import load_blueprint, parse_blueprint
from p2pbot.config import load_settings
from p2pbot.cron import CronExpression
from p2pbot.errors import BlueprintError, ConfigError, MissingCapError, MissingMarketDataError, TransportError
from p2pbot.market import MarketStore, build_snapshot
from p2pbot.models import AdActionResult, ComputedAd, Pair
from p2pbot.publisher import AdPublisher, AdStore
from p2pbot.rates import RateStore
from p2pbot.scheduler import Scheduler
from p2pbot.services import (
    BotServices,
    ScenarioManager,
    StatusSnapshot,
    build_services,
    configure_parser_job,
    validate_scenario_accounts,
)

from conftest import FakeClock, FakeTransport, make_ad, make_snapshot

UTC = timezone.utc

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


class _StubAdapter:
    """Adapter double: serves canned competitor ads and records ad requests."""

    def __init__(self, platform: str, *, ads=(), error: BaseException | None = None) -> None:
        self.platform = platform
        self.ads = tuple(ads)
        self.error = error
        self.searches = 0
        self.built: list[str] = []

    def search_ads(self, pair, *, filters=None, **kwargs):
        self.searches += 1
        if self.error is not None:
            raise self.error
        return build_snapshot(self.platform, pair, self.ads, filters)

    def build_login_request(self, account):
        return None

    def build_create_ad_request(self, account, spec, adv_no=None):
        self.built.append("create")
        return "create-request"

    def build_update_ad_request(self, account, spec, adv_no):
        self.built.append("update")
        return "update-request"

    def send_private(self, account, request):
        return {"ok": True}

    def parse_ad_result(self, payload, *, account, pair, spec, created):
        return AdActionResult(
            platform=self.platform,
            account_id=account.id,
            pair=pair,
            adv_no="2048",
            price=spec.price,
            created=created,
            raw=payload,
        )


def _settings(tmp_path: Path, **overrides) -> object:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_OWNER_ID": "4242",
        "STATE_PATH": str(tmp_path / "var" / "state.json"),
        "MARKET_PATH": str(tmp_path / "var" / "market.json"),
        "ADS_PATH": str(tmp_path / "var" / "ads.json"),
        "SCENARIOS_DIR": str(SCENARIOS),
        "LOG_PATH": "",
        "BINANCE_1_API_KEY": "k1",
        "BINANCE_1_SECRET_KEY": "s1",
        "BINANCE_2_API_KEY": "k2",
        "BINANCE_2_SECRET_KEY": "s2",
        "OKX_1_API_KEY": "ok",
        "OKX_1_SECRET_KEY": "os",
        "OKX_1_PASSPHRASE": "op",
        "BYBIT_1_API_KEY": "bk",
        "BYBIT_1_SECRET_KEY": "bs",
    }
    env.update({key.upper(): str(value) for key, value in overrides.items()})
    return load_settings(env_path=None, env=env, dotenv=False)


def _adapters(*, binance_error: BaseException | None = None) -> dict[str, _StubAdapter]:
    return {
        "binance": _StubAdapter(
            "binance",
            ads=[make_ad(platform="binance", pair="PLN/USDT", price="4.31")],
            error=binance_error,
        ),
        "okx": _StubAdapter(
            "okx", ads=[make_ad(platform="okx", pair="PLN/USDT", price="4.33")]
        ),
        "bybit": _StubAdapter("bybit"),
    }


def _services(tmp_path: Path, *, overrides=None, adapters=None, clock=None) -> BotServices:
    settings = _settings(tmp_path, **(overrides or {}))
    return build_services(
        settings,
        transport=FakeTransport(),
        clock=clock or FakeClock(),
        adapters=adapters if adapters is not None else _adapters(),
    )


# -- ScenarioManager -------------------------------------------------------------------
def test_scenario_manager_lists_the_shipped_scenarios(tmp_path: Path) -> None:
    manager = ScenarioManager(SCENARIOS, state_path=tmp_path / "scenario.json")
    assert manager.available() == ("pln", "uah")
    assert manager.active_name() is None
    assert ScenarioManager(tmp_path / "missing").available() == ()


def test_blueprint_without_an_active_scenario_is_a_config_error(tmp_path: Path) -> None:
    manager = ScenarioManager(SCENARIOS, state_path=tmp_path / "scenario.json")
    with pytest.raises(ConfigError, match="no active scenario; available: pln, uah"):
        manager.blueprint()


def test_activate_loads_validates_and_persists(tmp_path: Path) -> None:
    state = tmp_path / "scenario.json"
    manager = ScenarioManager(SCENARIOS, state_path=state)
    blueprint = manager.activate("uah")
    assert blueprint.name == "uah"
    assert manager.active_name() == "uah"
    assert manager.blueprint() is blueprint  # cached
    assert json.loads(state.read_text(encoding="utf-8")) == {"active_scenario": "uah"}

    reloaded = ScenarioManager(SCENARIOS, state_path=state)
    assert reloaded.active_name() == "uah"
    assert reloaded.blueprint().name == "uah"


def test_activate_unknown_scenario_is_reported(tmp_path: Path) -> None:
    manager = ScenarioManager(SCENARIOS, state_path=tmp_path / "scenario.json")
    with pytest.raises(BlueprintError, match="unknown scenario"):
        manager.activate("nope")


def test_activate_validates_the_accounts_against_settings(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = ScenarioManager(SCENARIOS, state_path=tmp_path / "scenario.json", settings=settings)
    assert manager.activate("uah").name == "uah"

    empty = load_settings(env_path=None, env={"SCENARIOS_DIR": str(SCENARIOS)}, dotenv=False)
    strict = ScenarioManager(SCENARIOS, state_path=tmp_path / "other.json", settings=empty)
    with pytest.raises(ConfigError, match="accounts missing from .env"):
        strict.activate("uah")


def test_reload_picks_up_an_edited_blueprint(tmp_path: Path) -> None:
    copy_dir = tmp_path / "scenarios"
    copy_dir.mkdir()
    target = copy_dir / "uah.json"
    target.write_text((SCENARIOS / "uah.json").read_text(encoding="utf-8"), encoding="utf-8")
    manager = ScenarioManager(copy_dir, state_path=tmp_path / "scenario.json")
    assert manager.activate("uah").name == "uah"

    data = json.loads(target.read_text(encoding="utf-8"))
    data["name"] = "uah-edited"
    target.write_text(json.dumps(data), encoding="utf-8")

    assert manager.blueprint().name == "uah"
    assert manager.reload().name == "uah-edited"


def test_corrupt_scenario_state_degrades_to_none(tmp_path: Path) -> None:
    state = tmp_path / "scenario.json"
    state.write_text("{not json", encoding="utf-8")
    manager = ScenarioManager(SCENARIOS, state_path=state)
    assert manager.active_name() is None
    assert manager.available() == ("pln", "uah")


def test_scenario_state_without_a_name_degrades_to_none(tmp_path: Path) -> None:
    state = tmp_path / "scenario.json"
    state.write_text(json.dumps({"active_scenario": ""}), encoding="utf-8")
    assert ScenarioManager(SCENARIOS, state_path=state).active_name() is None
    state.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert ScenarioManager(SCENARIOS, state_path=state).active_name() is None


def test_activation_without_a_state_path_still_works(tmp_path: Path) -> None:
    manager = ScenarioManager(SCENARIOS)
    assert manager.activate("pln").name == "pln"
    assert manager.active_name() == "pln"
    assert list(tmp_path.iterdir()) == []


def test_validate_scenario_accounts_reports_every_missing_account() -> None:
    blueprint = load_blueprint(SCENARIOS / "uah.json")
    present = {
        "Binance#1": object(),
        "Binance#2": object(),
        "Okx#1": object(),
    }
    with pytest.raises(ConfigError) as excinfo:
        validate_scenario_accounts(blueprint, present)
    message = str(excinfo.value)
    assert "UAH/USDT:Bybit#1" in message
    assert "UAH/USDC:Bybit#1" in message
    assert "Okx#1" not in message

    complete = {
        account_id: object()
        for plan in blueprint.pairs
        for account_id in plan.accounts
    }
    assert validate_scenario_accounts(blueprint, complete) is None


# -- BotServices: tolerant vs strict ---------------------------------------------------
def test_refresh_prices_publishes_what_it_can_and_records_problems(tmp_path: Path, caplog) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "46.50")  # UAH/USDC deliberately has no cap

    with caplog.at_level("WARNING"):
        results = services.refresh_prices(dry_run=True)

    assert [result.status for result in results] == ["dry_run"] * 4
    assert services.last_problems and len(services.last_problems) == 3
    for problem in services.last_problems:
        assert problem.startswith("UAH/USDC ")
        assert "MissingCapError" in problem
    for result in results:
        assert result.price is not None and result.price <= Decimal("46.50")
    assert "skipped 3 entry/entries" in caplog.text


def test_compute_is_the_strict_passthrough(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "46.50")
    with pytest.raises(MissingCapError):
        services.compute()
    ads, problems = services.compute_with_problems()
    assert len(ads) == 3
    assert len(problems) == 3


def test_refresh_prices_publishes_everything_when_nothing_is_missing(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")

    results = services.refresh_prices(dry_run=True)

    assert services.last_problems == ()
    assert len(results) == 8
    prices = {(result.platform, result.pair.symbol): result.price for result in results}
    assert prices[("binance", "UAH/USDC")] == Decimal("46.75")
    assert prices[("okx", "UAH/USDC")] == Decimal("46.99")


def test_refresh_prices_really_publishes_when_not_dry(tmp_path: Path) -> None:
    adapters = _adapters()
    services = _services(tmp_path, adapters=adapters)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")

    results = services.refresh_prices()

    assert {result.status for result in results} == {"created"}
    assert adapters["binance"].built.count("create") == 4  # 2 accounts x USDT/USDC
    assert services.publisher.store.get("Binance#1", "UAH/USDT").adv_no == "2048"
    assert services.last_publish == results
    assert (tmp_path / "var" / "ads.json").is_file()


def test_set_active_limits_to_the_requested_pairs(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")

    results = services.set_active(False, pairs=["uah/usdc"])

    assert {result.pair.symbol for result in results} == {"UAH/USDC"}
    assert services.last_publish == results
    assert services.last_problems == ()


def test_set_active_for_a_pair_with_no_prices_returns_nothing(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    before = services.last_publish
    assert services.set_active(True, pairs=["UAH/TRY"]) == ()
    assert services.last_publish == before


def test_set_active_records_problems_without_raising(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    results = services.set_active(False)
    assert len(results) == 4
    assert len(services.last_problems) == 3


def test_run_parser_uses_the_active_scenario(tmp_path: Path) -> None:
    adapters = _adapters()
    services = _services(tmp_path, adapters=adapters)
    services.scenarios.activate("pln")

    results = services.run_parser()

    assert [(row.platform, row.pair.symbol) for row in results] == [
        ("binance", "PLN/USDT"),
        ("okx", "PLN/USDT"),
        ("binance", "PLN/USDC"),
        ("okx", "PLN/USDC"),
    ]
    assert adapters["binance"].searches == 2
    assert services.market.middle("binance", "PLN/USDT") == Decimal("4.31")


def test_run_parser_of_a_scenario_without_market_sources(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    assert services.run_parser() == ()


# -- snapshot --------------------------------------------------------------------------
def test_snapshot_without_an_active_scenario(tmp_path: Path) -> None:
    services = _services(tmp_path)
    snapshot = services.snapshot()
    assert isinstance(snapshot, StatusSnapshot)
    assert snapshot.scenario is None
    assert snapshot.fiat is None and snapshot.strategy is None
    assert snapshot.rates == () and snapshot.prices == ()
    assert snapshot.engine_problems == ()
    assert snapshot.engine_error is not None
    assert [job.name for job in snapshot.jobs] == ["parser"]


def test_snapshot_with_a_healthy_scenario(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")
    services.market.put(make_snapshot(platform="binance", pair="UAH/USDT", prices=("46.90", "47.10")))

    snapshot = services.snapshot()

    assert (snapshot.scenario, snapshot.fiat, snapshot.strategy) == ("uah", "UAH", "fixed_spread")
    assert snapshot.engine_error is None
    assert snapshot.engine_problems == ()
    assert [(row.pair, row.base, row.cap) for row in snapshot.rates] == [
        ("UAH/USDT", Decimal("47.00"), Decimal("47.20")),
        ("UAH/USDC", None, Decimal("47.20")),
    ]
    assert len(snapshot.prices) == 6
    assert snapshot.market[0].middle == Decimal("47.00")
    assert snapshot.market[0].filtered == 2


def test_snapshot_reports_engine_problems(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")

    snapshot = services.snapshot()

    assert len(snapshot.engine_problems) == 3
    assert snapshot.engine_error == snapshot.engine_problems[0]
    assert len(snapshot.prices) == 3


def test_snapshot_carries_the_last_publish_results(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")
    results = services.refresh_prices(dry_run=True)
    assert services.snapshot().last_publish == results


# -- wiring ----------------------------------------------------------------------------
def test_build_services_wires_every_component(tmp_path: Path) -> None:
    clock = FakeClock()
    settings = _settings(tmp_path, SCENARIO="pln")
    states = tmp_path / "var"
    states.mkdir(parents=True)
    seed = RateStore(path=settings.state_path)
    seed.set_cap("PLN/USDT", "10.00")
    seed.save()
    market_seed = MarketStore(path=settings.market_path)
    market_seed.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=("4.31",)))
    market_seed.save()
    store_seed = AdStore(path=settings.ads_path)
    store_seed.save()

    adapters = _adapters()
    services = build_services(
        settings, transport=FakeTransport(), clock=clock, adapters=adapters
    )

    assert isinstance(services.publisher, AdPublisher)
    assert isinstance(services.scheduler, Scheduler)
    assert services.scheduler.now() == clock()
    assert services.clock is clock
    assert set(services.adapters) == {"binance", "okx", "bybit"}
    assert services.parser.market is services.market
    assert services.rates.cap("PLN/USDT") == Decimal("10.00")
    assert services.market.middle("binance", "PLN/USDT") == Decimal("4.31")
    assert services.scenarios.active_name() == "pln"  # from the SCENARIO switch
    assert services.settings is settings


def test_parser_job_is_registered_by_default(tmp_path: Path) -> None:
    services = _services(tmp_path)
    job = services.scheduler.get("parser")
    assert job.interval_minutes == 25
    assert job.next_run_at is not None


def test_refresh_job_is_registered_when_configured(tmp_path: Path) -> None:
    services = _services(tmp_path, overrides={"REFRESH_INTERVAL_MINUTES": "10"})
    assert [job.name for job in services.scheduler.jobs()] == ["parser", "prices"]
    assert services.scheduler.get("prices").interval_minutes == 10


@pytest.mark.parametrize("value", ["", "0", "-5", "  "])
def test_refresh_job_is_skipped_for_disabled_or_invalid_values(tmp_path: Path, value: str) -> None:
    services = _services(tmp_path, overrides={"REFRESH_INTERVAL_MINUTES": value})
    assert [job.name for job in services.scheduler.jobs()] == ["parser"]


def test_refresh_job_rejects_a_non_numeric_interval(tmp_path: Path) -> None:
    settings = _settings(tmp_path, REFRESH_INTERVAL_MINUTES="soon")
    with pytest.raises(ConfigError, match="REFRESH_INTERVAL_MINUTES must be an integer"):
        build_services(
            settings, transport=FakeTransport(), clock=FakeClock(), adapters=_adapters()
        )


def test_parser_job_runs_a_parse_and_a_refresh(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "47.20")
    services.rates.set_cap("UAH/USDC", "47.20")

    summary = services.scheduler.get("parser").func()

    assert summary == "parser: 0 fetches, 0 failed, 8 ads pushed"


def test_parser_job_summary_lists_venue_failures(tmp_path: Path) -> None:
    adapters = _adapters(binance_error=TransportError("venue down"))
    services = _services(tmp_path, adapters=adapters)
    services.scenarios.activate("pln")
    services.rates.set_cap("PLN/USDT", "10.00")
    services.rates.set_cap("PLN/USDC", "10.00")

    summary = services.scheduler.get("parser").func()

    assert "4 fetches, 2 failed" in summary
    assert "binance PLN/USDT: TransportError: venue down" in summary


# -- parser job alignment ---------------------------------------------------------------
def test_configure_parser_job_removes_it_for_a_non_parsing_scenario(tmp_path: Path) -> None:
    services = _services(tmp_path)
    assert services.scheduler.get("parser") is not None
    result = configure_parser_job(services, load_blueprint(SCENARIOS / "uah.json"))
    assert result is None
    with pytest.raises(KeyError):
        services.scheduler.get("parser")


def test_configure_parser_job_uses_the_blueprint_cron(tmp_path: Path) -> None:
    services = _services(tmp_path)
    job = configure_parser_job(services, load_blueprint(SCENARIOS / "pln.json"))
    assert job is not None
    assert job.cron == CronExpression.parse("*/25 * * * *")
    assert job.interval_minutes is None
    # the job must stay scheduled: a None next_run_at would make Scheduler.due() skip it forever
    assert job.next_run_at is not None
    assert services.scheduler.due(job.next_run_at) == (job,)


def test_configure_parser_job_uses_the_interval_when_there_is_no_cron(tmp_path: Path) -> None:
    blueprint = parse_blueprint(
        {
            "version": 1,
            "name": "interval",
            "fiat": "PLN",
            "strategy": "market_middle",
            "parser": {"enabled": True, "interval_minutes": 15},
            "pairs": [
                {"pair": "PLN/USDT", "anchor": True, "accounts": ["Binance#1", "Okx#1", "Bybit#1"]}
            ],
        }
    )
    services = _services(tmp_path)
    job = configure_parser_job(services, blueprint)
    assert job is not None
    assert job.interval_minutes == 15
    assert job.cron is None


def test_configure_parser_job_creates_a_missing_job(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scheduler.remove("parser")
    blueprint = load_blueprint(SCENARIOS / "pln.json")
    job = configure_parser_job(services, blueprint)
    assert job is not None
    assert services.scheduler.get("parser") is job
    assert job.cron is not None



def test_snapshot_prices_never_exceed_the_cap_when_venues_fail(tmp_path: Path) -> None:
    """End-to-end safety property with a partially failing market (SPEC 7.1 + 10)."""
    adapters = _adapters(binance_error=TransportError("down"))
    services = _services(tmp_path, adapters=adapters)
    services.scenarios.activate("pln")
    services.rates.set_cap("PLN/USDT", "4.20")
    services.rates.set_cap("PLN/USDC", "4.20")
    adapters["okx"].ads = (make_ad(platform="okx", pair="PLN/USDT", price="4.31"),)
    services.run_parser()

    ads, problems = services.compute_with_problems()

    assert ads  # the healthy venue still priced
    for ad in ads:
        assert ad.price <= Decimal("4.20")
    assert any("binance" in problem for problem in problems)


def test_missing_market_data_is_isolated_per_platform(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("pln")
    services.rates.set_cap("PLN/USDT", "10.00")
    services.rates.set_cap("PLN/USDC", "10.00")
    services.market.put(make_snapshot(platform="binance", pair="PLN/USDT", prices=("4.31",)))

    ads, problems = services.compute_with_problems()

    by_platform = {ad.platform for ad in ads if ad.pair == Pair.parse("PLN/USDT")}
    assert by_platform == {"binance", "bybit"}  # bybit copies the healthy binance price
    assert by_platform.isdisjoint({"okx"})
    assert any(problem.startswith("PLN/USDT okx") for problem in problems)
    with pytest.raises(MissingMarketDataError):
        services.compute()


def test_services_timestamp_helpers_use_the_injected_clock(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 1, 6, 0, tzinfo=UTC))
    services = _services(tmp_path, clock=clock)
    assert services.clock() == datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
    assert services.snapshot().version == services.version
    assert services.last_publish == ()


def test_computed_ads_from_services_are_ordered_and_capped(tmp_path: Path) -> None:
    services = _services(tmp_path)
    services.scenarios.activate("uah")
    services.rates.set_base("UAH/USDT", "47.00")
    services.rates.set_cap("UAH/USDT", "46.50")
    services.rates.set_cap("UAH/USDC", "46.50")
    ads: tuple[ComputedAd, ...] = services.compute()
    assert [ad.pair.symbol for ad in ads] == ["UAH/USDT"] * 3 + ["UAH/USDC"] * 3
    assert all(ad.clamped for ad in ads)
    assert {ad.price for ad in ads} == {Decimal("46.50")}
    assert all(ad.price <= ad.cap for ad in ads)


# -- parser job scheduling (the three cadence branches) --------------------------------
def test_parser_job_is_due_immediately_when_the_market_is_empty(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    services = _services(tmp_path, clock=clock)
    job = configure_parser_job(services, load_blueprint(SCENARIOS / "pln.json"))
    assert job is not None
    assert job.next_run_at == clock()
    assert services.scheduler.due(clock()) == (job,)


def _pln_interval_blueprint():
    return parse_blueprint(
        {
            "version": 1,
            "name": "interval",
            "fiat": "PLN",
            "strategy": "market_middle",
            "parser": {"enabled": True, "interval_minutes": 25},
            "pairs": [
                {
                    "pair": "PLN/USDT",
                    "anchor": True,
                    "accounts": ["Binance#1", "Okx#1", "Bybit#1"],
                },
                {
                    "pair": "PLN/USDC",
                    "anchor": False,
                    "linked_to": "PLN/USDT",
                    "accounts": ["Binance#1", "Okx#1", "Bybit#1"],
                },
            ],
        }
    )


def test_parser_job_waits_a_full_interval_when_the_market_is_populated(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    services = _services(tmp_path, clock=clock)
    for pair in ("PLN/USDT", "PLN/USDC"):
        for platform in ("binance", "okx"):
            services.market.put(make_snapshot(platform=platform, pair=pair, prices=("4.31",)))

    job = configure_parser_job(services, _pln_interval_blueprint())

    assert job is not None
    assert job.next_run_at == clock() + timedelta(minutes=25)
    assert services.scheduler.due(clock()) == ()
    assert services.scheduler.due(clock() + timedelta(minutes=25)) == (job,)


def test_parser_job_uses_the_cron_cadence_and_fires_on_the_next_slot(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
    services = _services(tmp_path, clock=clock)
    for pair in ("PLN/USDT", "PLN/USDC"):
        for platform in ("binance", "okx"):
            services.market.put(make_snapshot(platform=platform, pair=pair, prices=("4.31",)))

    job = configure_parser_job(services, load_blueprint(SCENARIOS / "pln.json"))

    assert job is not None
    assert job.next_run_at == datetime(2026, 1, 1, 0, 25, tzinfo=UTC)
    assert services.scheduler.due(clock()) == ()
    assert services.scheduler.due(datetime(2026, 1, 1, 0, 26, tzinfo=UTC)) == (job,)


def test_parser_job_round_trip_between_uah_and_pln(tmp_path: Path) -> None:
    services = _services(tmp_path)

    assert configure_parser_job(services, load_blueprint(SCENARIOS / "uah.json")) is None
    with pytest.raises(KeyError):
        services.scheduler.get("parser")

    job = configure_parser_job(services, load_blueprint(SCENARIOS / "pln.json"))
    assert job is not None
    assert services.scheduler.get("parser") is job
    assert job.next_run_at is not None
    assert services.scheduler.due(job.next_run_at) == (job,)


def test_snapshot_reports_an_engine_that_raises_while_computing(tmp_path: Path) -> None:
    """A façade whose engine blows up wholesale still produces a status snapshot."""
    services = _services(tmp_path)
    services.scenarios.activate("uah")

    class _ExplodingEngine:
        def compute(self):
            raise MissingCapError("no cap_rate stored for UAH/USDT")

        def compute_with_problems(self):
            raise MissingCapError("no cap_rate stored for UAH/USDT")

    services.engine_factory = lambda blueprint: _ExplodingEngine()

    snapshot = services.snapshot()

    assert snapshot.engine_error == "no cap_rate stored for UAH/USDT"
    assert snapshot.prices == ()
    assert snapshot.engine_problems == ()
    assert snapshot.scenario == "uah"
