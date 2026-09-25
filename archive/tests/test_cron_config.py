"""``p2pbot/cron_config.py``: the settings, their validation and the ``pln-edits`` cron job."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from p2pbot import cron_config, services as services_module
from p2pbot.cron_config import CronConfig, load_cron_config
from p2pbot.edit_queue import AdEdit, EditQueueReport
from p2pbot.errors import ConfigError
from p2pbot.models import Pair, PublishResult
from p2pbot.services import build_services, run_cron_edits

from conftest import FakeClock, FakeTransport

PLN_USDT = Pair.parse("PLN/USDT")
PLN_USDC = Pair.parse("PLN/USDC")
UTC = timezone.utc


# -- the settings ----------------------------------------------------------------------
def test_the_shipped_settings_run_every_25_minutes_over_both_pln_pairs() -> None:
    assert cron_config.SCHEDULE_TIME == 25
    assert cron_config.PAIRS == ["USDT", "USDC"]
    assert load_cron_config() == CronConfig(interval_minutes=25, pairs=(PLN_USDT, PLN_USDC))


def test_tickers_and_full_symbols_both_name_a_pln_pair() -> None:
    config = load_cron_config(10, ["usdt", "PLN/USDC", " BTC "])

    assert config.interval_minutes == 10
    assert config.pairs == (PLN_USDT, PLN_USDC, Pair.parse("PLN/BTC"))
    assert config.describe() == "every 10 min, pairs PLN/USDT, PLN/USDC, PLN/BTC"


@pytest.mark.parametrize("value", [0, -5, "25", 2.5, True])
def test_schedule_time_must_be_a_positive_whole_number(value: object) -> None:
    with pytest.raises(ConfigError, match="cron_config.SCHEDULE_TIME must be a positive whole"):
        load_cron_config(schedule_time=value)


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        ([], "cron_config.PAIRS must be a non-empty list"),
        ("USDT", "cron_config.PAIRS must be a non-empty list"),
        ({"USDT": 1}, "cron_config.PAIRS must be a non-empty list"),
        ([5], "entries must be tickers"),
        ([""], "entries must be tickers"),
        (["US$T"], "is not a valid ticker"),
        (["UAH/USDT"], "is not a PLN pair"),
        (["USDT", "PLN/USDT"], "lists PLN/USDT twice"),
    ],
)
def test_bad_pairs_are_refused_naming_the_setting(pairs: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_cron_config(pairs=pairs)


# -- the cron job ----------------------------------------------------------------------
def _services(settings, clock, cron=None):
    return build_services(settings, transport=FakeTransport(), clock=clock, adapters={}, cron=cron)


def test_build_services_registers_the_pln_edits_job_on_schedule_time(settings) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))

    services = _services(settings, clock, cron=load_cron_config(10, ["USDT"]))

    job = services.scheduler.get("pln-edits")
    assert job.interval_minutes == 10
    assert job.next_run_at == clock() + timedelta(minutes=10)
    assert services.cron.pairs == (PLN_USDT,)


def test_build_services_uses_the_file_settings_by_default(settings, monkeypatch) -> None:
    monkeypatch.setattr(cron_config, "SCHEDULE_TIME", 7)
    monkeypatch.setattr(cron_config, "PAIRS", ["USDC"])

    services = _services(settings, FakeClock())

    assert services.scheduler.get("pln-edits").interval_minutes == 7
    assert services.cron.pairs == (PLN_USDC,)


def test_a_bad_setting_stops_the_bot_before_anything_runs(settings, monkeypatch) -> None:
    monkeypatch.setattr(cron_config, "SCHEDULE_TIME", 0)

    with pytest.raises(ConfigError, match="cron_config.SCHEDULE_TIME"):
        _services(settings, FakeClock())


def _fake_queue(calls: list[dict], report: EditQueueReport):
    def run(blueprint, engine, publisher, *, parser, dry_run, pairs):
        calls.append({"scenario": blueprint.name, "dry_run": dry_run, "pairs": pairs})
        return report

    return run


def test_run_cron_edits_runs_the_queue_over_the_configured_pairs(settings, monkeypatch) -> None:
    edit = AdEdit("Binance#1", "binance", PLN_USDT, "1", Decimal("3.83"), "market_middle", "buy")
    report = EditQueueReport(
        queued=(edit,),
        unchanged=(),
        results=(PublishResult("Binance#1", "binance", PLN_USDT, "updated", price=Decimal("3.83")),),
        problems=("Okx#1: cannot list ads: 404",),
    )
    calls: list[dict] = []
    monkeypatch.setattr(services_module, "run_pln_edit_queue", _fake_queue(calls, report))
    services = _services(settings, FakeClock(), cron=load_cron_config(25, ["USDT"]))
    services.scenarios.activate("pln")

    summary = run_cron_edits(services)

    assert calls == [{"scenario": "pln", "dry_run": False, "pairs": (PLN_USDT,)}]
    assert summary == (
        "pln-edits: 1 queued, 1 edited, 0 failed, 0 unchanged | "
        "1 problem(s): Okx#1: cannot list ads: 404"
    )


def test_the_scheduler_fires_the_job_every_schedule_time(settings, monkeypatch) -> None:
    calls: list[dict] = []
    empty = EditQueueReport((), (), (), ())
    monkeypatch.setattr(services_module, "run_pln_edit_queue", _fake_queue(calls, empty))
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    services = _services(settings, clock, cron=load_cron_config(25, ["USDT", "USDC"]))
    services.scenarios.activate("pln")
    job = services.scheduler.get("pln-edits")

    assert job not in services.scheduler.run_due(clock() + timedelta(minutes=24))
    assert job in services.scheduler.run_due(clock() + timedelta(minutes=25))
    assert job in services.scheduler.run_due(clock() + timedelta(minutes=50))

    assert [call["pairs"] for call in calls] == [(PLN_USDT, PLN_USDC)] * 2
    assert job.last_result == "pln-edits: 0 queued, 0 edited, 0 failed, 0 unchanged"
    assert job.last_error is None


def test_a_job_without_an_active_scenario_records_the_error(settings) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    services = _services(settings, clock)
    job = services.scheduler.get("pln-edits")

    services.scheduler.run_due(clock() + timedelta(minutes=25))

    assert "no active scenario" in (job.last_error or "")
