"""Offline doubles for the exchange-adapter test slice (no socket is ever opened).

``tests/conftest.py`` is shared by every test module and is not editable from this slice, so
the doubles the adapter suite needs live here: a recording :class:`FakeTransport`
(replays scripted :class:`~p2pbot.exchanges.base.HttpResponse` objects or exceptions), a
fake ``urllib`` opener that lets :class:`~p2pbot.exchanges.base.UrllibTransport` be driven
without a network, and deterministic account/spec builders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
import json

from p2pbot.constants import DEFAULT_TIMEOUT_SECONDS
from p2pbot.exchanges.base import HttpRequest, HttpResponse
from p2pbot.models import Account, AccountRef, AdSpec, Pair

__all__ = [
    "FIXED_NOW",
    "FIXED_NOW_ISO",
    "FIXED_NOW_MS",
    "BINANCE_CREDENTIALS",
    "BYBIT_CREDENTIALS",
    "OKX_CREDENTIALS",
    "FixedClock",
    "FakeOpener",
    "FakeOpenerResponse",
    "FakeTransport",
    "json_response",
    "make_account",
    "make_spec",
    "text_response",
]

#: Deterministic instant every signature assertion is computed against (ms precision).
FIXED_NOW = datetime(2026, 9, 24, 12, 0, 0, 123_000, tzinfo=timezone.utc)
#: ``FIXED_NOW`` as integer epoch milliseconds, computed once (2026-09-24T12:00:00.123Z).
FIXED_NOW_MS = 1790251200123
#: ``FIXED_NOW`` as OKX's ``OK-ACCESS-TIMESTAMP``.
FIXED_NOW_ISO = "2026-09-24T12:00:00.123Z"

BINANCE_CREDENTIALS: dict[str, str] = {"API_KEY": "binance-key", "SECRET_KEY": "binance-secret"}
OKX_CREDENTIALS: dict[str, str] = {
    "API_KEY": "okx-key",
    "SECRET_KEY": "okx-secret",
    "PASSPHRASE": "okx-passphrase",
}
BYBIT_CREDENTIALS: dict[str, str] = {"API_KEY": "bybit-key", "SECRET_KEY": "bybit-secret"}


def make_account(
    platform: str = "binance",
    index: int = 1,
    credentials: Mapping[str, str] | None = None,
) -> Account:
    """An :class:`~p2pbot.models.Account` with the credentials of one venue."""
    return Account(ref=AccountRef(platform, index), credentials=dict(credentials or {}))


def make_spec(
    *,
    pair: Pair | str = "UAH/USDT",
    price: str | Decimal = "47.00",
    min_amount: str | Decimal = "1000.00",
    max_amount: str | Decimal = "47000.00",
    payment_methods: Iterable[str] = (),
    active: bool = True,
    side: str = "sell",
    quantity: str | Decimal | None = None,
    payment_ids: Iterable[str] = (),
    price_floating_ratio: str | Decimal | None = None,
) -> AdSpec:
    """An :class:`~p2pbot.models.AdSpec` built from decimal *strings* (money stays exact)."""
    resolved_pair = pair if isinstance(pair, Pair) else Pair.parse(pair)
    return AdSpec(
        pair=resolved_pair,
        price=Decimal(price),
        min_amount=Decimal(min_amount),
        max_amount=Decimal(max_amount),
        payment_methods=tuple(payment_methods),
        active=active,
        side=side,
        quantity=None if quantity is None else Decimal(quantity),
        payment_ids=tuple(payment_ids),
        price_floating_ratio=None if price_floating_ratio is None else Decimal(price_floating_ratio),
    )


class FixedClock:
    """Callable clock: every call returns the same instant (stable signature timestamps)."""

    def __init__(self, moment: datetime = FIXED_NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def json_response(
    payload: Any, status: int = 200, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    """A JSON ``HttpResponse`` (``200`` unless told otherwise)."""
    return HttpResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=dict(headers or {}),
    )


def text_response(
    text: str, status: int = 200, headers: Mapping[str, str] | None = None
) -> HttpResponse:
    """A non-JSON body, e.g. an Akamai/WAF HTML error page."""
    return HttpResponse(status=status, body=text.encode("utf-8"), headers=dict(headers or {}))


class FakeTransport:
    """Records every :class:`HttpRequest` and replays scripted responses or exceptions."""

    def __init__(self, *scripted: HttpResponse | BaseException) -> None:
        self.scripted: list[HttpResponse | BaseException] = list(scripted)
        self.requests: list[HttpRequest] = []
        self.timeouts: list[float] = []

    # -- scripting -----------------------------------------------------------------
    def push(self, *items: HttpResponse | BaseException) -> "FakeTransport":
        self.scripted.extend(items)
        return self

    # -- Transport protocol --------------------------------------------------------
    def send(self, request: HttpRequest, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.scripted:
            raise AssertionError(
                f"FakeTransport got an unexpected request: {request.method} {request.url}"
            )
        item = self.scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    # -- assertions helpers --------------------------------------------------------
    @property
    def last(self) -> HttpRequest:
        assert self.requests, "no request was sent"
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [request.url for request in self.requests]

    def __len__(self) -> int:
        return len(self.requests)


class FakeOpenerResponse:
    """The context-manager object a fake ``urllib`` opener hands back."""

    def __init__(
        self,
        body: bytes = b"",
        status: int | None = 200,
        headers: Any = None,
    ) -> None:
        self.body = body
        if status is not None:
            self.status = status
        if headers is not None:
            self.headers = headers

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "FakeOpenerResponse":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class FakeOpener:
    """Stands in for a ``urllib.request.OpenerDirector``; records prepared requests."""

    def __init__(self, *results: Any) -> None:
        self.results: list[Any] = list(results)
        self.requests: list[Any] = []
        self.timeouts: list[float] = []

    def open(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result
