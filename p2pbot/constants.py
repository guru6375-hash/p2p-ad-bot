"""Hardcoded market/business constants.

Values in this module are *policy*, not configuration: the UAH spreads, the Binance
advertiser thresholds and the parser interval are fixed by the product requirement. The
blueprint loader rejects any attempt to override them (see :mod:`p2pbot.blueprint`).
"""

from __future__ import annotations

from decimal import Decimal

from .models import Filters

VERSION = "1.0.0"

PLATFORMS: tuple[str, ...] = ("binance", "okx", "bybit")

#: Price precision per fiat currency. UAH and PLN advertisements are quoted with 2 decimals.
PRICE_TICK: dict[str, Decimal] = {
    "UAH": Decimal("0.01"),
    "PLN": Decimal("0.01"),
}
DEFAULT_PRICE_TICK = Decimal("0.01")

#: HARDCODED minimum difference between the USDT advertisement and the USDC advertisement
#: for UAH, per platform.  Requirement: Binance 0.25 UAH, ByBit/OKX 0.01 UAH.
UAH_SPREAD: dict[str, Decimal] = {
    "binance": Decimal("0.25"),
    "okx": Decimal("0.01"),
    "bybit": Decimal("0.01"),
}

#: HARDCODED competitor-parser cadence for the PLN scenario.
DEFAULT_PARSER_INTERVAL_MINUTES = 25

SIDE_SELL = "sell"
SIDE_BUY = "buy"

#: Binance advertiser filter: merchants only, > 500 orders in the last 30 days,
#: positiveRate > 97 %, monthFinishRate > 94 %.  Comparisons are strict (">").
BINANCE_FILTERS = Filters(
    user_type="merchant",
    min_month_order_count=Decimal("500"),
    min_positive_rate=Decimal("0.97"),
    min_month_finish_rate=Decimal("0.94"),
)

#: OKX advertiser filter: merchants only.
OKX_FILTERS = Filters(user_type="merchant")

FILTERS_BY_PLATFORM: dict[str, Filters] = {
    "binance": BINANCE_FILTERS,
    "okx": OKX_FILTERS,
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 15.0

#: Telegram policy constants.
TELEGRAM_POLL_TIMEOUT_SECONDS = 25
TELEGRAM_MAX_MESSAGE_LENGTH = 3900
RATE_LIMIT_MAX_MESSAGES = 20
RATE_LIMIT_WINDOW_SECONDS = 60
TELEGRAM_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "usage summary"),
    ("help", "usage summary"),
    ("setbase", "set base rate: /setbase UAH/USDT 47.00"),
    ("setcap", "set cap rate: /setcap UAH/USDT 47.10"),
    ("rates", "show base/cap and computed advertisement prices"),
    ("scenarios", "list available scenario blueprints"),
    ("scenario", "activate a scenario: /scenario uah"),
    ("parse", "run the competitor parser once: /parse [PAIR]"),
    ("publish", "create/update advertisements now: /publish [--dry]"),
    ("pause", "deactivate advertisements: /pause [PAIR]"),
    ("resume", "reactivate advertisements: /resume [PAIR]"),
    ("status", "scenario, rates, scheduler and last publish results"),
    ("version", "bot version"),
)

#: Telegram message keys that carry an uploaded file or any other binary payload.
#: A message carrying any of these is refused outright and never downloaded.
ATTACHMENT_KEYS: tuple[str, ...] = (
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
    "dice",
    "game",
    "invoice",
    "successful_payment",
    "passport_data",
    "story",
    "paid_media",
)

#: Credential field names whose values must never appear in logs or chat output.
SECRET_FIELDS: tuple[str, ...] = (
    "API_KEY",
    "SECRET_KEY",
    "PASSPHRASE",
    "SESSION_COOKIE",
    "CSRF_TOKEN",
    "TOKEN",
)

REDACTED = "***"
