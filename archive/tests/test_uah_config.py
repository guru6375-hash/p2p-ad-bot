"""``p2pbot/uah_config.py`` (STEP) and ``BotServices.set_uah_rate`` behind ``/setrate``."""

from __future__ import annotations

from decimal import Decimal

import pytest

from p2pbot import services as services_module, uah_config
from p2pbot.cli import main
from p2pbot.edit_queue import EditQueueReport
from p2pbot.errors import ConfigError
from p2pbot.services import build_services
from p2pbot.uah_config import describe_steps, load_uah_steps

from conftest import FakeClock, FakeTransport


def test_the_shipped_steps_are_binance_025_okx_001_bybit_001() -> None:
    assert load_uah_steps() == {
        "binance": Decimal("0.25"),
        "okx": Decimal("0.01"),
        "bybit": Decimal("0.01"),
    }
    assert describe_steps(load_uah_steps()) == "binance 0.25, okx 0.01, bybit 0.01"


def test_steps_accept_numbers_and_any_key_case() -> None:
    steps = load_uah_steps({"Binance": 0.5, "OKX": 0, "bybit": "0.02"})

    assert steps == {"binance": Decimal("0.5"), "okx": Decimal("0"), "bybit": Decimal("0.02")}


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (["0.25"], "uah_config.STEP must be a dict"),
        ({"binance": "0.25", "okx": "0.01", "bybit": "0.01", "kraken": "1"}, "unknown exchange 'kraken'"),
        ({"binance": "-0.25", "okx": "0.01", "bybit": "0.01"}, "must not be negative"),
        ({"binance": "abc", "okx": "0.01", "bybit": "0.01"}, "is not a valid decimal"),
        ({"binance": "0.25", "okx": "0.01"}, "needs a value for bybit"),
    ],
)
def test_bad_steps_are_refused_naming_the_setting(step: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_uah_steps(step)


def test_build_services_loads_the_steps_and_a_bad_step_stops_it(settings, monkeypatch) -> None:
    services = build_services(settings, transport=FakeTransport(), clock=FakeClock(), adapters={})
    assert services.uah_steps["binance"] == Decimal("0.25")

    monkeypatch.setattr(uah_config, "STEP", {"binance": "0.25"})
    with pytest.raises(ConfigError, match="uah_config.STEP needs a value for okx, bybit"):
        build_services(settings, transport=FakeTransport(), clock=FakeClock(), adapters={})


def test_set_uah_rate_runs_the_uah_queue_with_the_configured_steps(settings, monkeypatch) -> None:
    calls: list[dict] = []
    report = EditQueueReport((), (), (), ())

    def fake_queue(rate, publisher, *, steps, caps, dry_run):
        calls.append(
            {"rate": rate, "publisher": publisher, "steps": steps, "caps": caps, "dry_run": dry_run}
        )
        return report

    monkeypatch.setattr(services_module, "run_uah_rate_queue", fake_queue)
    services = build_services(
        settings,
        transport=FakeTransport(),
        clock=FakeClock(),
        adapters={},
        uah_steps={"binance": Decimal("0.30"), "okx": Decimal("0.02"), "bybit": Decimal("0.02")},
    )
    services.rates.set_cap("UAH/USDT", "46.00")  # as /setcap stores it

    assert services.set_uah_rate(Decimal("47.00"), dry_run=True) is report
    assert calls == [
        {
            "rate": Decimal("47.00"),
            "publisher": services.publisher,
            "steps": {"binance": Decimal("0.30"), "okx": Decimal("0.02"), "bybit": Decimal("0.02")},
            "caps": {"UAH/USDT": Decimal("46.00"), "UAH/USDC": None},  # read at call time
            "dry_run": True,
        }
    ]


def test_verify_config_checks_the_steps(env_factory, capsys, monkeypatch) -> None:
    assert main(["verify-config"], env=env_factory()) == 0
    assert "ok uah_config.py: STEP binance 0.25, okx 0.01, bybit 0.01" in capsys.readouterr().out

    monkeypatch.setattr(uah_config, "STEP", {"binance": "-1", "okx": "0.01", "bybit": "0.01"})
    assert main(["verify-config"], env=env_factory()) == 1
    assert "uah_config.STEP['binance'] must not be negative" in capsys.readouterr().err


def test_verify_config_names_the_disabled_exchanges(env_factory, capsys) -> None:
    assert main(["verify-config"], env=env_factory(DISABLED_EXCHANGES="okx")) == 0
    assert "disabled exchanges: okx (DISABLED_EXCHANGES)" in capsys.readouterr().out

    assert main(["verify-config"], env=env_factory(DISABLED_EXCHANGES="kraken")) == 1
    assert "DISABLED_EXCHANGES has unknown exchange(s) kraken" in capsys.readouterr().err
