"""The bot façade: wiring, /getads and the two /setrate queues."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot import services as services_module, uah_config
from p2pbot.config import load_settings
from p2pbot.edit_queue import EditQueueReport
from p2pbot.errors import ConfigError
from p2pbot.publisher import AdPublisher
from p2pbot.services import build_services

from conftest import FakeClock, FakeTransport


def _settings(tmp_path: Path, **overrides) -> object:
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
        "TELEGRAM_OWNER_ID": "4242",
        "STATE_PATH": str(tmp_path / "var" / "state.json"),
        "MARKET_PATH": str(tmp_path / "var" / "market.json"),
        "ADS_PATH": str(tmp_path / "var" / "ads.json"),
        "LOG_PATH": "",
        "BINANCE_1_API_KEY": "k1",
        "BINANCE_1_SECRET_KEY": "s1",
        "BYBIT_1_API_KEY": "bk",
        "BYBIT_1_SECRET_KEY": "bs",
    }
    env.update({key.upper(): str(value) for key, value in overrides.items()})
    return load_settings(env_path=None, env=env, dotenv=False)


class _ListingAdapter:
    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.listed: list[str] = []

    def fetch_own_ads(self, account, *, include_closed=False):
        self.listed.append(account.id)
        return ()


def test_build_services_wires_the_publisher_and_the_steps(tmp_path: Path) -> None:
    clock = FakeClock()
    settings = _settings(tmp_path)
    adapters = {"binance": _ListingAdapter("binance"), "bybit": _ListingAdapter("bybit")}

    services = build_services(settings, transport=FakeTransport(), clock=clock, adapters=adapters)

    assert isinstance(services.publisher, AdPublisher)
    assert services.publisher.adapters == adapters
    assert services.adapters == adapters
    assert services.settings is settings
    assert services.clock is clock
    assert services.uah_steps == {"binance": Decimal("0.25"), "bybit": Decimal("0.01")}
    for archived in ("scheduler", "rates", "scenarios", "market", "parser", "engine_factory"):
        assert not hasattr(services, archived)


def test_get_own_ads_lists_every_enabled_account(tmp_path: Path) -> None:
    adapters = {"binance": _ListingAdapter("binance"), "bybit": _ListingAdapter("bybit")}
    services = build_services(_settings(tmp_path), adapters=adapters, clock=FakeClock())

    listings = services.get_own_ads()

    assert [listing.account_id for listing in listings] == ["Binance#1", "Bybit#1"]


def test_set_uah_rate_runs_the_uah_ladder_with_the_configured_steps(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    report = EditQueueReport((), (), (), ())

    def fake_queue(rate, publisher, *, steps, dry_run):
        calls.append({"rate": rate, "publisher": publisher, "steps": steps, "dry_run": dry_run})
        return report

    monkeypatch.setattr(services_module, "run_uah_rate_queue", fake_queue)
    steps = {"binance": Decimal("0.30"), "bybit": Decimal("0.02")}
    services = build_services(_settings(tmp_path), adapters={}, clock=FakeClock(), uah_steps=steps)

    assert services.set_uah_rate(Decimal("47.00"), dry_run=True) is report
    assert calls == [
        {"rate": Decimal("47.00"), "publisher": services.publisher, "steps": steps, "dry_run": True}
    ]


def test_set_pln_rate_runs_the_flat_pln_queue(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []
    report = EditQueueReport((), (), (), ())

    def fake_queue(rate, publisher, *, dry_run):
        calls.append({"rate": rate, "publisher": publisher, "dry_run": dry_run})
        return report

    monkeypatch.setattr(services_module, "run_pln_rate_queue", fake_queue)
    services = build_services(_settings(tmp_path), adapters={}, clock=FakeClock())

    assert services.set_pln_rate(Decimal("3.85")) is report
    assert calls == [{"rate": Decimal("3.85"), "publisher": services.publisher, "dry_run": False}]


def test_a_bad_step_stops_build_services(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(uah_config, "STEP", {"binance": "-1"})

    with pytest.raises(ConfigError, match="must not be negative"):
        build_services(_settings(tmp_path), adapters={}, clock=FakeClock())
