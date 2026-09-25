"""``p2pbot/uah_config.py``: the STEP of the UAH ``/setrate`` ladder."""

from __future__ import annotations

from decimal import Decimal

import pytest

from p2pbot import uah_config
from p2pbot.cli import main
from p2pbot.errors import ConfigError
from p2pbot.uah_config import describe_steps, load_uah_steps


def test_the_shipped_steps_are_binance_025_bybit_001_and_okx_is_off() -> None:
    assert load_uah_steps() == {"binance": Decimal("0.25"), "bybit": Decimal("0.01")}
    assert describe_steps(load_uah_steps()) == "binance 0.25, bybit 0.01"


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
        ({}, "needs a value for at least one exchange"),
    ],
)
def test_bad_steps_are_refused_naming_the_setting(step: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_uah_steps(step)


def test_verify_config_checks_the_steps(env_factory, capsys, monkeypatch) -> None:
    assert main(["verify-config"], env=env_factory()) == 0
    assert "ok uah_config.py: STEP binance 0.25, bybit 0.01" in capsys.readouterr().out

    monkeypatch.setattr(uah_config, "STEP", {"binance": "-1", "okx": "0.01", "bybit": "0.01"})
    assert main(["verify-config"], env=env_factory()) == 1
    assert "uah_config.STEP['binance'] must not be negative" in capsys.readouterr().err


def test_verify_config_names_the_disabled_exchanges(env_factory, capsys) -> None:
    assert main(["verify-config"], env=env_factory(DISABLED_EXCHANGES="okx")) == 0
    assert "disabled exchanges: okx (DISABLED_EXCHANGES)" in capsys.readouterr().out

    assert main(["verify-config"], env=env_factory(DISABLED_EXCHANGES="kraken")) == 1
    assert "DISABLED_EXCHANGES has unknown exchange(s) kraken" in capsys.readouterr().err
