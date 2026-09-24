"""Exchange adapter registry.

The single place that maps a platform name to its adapter class, so nothing else in the
package has to know the concrete classes. Adapters receive an injected
:class:`~p2pbot.exchanges.base.Transport` (defaulting to the stdlib
:class:`~p2pbot.exchanges.base.UrllibTransport`) and an injectable clock, which keeps the
whole suite offline and deterministic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .base import ExchangeAdapter, HttpRequest, HttpResponse, Transport, UrllibTransport
from .binance import BinanceAdapter
from .bybit import BybitAdapter
from .okx import OkxAdapter

__all__ = [
    "ADAPTERS",
    "build_adapters",
    "ExchangeAdapter",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "UrllibTransport",
]

#: Platform name -> adapter class. Keys are lowercase and match ``constants.PLATFORMS``.
ADAPTERS: dict[str, type[ExchangeAdapter]] = {
    "binance": BinanceAdapter,
    "okx": OkxAdapter,
    "bybit": BybitAdapter,
}


def build_adapters(
    transport: Transport | None = None,
    *,
    now: Callable[[], datetime] | None = None,
) -> dict[str, ExchangeAdapter]:
    """Instantiate one adapter per platform, sharing a single transport."""
    resolved = transport if transport is not None else UrllibTransport()
    return {name: factory(resolved, now=now) for name, factory in ADAPTERS.items()}
