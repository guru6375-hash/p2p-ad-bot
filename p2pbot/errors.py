"""Exception hierarchy for the P2P ad manager.

Layering rule: ``ConfigError`` and its subclasses are *user/config* faults and subclass
``ValueError`` so callers can treat bad input uniformly. ``EngineError`` subclasses are
runtime state faults. ``ExchangeError`` subclasses describe venue interaction failures.

Nothing in this module performs I/O.
"""

from __future__ import annotations

from typing import Any


class BotError(Exception):
    """Base class for every error raised by :mod:`p2pbot`."""


class ConfigError(BotError, ValueError):
    """Invalid configuration or user-supplied value."""


class BlueprintError(ConfigError):
    """Scenario blueprint JSON is malformed or violates the schema."""


class RateError(ConfigError):
    """A base/cap rate value is invalid."""


class CronError(ConfigError):
    """A cron expression is malformed or can never fire."""


class SecurityError(BotError):
    """An interaction violated the access policy."""


class TelegramError(BotError):
    """The Telegram Bot API call failed."""


class EngineError(BotError):
    """Base class for price-computation faults."""


class MissingRateError(EngineError):
    """No ``base_rate`` is stored for the pair required by the scenario."""


class MissingCapError(EngineError):
    """No ``cap_rate`` is stored for a pair that would be advertised.

    Publishing without a ceiling is forbidden by design: the cap is a hard rule, so an
    absent cap must fail loudly instead of silently dropping the guard.
    """


class MissingMarketDataError(EngineError):
    """A ``market_middle``/``copy:`` source has no data available for the pair."""


class PriceError(EngineError):
    """The computed advertisement price is not publishable (e.g. <= 0)."""


class ExchangeError(BotError):
    """Base class for venue interaction failures."""


class TransportError(ExchangeError):
    """Connection-level failure: DNS, TLS, timeout, connection reset."""


class ApiError(ExchangeError):
    """The venue answered, but reported its own failure code/message."""

    def __init__(self, message: str, payload: Any = None, status: int | None = None) -> None:
        super().__init__(message)
        self.payload = payload
        self.status = status
