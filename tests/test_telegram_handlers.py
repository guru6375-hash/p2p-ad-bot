"""Command router: every command, its arguments, malformed input and error reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from p2pbot import constants
from p2pbot.errors import ConfigError, MissingCapError, TransportError
from p2pbot.models import ComputedAd, Pair
from p2pbot.rates import RateStore
from p2pbot.telegram.api import Message, Update
from p2pbot.telegram.handlers import (
    HELP_TEXT,
    Dispatcher,
    HandlerResult,
    render_results,
    render_snapshot,
)

from conftest import (
    FakeClock,
    StubJobRow,
    StubMarketRow,
    StubPublishResult,
    StubRateRow,
    StubScenarioManager,
    StubServices,
    StubStatusSnapshot,
)

UTC = timezone.utc


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
def dispatcher(stub_services: StubServices, clock: FakeClock) -> Dispatcher:
    return Dispatcher(stub_services, clock=clock)


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
    result = dispatcher.dispatch(_update("/version@p2p_manager_bot"))
    assert result is not None
    assert result.text.startswith("p2pbot ")


def test_updates_without_text_or_message_are_ignored(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(Update(update_id=1, raw={})) is None
    assert dispatcher.dispatch(_update("   ")) is None


def test_commands_without_arguments_still_work(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/rates")) is not None
    assert dispatcher.dispatch(_update("/status")) is not None


def test_dispatcher_exposes_the_registered_commands(dispatcher: Dispatcher) -> None:
    names = set(dispatcher.commands)
    assert set(name for name, _ in constants.TELEGRAM_COMMANDS) <= names


# -- /setbase --------------------------------------------------------------------------
def test_setbase_stores_and_persists_the_rate(
    stub_services: StubServices, clock: FakeClock, tmp_path: Path
) -> None:
    stub_services.rates = RateStore(path=tmp_path / "state.json")
    dispatcher = Dispatcher(stub_services, clock=clock)

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


# -- /parse ----------------------------------------------------------------------------
def test_parse_reports_each_venue(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    from p2pbot.market import MarketFetchResult

    stub_services.parser_results = (
        MarketFetchResult(platform="binance", pair=Pair.parse("PLN/USDT"), fetched=12, kept=3, middle=Decimal("4.31")),
        MarketFetchResult(platform="okx", pair=Pair.parse("PLN/USDT"), error="TransportError: reset"),
    )
    result = dispatcher.dispatch(_update("/parse"))
    assert result is not None
    assert stub_services.parser_calls == [{"pairs": None}]
    assert "binance PLN/USDT fetched=12 kept=3 middle=4.31" in result.text
    assert "okx PLN/USDT fetched=0 kept=0 middle=- error=TransportError: reset" in result.text


def test_parse_with_a_pair_filter(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    dispatcher.dispatch(_update("/parse pln/usdt"))
    assert stub_services.parser_calls == [{"pairs": ["PLN/USDT"]}]


def test_parse_with_nothing_to_fetch(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/parse")) == HandlerResult("parse: nothing to fetch")


def test_parse_usage_and_invalid_pair(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/parse A B")) == HandlerResult("Usage: /parse [<PAIR>]")
    result = dispatcher.dispatch(_update("/parse nope"))
    assert result is not None
    assert result.text.startswith("ConfigError: invalid pair 'nope'")


def test_parse_facade_fault_is_reported(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    stub_services.parser_error = TransportError("DNS failure")
    result = dispatcher.dispatch(_update("/parse"))
    assert result is not None
    assert result.text == "TransportError: DNS failure"


# -- /publish --------------------------------------------------------------------------
def test_publish_passes_dry_run_and_renders_results(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    stub_services.publish_results = (
        StubPublishResult(
            account_id="Binance#1",
            platform="binance",
            pair="UAH/USDT",
            status="created",
            price=Decimal("47.00"),
            adv_no="2048",
        ),
        StubPublishResult(
            account_id="Okx#1",
            platform="okx",
            pair="UAH/USDT",
            status="error",
            error="ApiError: bad cookie",
        ),
    )
    result = dispatcher.dispatch(_update("/publish"))
    assert stub_services.publish_calls == [{"dry_run": False}]
    assert result is not None
    assert result.text.splitlines()[0] == "publish:"
    assert "  created Binance#1 UAH/USDT 47.00 adv 2048" in result.text
    assert "  error Okx#1 UAH/USDT - error: ApiError: bad cookie" in result.text


def test_publish_dry_run_is_flagged(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    stub_services.publish_results = (
        StubPublishResult(
            account_id="Binance#1",
            platform="binance",
            pair="UAH/USDT",
            status="dry_run",
            price=Decimal("47.00"),
            dry_run=True,
        ),
    )
    result = dispatcher.dispatch(_update("/publish --dry"))
    assert stub_services.publish_calls == [{"dry_run": True}]
    assert result is not None
    assert result.text.splitlines()[0] == "publish (dry run):"
    assert "[dry-run]" in result.text


def test_publish_with_no_results(dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/publish"))
    assert result == HandlerResult("publish:\nno results")


@pytest.mark.parametrize("text", ["/publish --wet", "/publish --dry extra", "/publish now"])
def test_publish_usage(dispatcher: Dispatcher, text: str) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult("Usage: /publish [--dry]")


def test_publish_reports_a_facade_fault_as_the_engine_error(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    stub_services.publish_error = MissingCapError("no cap_rate stored for UAH/USDT")
    result = dispatcher.dispatch(_update("/publish"))
    assert result is not None
    assert result.text == "MissingCapError: no cap_rate stored for UAH/USDT"


# -- /pause and /resume ----------------------------------------------------------------
def test_pause_and_resume_call_the_facade(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    stub_services.active_results = (
        StubPublishResult(
            account_id="Binance#1", platform="binance", pair="UAH/USDT", status="updated", price=Decimal("47.00")
        ),
    )
    paused = dispatcher.dispatch(_update("/pause"))
    resumed = dispatcher.dispatch(_update("/resume"))
    assert stub_services.active_calls == [
        {"active": False, "pairs": None},
        {"active": True, "pairs": None},
    ]
    assert paused is not None and paused.text.startswith("pause all pairs:")
    assert resumed is not None and resumed.text.startswith("resume all pairs:")
    assert "updated Binance#1 UAH/USDT 47.00" in paused.text


def test_pause_and_resume_scope_to_one_pair(dispatcher: Dispatcher, stub_services: StubServices) -> None:
    dispatcher.dispatch(_update("/pause uah/usdc"))
    dispatcher.dispatch(_update("/resume UAH/USDC"))
    assert stub_services.active_calls == [
        {"active": False, "pairs": ["UAH/USDC"]},
        {"active": True, "pairs": ["UAH/USDC"]},
    ]


@pytest.mark.parametrize(
    ("text", "usage"),
    [
        ("/pause a b", "Usage: /pause [<PAIR>]"),
        ("/resume a b", "Usage: /resume [<PAIR>]"),
    ],
)
def test_pause_resume_usage(dispatcher: Dispatcher, text: str, usage: str) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult(usage)


def test_pause_with_an_invalid_pair_is_reported(dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/pause nope"))
    assert result is not None
    assert result.text.startswith("ConfigError: invalid pair")


def test_pause_without_results(dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/pause"))
    assert result == HandlerResult("pause all pairs:\nno results")


# -- /status ---------------------------------------------------------------------------
def test_status_renders_every_section(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stub_services.clock = lambda: now
    dispatcher = Dispatcher(stub_services, clock=lambda: now)
    stub_services.snapshot_override = StubStatusSnapshot(
        version="1.0.0",
        scenario="pln",
        fiat="PLN",
        strategy="market_middle",
        rates=(StubRateRow(pair="PLN/USDT", base=None, cap=Decimal("4.50")),),
        prices=(
            ComputedAd(
                pair=Pair.parse("PLN/USDT"),
                platform="binance",
                price=Decimal("4.50"),
                source="market_middle",
                cap=Decimal("4.50"),
                clamped=True,
            ),
        ),
        market=(
            StubMarketRow(
                platform="binance",
                pair="PLN/USDT",
                middle=Decimal("4.50"),
                filtered=4,
                fetched_at=now - timedelta(minutes=10),
            ),
            StubMarketRow(platform="okx", pair="PLN/USDT", middle=None, filtered=0, fetched_at=None),
        ),
        jobs=(StubJobRow(name="parser", next_run_at=now + timedelta(minutes=15), last_error=None),),
        last_publish=(
            StubPublishResult(
                account_id="Binance#1", platform="binance", pair="PLN/USDT", status="updated", price=Decimal("4.50")
            ),
        ),
    )
    result = dispatcher.dispatch(_update("/status"))
    assert result is not None
    lines = result.text
    assert "version: 1.0.0" in lines
    assert "scenario: pln fiat: PLN strategy: market_middle" in lines
    assert "PLN/USDT base - cap 4.50" in lines
    assert "PLN/USDT binance 4.50 market_middle [clamped]" in lines
    assert "binance PLN/USDT middle 4.50 filtered 4 fetched 10m0s ago" in lines
    assert "okx PLN/USDT middle - filtered 0 fetched never" in lines
    assert "parser next_run 2026-01-01T12:15:00+00:00 (in 15m0s)" in lines
    assert "updated Binance#1 PLN/USDT 4.50" in lines


def test_status_marks_a_stale_snapshot(stub_services: StubServices) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    snapshot = StubStatusSnapshot(
        version="1.0.0",
        scenario="pln",
        fiat="PLN",
        strategy="market_middle",
        market=(
            StubMarketRow(
                platform="binance",
                pair="PLN/USDT",
                middle=Decimal("4.31"),
                filtered=2,
                fetched_at=now - timedelta(minutes=40),
            ),
        ),
        jobs=(StubJobRow(name="parser", next_run_at=None, last_error="ValueError: venue down"),),
    )
    text = render_snapshot(snapshot, now=now)
    assert "40m0s ago [stale]" in text
    assert "parser next_run never last_error ValueError: venue down" in text


def test_status_survives_an_empty_snapshot() -> None:
    text = render_snapshot(
        StubStatusSnapshot(version="1.0.0", scenario=None, fiat=None, strategy=None),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert "scenario: none fiat: - strategy: -" in text
    assert text.count("  none") >= 5


def test_render_results_of_nothing() -> None:
    assert render_results(()) == "no results"
    assert render_results(None) == "no results"


# -- /version --------------------------------------------------------------------------
def test_version_reports_uptime(stub_services: StubServices, clock: FakeClock) -> None:
    dispatcher = Dispatcher(stub_services, clock=clock)
    clock.advance(minutes=2, seconds=3)
    result = dispatcher.dispatch(_update("/version"))
    assert result == HandlerResult("p2pbot 1.0.0, uptime 2m3s")


def test_version_uptime_formats_days_and_hours(stub_services: StubServices, clock: FakeClock) -> None:
    dispatcher = Dispatcher(stub_services, clock=clock)
    clock.advance(days=1, hours=2)
    result = dispatcher.dispatch(_update("/version"))
    assert result is not None
    assert result.text.endswith("uptime 1d2h")


# -- duration rendering / problem reporting --------------------------------------------
def test_version_uptime_of_hours_and_minutes(stub_services: StubServices, clock: FakeClock) -> None:
    dispatcher = Dispatcher(stub_services, clock=clock)
    clock.advance(hours=1, minutes=30)
    result = dispatcher.dispatch(_update("/version"))
    assert result == HandlerResult("p2pbot 1.0.0, uptime 1h30m")


def test_render_results_appends_a_skipped_line() -> None:
    result = StubPublishResult(
        account_id="Binance#1", platform="binance", pair="UAH/USDT", status="created", price=Decimal("47.00")
    )
    text = render_results([result], problems=("UAH/USDC okx: MissingMarketDataError: nothing",))
    assert text.splitlines()[0] == "  created Binance#1 UAH/USDT 47.00"
    assert text.splitlines()[-1] == "skipped 1: UAH/USDC okx: MissingMarketDataError: nothing"
    assert render_results([], problems=["only-reason"]) == "nothing to push: only-reason"
    assert render_results([], problems=5) == "no results"
    assert render_results([], problems="a single problem") == "nothing to push: a single problem"


def test_status_lists_engine_problems_with_a_cap() -> None:
    problems = tuple(f"UAH/USDC okx: MissingMarketDataError: no data {index}" for index in range(12))
    snapshot = StubStatusSnapshot(
        version="1.0.0", scenario="uah", fiat="UAH", strategy="fixed_spread", engine_problems=problems
    )
    text = render_snapshot(snapshot, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert "skipped 12:" in text
    assert "UAH/USDC okx: MissingMarketDataError: no data 0" in text
    assert "UAH/USDC okx: MissingMarketDataError: no data 9" in text
    assert "... and 2 more" in text


def test_status_without_problems_has_no_skipped_block() -> None:
    text = render_snapshot(
        StubStatusSnapshot(version="1.0.0", scenario="uah", fiat="UAH", strategy="fixed_spread"),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert "skipped" not in text


def test_setbase_reports_a_successful_immediate_refresh(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.publish_results = (
        StubPublishResult(
            account_id="Binance#1", platform="binance", pair="UAH/USDT", status="updated", price=Decimal("47.00")
        ),
        StubPublishResult(
            account_id="Okx#1",
            platform="okx",
            pair="UAH/USDT",
            status="error",
            error="ApiError: bad session",
        ),
    )
    stub_services.last_problems = ("UAH/USDC okx: MissingMarketDataError: no data",)

    result = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))

    assert result is not None
    lines = result.text.splitlines()
    assert lines[0] == "base_rate UAH/USDT = 47.00"
    assert lines[-3] == "publish: 2 ads (1 error)"
    assert lines[-2] == "  error Okx#1 UAH/USDT - error: ApiError: bad session"
    assert lines[-1] == "skipped 1: UAH/USDC okx: MissingMarketDataError: no data"
    assert stub_services.publish_calls == [{"dry_run": False}]


def test_setbase_with_nothing_to_push_and_a_reason(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.last_problems = "UAH/USDC binance: MissingCapError: no cap"
    result = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))
    assert result is not None
    assert result.text.splitlines()[-1] == "nothing to push: UAH/USDC binance: MissingCapError: no cap"


def test_setcap_with_nothing_to_push(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    result = dispatcher.dispatch(_update("/setcap UAH/USDT 47.20"))
    assert result is not None
    assert result.text.splitlines()[-1] == "publish: 0 ads"


def test_a_failed_refresh_never_invalidates_the_new_rate(
    stub_services: StubServices, dispatcher: Dispatcher, caplog
) -> None:
    stub_services.publish_error = MissingCapError("no cap_rate stored for UAH/USDC")
    with caplog.at_level("WARNING"):
        result = dispatcher.dispatch(_update("/setbase UAH/USDT 47.00"))
    assert result is not None
    assert result.text.splitlines()[-1] == (
        "ads not updated: MissingCapError: no cap_rate stored for UAH/USDC"
    )
    assert stub_services.rates.base("UAH/USDT") == Decimal("47.00")
    assert "price refresh after a rate change failed" in caplog.text


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
