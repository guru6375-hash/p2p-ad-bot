"""Hardcoded policy constants (SPEC section 5) — the values the product must never bend."""

from __future__ import annotations

from decimal import Decimal

from p2pbot import constants

#: The attachment keys listed in SPEC section 11.1; the bot must reject all of them.
SPEC_ATTACHMENT_KEYS = (
    "document",
    "photo",
    "video",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "animation",
    "new_chat_photo",
    "contact",
    "location",
    "venue",
    "poll",
    "invoice",
    "successful_payment",
    "passport_data",
)

#: The commands listed in SPEC section 11.2.
SPEC_COMMANDS = (
    "start",
    "help",
    "setbase",
    "setcap",
    "rates",
    "scenarios",
    "scenario",
    "parse",
    "publish",
    "pause",
    "resume",
    "status",
    "version",
)


def test_platforms_and_side_constants() -> None:
    assert constants.PLATFORMS == ("binance", "okx", "bybit")
    assert constants.SIDE_SELL == "sell"
    assert constants.SIDE_BUY == "buy"
    assert constants.DEFAULT_TIMEOUT_SECONDS == 15.0
    assert constants.VERSION.count(".") == 2


def test_price_ticks_are_decimal_and_cover_both_fiats() -> None:
    assert set(constants.PRICE_TICK) == {"UAH", "PLN"}
    assert all(isinstance(tick, Decimal) for tick in constants.PRICE_TICK.values())
    assert constants.PRICE_TICK["UAH"] == Decimal("0.01")
    assert constants.PRICE_TICK["PLN"] == Decimal("0.01")
    assert constants.DEFAULT_PRICE_TICK == Decimal("0.01")


def test_uah_spread_is_the_hardcoded_per_platform_difference() -> None:
    """Binance 0.25 UAH, OKX/ByBit 0.01 UAH (verified reference behaviour)."""
    assert constants.UAH_SPREAD == {
        "binance": Decimal("0.25"),
        "okx": Decimal("0.01"),
        "bybit": Decimal("0.01"),
    }
    assert all(type(value) is Decimal for value in constants.UAH_SPREAD.values())


def test_parser_interval_default_is_25_minutes() -> None:
    assert constants.DEFAULT_PARSER_INTERVAL_MINUTES == 25


def test_binance_filters_are_merchant_with_strict_thresholds() -> None:
    filters = constants.BINANCE_FILTERS
    assert filters.user_type == "merchant"
    assert filters.min_month_order_count == Decimal("500")
    assert filters.min_positive_rate == Decimal("0.97")
    assert filters.min_month_finish_rate == Decimal("0.94")
    assert all(
        type(value) is Decimal
        for value in (
            filters.min_month_order_count,
            filters.min_positive_rate,
            filters.min_month_finish_rate,
        )
    )
    payload = filters.to_dict()
    assert payload["min_month_order_count"] == "500"
    assert payload["min_positive_rate"] == "0.97"
    assert payload["min_month_finish_rate"] == "0.94"


def test_okx_filters_are_merchant_only() -> None:
    filters = constants.OKX_FILTERS
    assert filters.user_type == "merchant"
    assert filters.min_month_order_count is None
    assert filters.min_positive_rate is None
    assert filters.min_month_finish_rate is None


def test_filters_by_platform_registry() -> None:
    assert constants.FILTERS_BY_PLATFORM["binance"] is constants.BINANCE_FILTERS
    assert constants.FILTERS_BY_PLATFORM["okx"] is constants.OKX_FILTERS
    assert "bybit" not in constants.FILTERS_BY_PLATFORM


def test_telegram_policy_constants() -> None:
    assert constants.TELEGRAM_POLL_TIMEOUT_SECONDS == 25
    assert constants.TELEGRAM_MAX_MESSAGE_LENGTH == 3900
    assert constants.RATE_LIMIT_MAX_MESSAGES == 20
    assert constants.RATE_LIMIT_WINDOW_SECONDS == 60
    assert constants.REDACTED == "***"


def test_every_spec_attachment_key_is_rejected_by_policy() -> None:
    missing = [key for key in SPEC_ATTACHMENT_KEYS if key not in constants.ATTACHMENT_KEYS]
    assert missing == [], f"ATTACHMENT_KEYS misses SPEC keys: {missing}"
    assert len(set(constants.ATTACHMENT_KEYS)) == len(constants.ATTACHMENT_KEYS)
    assert all(isinstance(key, str) and key for key in constants.ATTACHMENT_KEYS)


def test_secret_fields_cover_every_credential_name() -> None:
    for field in ("API_KEY", "SECRET_KEY", "PASSPHRASE", "SESSION_COOKIE", "CSRF_TOKEN", "TOKEN"):
        assert field in constants.SECRET_FIELDS


def test_telegram_command_table_matches_the_spec() -> None:
    names = tuple(name for name, _ in constants.TELEGRAM_COMMANDS)
    assert sorted(names) == sorted(SPEC_COMMANDS)
    assert all(isinstance(description, str) and description for _, description in constants.TELEGRAM_COMMANDS)
