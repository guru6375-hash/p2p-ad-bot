"""Command router: every command, its arguments, malformed input and error reporting."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from p2pbot import constants
from p2pbot.errors import ConfigError
from p2pbot.models import ComputedAd, OwnAd, OwnAdsResult, Pair
from p2pbot.rates import RateStore
from p2pbot.telegram.api import Message, Update
from p2pbot.telegram.handlers import (
    HELP_TEXT,
    Dispatcher,
    HandlerResult,
)

from conftest import (
    StubRateRow,
    StubScenarioManager,
    StubServices,
    StubStatusSnapshot,
)


def _update(text: str, *, from_id: int = 4242) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            chat_id=from_id,
            chat_type="private",
            from_id=from_id,
            text=text,
        ),
        raw={},
    )


@pytest.fixture
def dispatcher(stub_services: StubServices) -> Dispatcher:
    return Dispatcher(stub_services)


# -- help / unknown --------------------------------------------------------------------
def test_help_lists_every_command() -> None:
    for name, description in constants.TELEGRAM_COMMANDS:
        assert f"/{name} - {description}" in HELP_TEXT
    assert "never accepted" in HELP_TEXT


def test_start_and_help_return_the_usage_summary(dispatcher: Dispatcher) -> None:
    for command in ("/start", "/help"):
        assert dispatcher.dispatch(_update(command)) == HandlerResult(HELP_TEXT)


def test_unknown_command_and_free_text_get_the_help_hint(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/nope")) == HandlerResult(
        "Unknown command. Send /help for the command list."
    )
    assert dispatcher.dispatch(_update("hello there")) == HandlerResult(
        "Unknown command. Send /help for the command list."
    )


def test_command_with_a_bot_suffix_is_routed(dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/rates@p2p_manager_bot"))
    assert result is not None
    assert result.text.startswith("rates:")


def test_updates_without_text_or_message_are_ignored(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(Update(update_id=1, raw={})) is None
    assert dispatcher.dispatch(_update("   ")) is None


def test_commands_without_arguments_still_work(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/rates")) is not None
    assert dispatcher.dispatch(_update("/scenarios")) is not None


def test_dispatcher_exposes_the_registered_commands(dispatcher: Dispatcher) -> None:
    names = set(dispatcher.commands)
    assert names == {name for name, _ in constants.TELEGRAM_COMMANDS}
    for removed in ("parse", "status", "version", "publish", "pause", "resume"):
        assert dispatcher.dispatch(_update(f"/{removed}")) == HandlerResult(
            "Unknown command. Send /help for the command list."
        )


# -- /setbase --------------------------------------------------------------------------
def test_setbase_stores_and_persists_the_rate(
    stub_services: StubServices, tmp_path: Path
) -> None:
    stub_services.rates = RateStore(path=tmp_path / "state.json")
    dispatcher = Dispatcher(stub_services)

    result = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))
    assert result is not None
    assert result.text.splitlines()[0] == "base_rate UAH/USDT = 47.00"
    assert stub_services.rates.base("UAH/USDT") == Decimal("47.00")
    assert (tmp_path / "state.json").is_file()
    assert RateStore.load(tmp_path / "state.json").base("UAH/USDT") == Decimal("47.00")


def test_setbase_warns_when_the_base_is_above_the_cap(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.rates.set_cap("UAH/USDT", "46.00")
    result = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))
    assert result is not None
    assert "WARNING: base_rate is above cap 46.00" in result.text


@pytest.mark.parametrize(
    "text",
    [
        "/setbase",
        "/setbase UAH/USDT",
        "/setbase UAH/USDT abc",
        "/setbase UAH/USDT 0",
        "/setbase UAH/USDT -1",
        "/setbase UAHUSDT 47.00",
        "/setbase UAH/USDT 47.00 extra",
    ],
)
def test_setbase_malformed_arguments_show_the_usage(dispatcher: Dispatcher, text: str) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult("Usage: /setbase <PAIR> <RATE>")


# -- /setcap ---------------------------------------------------------------------------
def test_setcap_stores_the_cap_and_reports_the_base(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.rates.set_base("UAH/USDT", "47.00")
    result = dispatcher.dispatch(_update("/setcap UAH/USDT 47.20"))
    assert result is not None
    lines = result.text.splitlines()
    assert lines[0] == "cap_rate UAH/USDT = 47.20"
    assert "advertisement prices can never exceed the cap" in lines[1]
    assert lines[2] == "base_rate UAH/USDT = 47.00"
    assert stub_services.rates.cap("UAH/USDT") == Decimal("47.20")


def test_setcap_flags_a_base_above_the_new_cap(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.rates.set_base("UAH/USDT", "47.00")
    result = dispatcher.dispatch(_update("/setcap UAH/USDT 46.00"))
    assert result is not None
    assert "(base_rate is currently above the cap)" in result.text


@pytest.mark.parametrize("text", ["/setcap", "/setcap UAH/USDT", "/setcap UAH/USDT 0", "/setcap x y"])
def test_setcap_malformed_arguments_show_the_usage(dispatcher: Dispatcher, text: str) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult("Usage: /setcap <PAIR> <RATE>")


# -- /rates ----------------------------------------------------------------------------
def test_rates_renders_base_cap_and_computed_prices(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    stub_services.snapshot_override = StubStatusSnapshot(
        version="1.0.0",
        scenario="uah",
        fiat="UAH",
        strategy="fixed_spread",
        rates=(StubRateRow(pair="UAH/USDT", base=Decimal("47.00"), cap=Decimal("47.20")),),
        prices=(
            ComputedAd(
                pair=Pair.parse("UAH/USDT"),
                platform="binance",
                price=Decimal("47.00"),
                source="base_rate",
                cap=Decimal("47.20"),
            ),
        ),
    )
    result = dispatcher.dispatch(_update("/rates"))
    assert result is not None
    assert "rates:" in result.text
    assert "UAH/USDT base 47.00 cap 47.20" in result.text
    assert "UAH/USDT binance 47.00 base_rate" in result.text


def test_rates_reports_missing_values_as_dashes(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    stub_services.snapshot_override = StubStatusSnapshot(
        version="1.0.0",
        scenario=None,
        fiat=None,
        strategy=None,
        rates=(StubRateRow(pair="UAH/USDT", base=None, cap=None),),
    )
    result = dispatcher.dispatch(_update("/rates"))
    assert result is not None
    assert "UAH/USDT base - cap -" in result.text
    assert "prices:\n  none" in result.text


# -- /scenarios and /scenario ----------------------------------------------------------
def test_scenarios_lists_available_and_active(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    stub_services.scenarios = StubScenarioManager(names=("pln", "uah"), active="uah")
    result = dispatcher.dispatch(_update("/scenarios"))
    assert result == HandlerResult("scenarios: pln, uah\nactive: uah")


def test_scenarios_without_files(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    stub_services.scenarios = StubScenarioManager(names=())
    assert dispatcher.dispatch(_update("/scenarios")) == HandlerResult("scenarios: none available")


def test_scenario_activates_and_reports_the_plan(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    manager = StubScenarioManager(names=("uah", "pln"))
    stub_services.scenarios = manager
    result = dispatcher.dispatch(_update("/scenario pln"))
    assert result is not None
    assert manager.activated == ["pln"]
    assert result.text == "scenario 'pln' activated: fiat=UAH strategy=fixed_spread pairs=2"


def test_scenario_usage_and_unknown_scenario(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/scenario")) == HandlerResult("Usage: /scenario <NAME>")
    assert dispatcher.dispatch(_update("/scenario a b")) == HandlerResult("Usage: /scenario <NAME>")

    stub_services.scenarios = StubScenarioManager(names=("uah",))
    result = dispatcher.dispatch(_update("/scenario nope"))
    assert result is not None
    assert result.text.startswith("BlueprintError: unknown scenario 'nope'")


def test_scenario_failure_is_reported_as_text(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    manager = StubScenarioManager()
    manager.activate_error = ConfigError("scenario references accounts missing from .env: Binance#9")
    stub_services.scenarios = manager
    result = dispatcher.dispatch(_update("/scenario uah"))
    assert result is not None
    assert result.text.startswith("ConfigError: scenario references accounts missing")


# -- skipped-entry reporting -----------------------------------------------------------
def test_rates_lists_engine_problems_with_a_cap(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    problems = tuple(f"PLN/USDC okx: MissingMarketDataError: no data {index}" for index in range(12))
    stub_services.snapshot_override = StubStatusSnapshot(
        version="1.0.0", scenario="pln", fiat="PLN", strategy="market_middle", engine_problems=problems
    )
    result = dispatcher.dispatch(_update("/rates"))
    assert result is not None
    text = result.text
    assert "skipped 12:" in text
    assert "PLN/USDC okx: MissingMarketDataError: no data 0" in text
    assert "PLN/USDC okx: MissingMarketDataError: no data 9" in text
    assert "... and 2 more" in text


def test_rates_without_problems_has_no_skipped_block(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.snapshot_override = StubStatusSnapshot(
        version="1.0.0", scenario="pln", fiat="PLN", strategy="market_middle"
    )
    result = dispatcher.dispatch(_update("/rates"))
    assert result is not None
    assert "skipped" not in result.text


def test_setbase_and_setcap_only_store_the_rate(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    base = dispatcher.dispatch(_update("/setbase PLN/USDT 3.85"))
    cap = dispatcher.dispatch(_update("/setcap PLN/USDT 3.90"))

    assert base is not None and base.text.splitlines() == ["base_rate PLN/USDT = 3.85"]
    assert cap is not None and cap.text.splitlines()[-1] == "base_rate PLN/USDT = 3.85"
    assert not hasattr(stub_services, "refresh_prices")  # the façade cannot push ads


def test_rate_lookup_survives_a_stub_rate_store(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    """A rates double whose cap is not callable (or raises) must not break /setbase."""
    from types import SimpleNamespace

    real = stub_services.rates
    stub_services.rates = SimpleNamespace(
        cap=5, base=real.base, set_base=real.set_base, save=real.save, set_cap=real.set_cap
    )
    quiet = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))
    assert quiet is not None
    assert "WARNING" not in quiet.text

    def _explode(pair):
        raise RuntimeError("no cap table")

    stub_services.rates = SimpleNamespace(
        cap=_explode, base=_explode, set_base=real.set_base, save=real.save, set_cap=real.set_cap
    )
    broken = dispatcher.dispatch(_update("/setcap UAH/USDT 47.20"))
    assert broken is not None
    assert broken.text.splitlines()[0] == "cap_rate UAH/USDT = 47.20"


# -- /setrate ---------------------------------------------------------------------------
def _edit_result(account_id: str, pair: str, status: str, price: str, **fields) -> SimpleNamespace:
    values = {"account_id": account_id, "pair": pair, "status": status, "price": Decimal(price)}
    values.update({"adv_no": None, "error": None, **fields})
    return SimpleNamespace(**values)


def test_setrate_reprices_through_the_facade_and_lists_every_edit(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(),
        results=(
            _edit_result("Binance#1", "UAH/USDT", "updated", "47.00", adv_no="b1"),
            _edit_result("Binance#1", "UAH/USDC", "updated", "46.75", adv_no="b2"),
            _edit_result("Okx#1", "UAH/USDT", "error", "47.00", error="ApiError: rejected"),
        ),
        unchanged=(
            SimpleNamespace(account_id="Bybit#1", pair="UAH/USDC", price=Decimal("46.99"), adv_no="y2"),
        ),
        problems=("Bybit#1 UAH/USDT: no online buy ad to edit",),
    )

    result = dispatcher.dispatch(_update("/setrate 47.00"))

    assert stub_services.setrate_calls == [{"rate": Decimal("47.00"), "dry_run": False}]
    assert result is not None
    assert result.text.splitlines() == [
        "✅ Rate 47.00 applied",
        "",
        "🏦 Binance#1",
        "   UAH/USDT: 47.00",
        "   UAH/USDC: 46.75",
        "",
        "🏦 Bybit#1",
        "   UAH/USDC: 46.99",
        "",
        "❌ Failed (1)",
        "   Okx#1 UAH/USDT → 47.00: ApiError: rejected",
        "",
        "⚠️ Notes (1)",
        "   Bybit#1 UAH/USDT: no online buy ad to edit",
        "",
        "2 updated · 1 failed · 1 already at rate",
    ]


def test_setrate_dry_run_is_forwarded_and_flagged(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    result = dispatcher.dispatch(_update("/setrate 47.10 --dry"))

    assert stub_services.setrate_calls == [{"rate": Decimal("47.10"), "dry_run": True}]
    assert result is not None
    assert result.text.splitlines() == [
        "🧪 Preview · rate 47.10 · nothing sent",
        "",
        "0 would be updated · 0 failed · 0 already at rate",
    ]


def test_setrate_truncates_a_long_problem_list(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(), results=(), unchanged=(), problems=tuple(f"problem {index}" for index in range(12))
    )

    result = dispatcher.dispatch(_update("/setrate 47"))

    assert result is not None
    assert result.text.splitlines()[-5:-2] == ["   problem 8", "   problem 9", "   ... and 2 more"]


@pytest.mark.parametrize(
    "text",
    ["/setrate", "/setrate abc", "/setrate 0", "/setrate -1", "/setrate 47 48", "/setrate --dry", "/setrate 47 --dry --dry"],
)
def test_setrate_malformed_arguments_show_the_usage(
    stub_services: StubServices, dispatcher: Dispatcher, text: str
) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult("Usage: /setrate <RATE> [--dry]")
    assert stub_services.setrate_calls == []


def test_setrate_facade_fault_is_reported_as_text(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_error = ConfigError("rate must be positive, got 0")

    result = dispatcher.dispatch(_update("/setrate 47.00"))

    assert result == HandlerResult("ConfigError: rate must be positive, got 0")


# -- /getads ----------------------------------------------------------------------------
def _ad(pair: str, side: str, price: str, adv_no: str, status: str = "online") -> OwnAd:
    return OwnAd(
        platform="binance", account_id="Binance#1", adv_no=adv_no, pair=Pair.parse(pair),
        side=side, status=status, price=Decimal(price), min_amount=Decimal("4000"),
        max_amount=Decimal("3670000"), quantity=Decimal("50000"),
    )


@pytest.fixture
def listed(stub_services: StubServices) -> StubServices:
    stub_services.own_ads = (
        OwnAdsResult(
            "Binance#1",
            "binance",
            ads=(
                _ad("UAH/USDT", "buy", "44.75", "t1"),
                _ad("UAH/USDT", "buy", "45.05", "t2"),
                _ad("UAH/USDC", "buy", "44.50", "c1"),
                _ad("UAH/USDT", "sell", "54.50", "s1"),
                _ad("UAH/USDT", "buy", "40.00", "o1", status="offline"),
                _ad("EUR/USDT", "buy", "0.83", "e1"),
                _ad("PLN/USDT", "buy", "3.75", "p1"),
            ),
        ),
        OwnAdsResult("Okx#1", "okx", error="ApiError: HTTP 404"),
    )
    return stub_services


def test_getads_shows_buy_ads_by_account_and_pair_rates_highest_first(
    listed: StubServices, dispatcher: Dispatcher
) -> None:
    result = dispatcher.dispatch(_update("/getads"))

    assert result is not None
    assert result.text.splitlines() == [
        "📊 Buy ads · UAH/USDT, UAH/USDC, PLN/USDT, PLN/USDC",
        "",
        "🏦 Binance#1",
        "   UAH/USDT: 45.05 · 44.75",  # the sell ad (54.50) and the offline one are left out
        "   UAH/USDC: 44.50",
        "   PLN/USDT: 3.75",  # PLN is shown by default too
        "",
        "⚠️ Okx#1: cannot list ads (ApiError: HTTP 404)",
    ]


def test_getads_offline_marks_offline_ads(listed: StubServices, dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/getads uah/usdt --offline"))

    assert result is not None
    lines = result.text.splitlines()
    assert lines[0] == "📊 Buy ads · UAH/USDT · incl. offline"
    assert "   UAH/USDT: 45.05 · 44.75 · 40.00 (off)" in lines
    assert not any("UAH/USDC" in line for line in lines[1:])


def test_getads_details_lists_every_ad(listed: StubServices, dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/getads UAH/USDT --details"))

    assert result is not None
    assert result.text.splitlines()[2:6] == [
        "🏦 Binance#1",
        "   UAH/USDT",
        "      45.05 | 4000–3670000 | left 50000 | adv t2",
        "      44.75 | 4000–3670000 | left 50000 | adv t1",
    ]


def test_getads_all_lists_every_pair_and_says_when_an_account_has_none(
    listed: StubServices, dispatcher: Dispatcher
) -> None:
    listed.own_ads = listed.own_ads + (OwnAdsResult("Bybit#1", "bybit", ads=()),)

    result = dispatcher.dispatch(_update("/getads all"))

    assert result is not None
    lines = result.text.splitlines()
    assert lines[0] == "📊 Buy ads · all pairs"
    assert lines[3:7] == [
        "   UAH/USDT: 45.05 · 44.75", "   UAH/USDC: 44.50", "   PLN/USDT: 3.75", "   EUR/USDT: 0.83",
    ]
    assert lines[-1] == "🏦 Bybit#1: no buy ads"


@pytest.mark.parametrize(
    "text",
    ["/getads UAHUSDT", "/getads UAH/USDT all", "/getads --offline --offline", "/getads --verbose"],
)
def test_getads_malformed_arguments_show_the_usage(dispatcher: Dispatcher, text: str) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult(
        "Usage: /getads [<PAIR>|all] [--offline] [--details]"
    )
