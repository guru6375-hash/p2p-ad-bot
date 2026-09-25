"""Command router: /help, /getads, the button-driven /setrate and error reporting."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from p2pbot import constants
from p2pbot.errors import ConfigError
from p2pbot.models import OwnAd, OwnAdsResult, Pair
from p2pbot.telegram.api import CallbackQuery, Message, Update
from p2pbot.telegram.handlers import (
    HELP_TEXT,
    PENDING_RATE_SECONDS,
    Dispatcher,
    HandlerResult,
)

from conftest import StubServices

HINT = "Unknown command. Send /help for the command list."


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


def _press(data: str, *, chat_id: int = 4242) -> Update:
    """The owner pressing an inline button under a bot message in ``chat_id``."""
    return Update(
        update_id=2,
        callback=CallbackQuery(
            id="cb-1",
            from_id=4242,
            data=data,
            message=Message(message_id=7, chat_id=chat_id, chat_type="private", from_id=1),
        ),
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def dispatcher(stub_services: StubServices, clock: _Clock) -> Dispatcher:
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
    assert dispatcher.dispatch(_update("/nope")) == HandlerResult(HINT)
    assert dispatcher.dispatch(_update("hello there")) == HandlerResult(HINT)
    assert dispatcher.dispatch(_update("47.00")) == HandlerResult(HINT)  # no market picked


def test_command_with_a_bot_suffix_is_routed(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_update("/help@p2p_manager_bot")) == HandlerResult(HELP_TEXT)


def test_updates_without_text_or_message_are_ignored(dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(Update(update_id=1, raw={})) is None
    assert dispatcher.dispatch(_update("   ")) is None


def test_dispatcher_exposes_only_the_kept_commands(dispatcher: Dispatcher) -> None:
    assert set(dispatcher.commands) == {name for name, _ in constants.TELEGRAM_COMMANDS}
    for removed in ("setbase", "setcap", "rates", "scenarios", "scenario", "parse", "status"):
        assert dispatcher.dispatch(_update(f"/{removed}")) == HandlerResult(HINT)


# -- /setrate: buttons, then the rate --------------------------------------------------
def _edit_result(account_id: str, pair: str, status: str, price: str, **fields) -> SimpleNamespace:
    values = {"account_id": account_id, "pair": pair, "status": status, "price": Decimal(price)}
    values.update({"adv_no": None, "error": None, **fields})
    return SimpleNamespace(**values)


def test_setrate_offers_the_uah_and_pln_buttons(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    result = dispatcher.dispatch(_update("/setrate"))

    assert result == HandlerResult(
        "💱 Set rate · pick a market",
        buttons=[[("🇺🇦 UAH", "setrate:uah"), ("🇵🇱 PLN", "setrate:pln")]],
    )
    assert stub_services.setrate_calls == []


def test_pressing_uah_asks_for_the_rate_and_the_next_message_sets_it(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(),
        results=(
            _edit_result("Binance#1", "UAH/USDT", "updated", "47.00", adv_no="b1"),
            _edit_result("Binance#1", "UAH/USDC", "updated", "46.75", adv_no="b2"),
            _edit_result("Bybit#1", "UAH/USDT", "error", "47.00", error="ApiError: rejected"),
        ),
        unchanged=(
            SimpleNamespace(account_id="Bybit#1", pair="UAH/USDC", price=Decimal("46.99"), adv_no="y2"),
        ),
        problems=("Bybit#1 UAH/USDT: no online buy ad to edit",),
    )

    prompt = dispatcher.dispatch(_press("setrate:uah"))

    assert prompt is not None
    assert prompt.edit == "💱 Set rate · 🇺🇦 UAH"
    assert prompt.text.splitlines()[0] == "🇺🇦 Send the UAH rate, e.g. 43.50"
    assert "(STEP Binance 0.25 · Bybit 0.01)" in prompt.text
    assert dispatcher.pending(4242) == "uah"
    assert stub_services.setrate_calls == []

    result = dispatcher.dispatch(_update("47.00"))

    assert stub_services.setrate_calls == [{"market": "uah", "rate": Decimal("47.00"), "dry_run": False}]
    assert dispatcher.pending(4242) is None
    assert result is not None
    assert result.text.splitlines() == [
        "✅ UAH rate 47.00 applied",
        "",
        "🏦 Binance#1",
        "   UAH/USDT: 47.00",
        "   UAH/USDC: 46.75",
        "",
        "🏦 Bybit#1",
        "   UAH/USDC: 46.99",
        "",
        "❌ Failed (1)",
        "   Bybit#1 UAH/USDT → 47.00: ApiError: rejected",
        "",
        "⚠️ Notes (1)",
        "   Bybit#1 UAH/USDT: no online buy ad to edit",
        "",
        "2 updated · 1 failed · 1 already at rate",
    ]
    # the prompt is used up: another number is not a rate any more
    assert dispatcher.dispatch(_update("48")) == HandlerResult(HINT)


def test_pressing_pln_sets_the_pln_rate(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(),
        results=(
            _edit_result("Binance#2", "PLN/USDT", "updated", "3.85"),
            _edit_result("Binance#2", "PLN/USDC", "updated", "3.85"),
        ),
        unchanged=(),
        problems=(),
    )

    prompt = dispatcher.dispatch(_press("setrate:pln"))
    assert prompt is not None and prompt.edit == "💱 Set rate · 🇵🇱 PLN"
    assert "PLN/USDT and PLN/USDC" in prompt.text

    result = dispatcher.dispatch(_update("3,85"))  # a decimal comma is accepted

    assert stub_services.setrate_calls == [{"market": "pln", "rate": Decimal("3.85"), "dry_run": False}]
    assert result is not None
    assert result.text.splitlines() == [
        "✅ PLN rate 3.85 applied",
        "",
        "🏦 Binance#2",
        "   PLN/USDT: 3.85",
        "   PLN/USDC: 3.85",
        "",
        "2 updated · 0 failed · 0 already at rate",
    ]


def test_skipped_ads_are_listed_and_not_counted_as_failures(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(),
        results=(_edit_result("Bybit#1", "PLN/USDT", "updated", "3.21"),),
        unchanged=(),
        problems=(),
        skipped=(SimpleNamespace(account_id="Bybit#1", pair="PLN/USDT", adv_no="y2", price=Decimal("3.21")),),
    )

    result = dispatcher.dispatch(_update("/setrate pln 3.21"))

    assert result is not None
    assert result.text.splitlines()[-6:] == [
        "   PLN/USDT: 3.21",
        "",
        "⏭ Skipped (1) · another ad already has this rate",
        "   Bybit#1 PLN/USDT adv y2",
        "",
        "1 updated · 0 failed · 0 already at rate · 1 skipped",
    ]


def test_a_dry_rate_is_forwarded_and_flagged(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    dispatcher.dispatch(_press("setrate:uah"))

    result = dispatcher.dispatch(_update("47.10 --dry"))

    assert stub_services.setrate_calls == [{"market": "uah", "rate": Decimal("47.10"), "dry_run": True}]
    assert result is not None
    assert result.text.splitlines() == [
        "🧪 Preview · UAH rate 47.10 · nothing sent",
        "",
        "0 would be updated · 0 failed · 0 already at rate",
    ]


@pytest.mark.parametrize("text", ["abc", "0", "-1", "47 48", "--dry", "47 --dry --dry"])
def test_a_bad_rate_is_refused_and_the_prompt_keeps_waiting(
    stub_services: StubServices, dispatcher: Dispatcher, text: str
) -> None:
    dispatcher.dispatch(_press("setrate:pln"))

    result = dispatcher.dispatch(_update(text))

    assert result is not None and result.text.startswith("❓ Not a rate:")
    assert "3.85" in result.text
    assert dispatcher.pending(4242) == "pln"
    assert stub_services.setrate_calls == []


def test_a_prompt_expires(stub_services: StubServices, dispatcher: Dispatcher, clock: _Clock) -> None:
    dispatcher.dispatch(_press("setrate:uah"))
    clock.now += PENDING_RATE_SECONDS + 1

    assert dispatcher.pending(4242) is None
    assert dispatcher.dispatch(_update("47")) == HandlerResult(
        "⌛ That rate prompt expired. Send /setrate again."
    )
    assert dispatcher.dispatch(_update("47")) == HandlerResult(HINT)
    assert stub_services.setrate_calls == []


def test_cancel_and_any_other_command_drop_the_prompt(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    dispatcher.dispatch(_press("setrate:uah"))
    assert dispatcher.dispatch(_update("/cancel")) == HandlerResult("Cancelled: UAH rate not changed.")
    assert dispatcher.dispatch(_update("/cancel")) == HandlerResult("Nothing is waiting for a rate.")

    dispatcher.dispatch(_press("setrate:pln"))
    dispatcher.dispatch(_update("/help"))
    assert dispatcher.dispatch(_update("3.85")) == HandlerResult(HINT)
    assert stub_services.setrate_calls == []


def test_a_prompt_belongs_to_its_chat(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    dispatcher.dispatch(_press("setrate:uah", chat_id=1))

    assert dispatcher.dispatch(_update("47")) == HandlerResult(HINT)  # chat 4242
    assert dispatcher.pending(1) == "uah"


def test_an_unknown_button_changes_nothing(stub_services: StubServices, dispatcher: Dispatcher) -> None:
    assert dispatcher.dispatch(_press("setrate:okx")) == HandlerResult(
        "This button is no longer used. Send /setrate."
    )
    assert dispatcher.dispatch(_press("other")) is not None
    assert dispatcher.pending(4242) is None


@pytest.mark.parametrize(
    ("text", "market", "rate", "dry_run"),
    [
        ("/setrate uah 47", "uah", "47", False),
        ("/setrate PLN 3.85 --dry", "pln", "3.85", True),
    ],
)
def test_setrate_with_a_market_and_rate_skips_the_buttons(
    stub_services: StubServices, dispatcher: Dispatcher, text: str, market: str, rate: str, dry_run: bool
) -> None:
    result = dispatcher.dispatch(_update(text))

    assert stub_services.setrate_calls == [{"market": market, "rate": Decimal(rate), "dry_run": dry_run}]
    assert result is not None and result.buttons is None


@pytest.mark.parametrize("text", ["/setrate 47", "/setrate okx 47", "/setrate uah", "/setrate uah abc"])
def test_setrate_malformed_arguments_show_the_usage(
    stub_services: StubServices, dispatcher: Dispatcher, text: str
) -> None:
    assert dispatcher.dispatch(_update(text)) == HandlerResult(
        "Usage: /setrate, or /setrate <uah|pln> <RATE> [--dry]"
    )
    assert stub_services.setrate_calls == []


def test_setrate_truncates_a_long_problem_list(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_report = SimpleNamespace(
        queued=(), results=(), unchanged=(), problems=tuple(f"problem {index}" for index in range(12))
    )

    result = dispatcher.dispatch(_update("/setrate uah 47"))

    assert result is not None
    assert result.text.splitlines()[-5:-2] == ["   problem 8", "   problem 9", "   ... and 2 more"]


def test_setrate_facade_fault_is_reported_as_text(
    stub_services: StubServices, dispatcher: Dispatcher
) -> None:
    stub_services.setrate_error = ConfigError("rate must be positive, got 0")
    dispatcher.dispatch(_press("setrate:uah"))

    result = dispatcher.dispatch(_update("47.00"))

    assert result == HandlerResult("ConfigError: rate must be positive, got 0")
    assert dispatcher.pending(4242) is None


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
