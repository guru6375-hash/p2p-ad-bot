"""Shared, hermetic test scaffolding.

Nothing in this package touches the network: every exchange/Telegram interaction goes
through a :class:`FakeTransport` (:class:`~p2pbot.exchanges.base.Transport` protocol fake)
that records the :class:`HttpRequest` objects the product code builds and replays
scripted responses, and an autouse fixture blocks ``socket`` outright.

The module deliberately avoids importing :mod:`p2pbot.exchanges` / :mod:`p2pbot.services`
at import time: those packages currently pull in adapter/publisher modules that sibling
developers are still writing, and the Telegram tests must keep running regardless. Where
a real product class is needed, the concrete test module imports it itself (guarded by
``pytest.importorskip`` when the dependency may legitimately be absent).
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # `python -m pytest` already does this; be explicit
    sys.path.insert(0, str(PROJECT_ROOT))

SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
DEFAULT_TIMEOUT = 15.0


# --------------------------------------------------------------------------------------
# module availability probes
# --------------------------------------------------------------------------------------
def _importable(module: str) -> bool:
    """True when ``module`` can be imported right now (parent packages included)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


EXCHANGE_ADAPTERS_READY = _importable("p2pbot.exchanges.base")
SERVICES_READY = _importable("p2pbot.services")

requires_exchanges = pytest.mark.skipif(
    not EXCHANGE_ADAPTERS_READY,
    reason="p2pbot.exchanges imports the adapters, which are still being written",
)


# --------------------------------------------------------------------------------------
# fake transport
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FakeResponse:
    """Offline stand-in for :class:`p2pbot.exchanges.base.HttpResponse`.

    Mirrors the observable surface the product code relies on: ``status``, ``body``,
    ``text``, ``json`` (raising :class:`~p2pbot.errors.ApiError` on a non-JSON body) and
    case-insensitive ``header()`` lookup.
    """

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        from p2pbot.errors import ApiError

        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise ApiError(
                f"response body is not valid JSON (HTTP {self.status}): {self.text[:200]!r}",
                status=self.status,
            ) from exc

    def header(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default


def json_response(payload: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> FakeResponse:
    """A JSON response (``200`` by default)."""
    return FakeResponse(
        status=status,
        body=json.dumps(payload).encode("utf-8"),
        headers=dict(headers or {}),
    )


def text_response(text: str, status: int = 200, headers: Mapping[str, str] | None = None) -> FakeResponse:
    """A non-JSON body, e.g. an HTML error page from a CDN."""
    return FakeResponse(status=status, body=text.encode("utf-8"), headers=dict(headers or {}))


def empty_response(status: int = 204) -> FakeResponse:
    return FakeResponse(status=status, body=b"")


class FakeTransport:
    """Records outgoing requests and replays scripted responses or exceptions.

    ``send`` never opens a socket. Scripted items may be a :class:`FakeResponse`, an
    exception instance (raised), or a callable ``(request) -> FakeResponse`` for
    per-request scripting.
    """

    def __init__(self, responses: Sequence[Any] | None = None) -> None:
        self.requests: list[Any] = []
        self.timeouts: list[float] = []
        self._scripted: list[Any] = list(responses or [])

    # -- scripting ------------------------------------------------------------------
    def push(self, response: Any) -> "FakeTransport":
        self._scripted.append(response)
        return self

    def push_json(self, payload: Any, status: int = 200) -> "FakeTransport":
        return self.push(json_response(payload, status=status))

    def push_text(self, text: str, status: int = 200) -> "FakeTransport":
        return self.push(text_response(text, status=status))

    def push_error(self, exc: BaseException) -> "FakeTransport":
        return self.push(exc)

    # -- transport protocol ---------------------------------------------------------
    def send(self, request: Any, *, timeout: float = DEFAULT_TIMEOUT) -> FakeResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._scripted:
            raise AssertionError(
                f"FakeTransport received an unscripted request: {request.method} {request.url}"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        return item

    # -- inspection -----------------------------------------------------------------
    @property
    def last_request(self) -> Any:
        assert self.requests, "no request was sent"
        return self.requests[-1]

    def request_at(self, index: int) -> Any:
        return self.requests[index]

    def requests_to(self, needle: str) -> list[Any]:
        return [request for request in self.requests if needle in str(getattr(request, "url", ""))]

    def reset(self) -> None:
        self.requests.clear()
        self.timeouts.clear()


# --------------------------------------------------------------------------------------
# fake clock / no-network guard
# --------------------------------------------------------------------------------------
class FakeClock:
    """Deterministic, monotonic-by-construction clock for engine/scheduler tests."""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def set(self, moment: datetime) -> datetime:
        self.moment = moment
        return self.moment

    def advance(
        self,
        *,
        seconds: float = 0.0,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
    ) -> datetime:
        self.moment = self.moment + timedelta(
            seconds=seconds, minutes=minutes, hours=hours, days=days
        )
        return self.moment


class MonotonicFakeClock:
    """``time.monotonic``-shaped clock used by the Telegram rate limiter."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += float(seconds)
        return self.value


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to open a real socket."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test (or the code under test) tries to reach the network."""

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkBlocked("network access is forbidden in the test suite")

    monkeypatch.setattr(socket.socket, "connect", _blocked, raising=True)
    monkeypatch.setattr(socket, "create_connection", _blocked, raising=True)


# --------------------------------------------------------------------------------------
# settings / paths
# --------------------------------------------------------------------------------------
@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def http_request_bridge(monkeypatch: pytest.MonkeyPatch) -> bool:
    """Ensure ``from p2pbot.exchanges.base import HttpRequest`` works offline.

    ``p2pbot.telegram.api._send`` performs that lazy import so the Telegram layer never
    depends on the exchange layer at import time. While the adapter package is incomplete
    (its ``__init__`` imports every adapter), the parent package cannot be imported at all,
    so this fixture installs a minimal, behaviour-compatible stand-in for the two modules.
    Returns ``True`` when the real modules were used.
    """
    if EXCHANGE_ADAPTERS_READY:
        return True

    @dataclass(frozen=True)
    class _StubRequest:
        method: str
        url: str
        params: Mapping[str, str] | None = None
        json_body: Any = None
        form_body: Mapping[str, str] | None = None
        headers: Mapping[str, str] = field(default_factory=dict)

    package = types.ModuleType("p2pbot.exchanges")
    module = types.ModuleType("p2pbot.exchanges.base")
    module.HttpRequest = _StubRequest  # type: ignore[attr-defined]
    module.Transport = object  # type: ignore[attr-defined]
    package.base = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "p2pbot.exchanges", package)
    monkeypatch.setitem(sys.modules, "p2pbot.exchanges.base", module)
    return False


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def scenarios_dir() -> Path:
    return SCENARIOS_DIR


@pytest.fixture
def env_factory(tmp_path: Path) -> Callable[..., dict[str, str]]:
    """Build an ``env`` mapping for :func:`p2pbot.config.load_settings` (never real env)."""

    def build(**overrides: Any) -> dict[str, str]:
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
            "TELEGRAM_OWNER_ID": "4242",
            "STATE_PATH": str(tmp_path / "state.json"),
            "MARKET_PATH": str(tmp_path / "market.json"),
            "ADS_PATH": str(tmp_path / "ads.json"),
            "SCENARIOS_DIR": str(SCENARIOS_DIR),
            "LOG_PATH": str(tmp_path / "bot.log"),
            "BINANCE_1_API_KEY": "binance-key-1",
            "BINANCE_1_SECRET_KEY": "binance-secret-1",
            "BINANCE_2_API_KEY": "binance-key-2",
            "BINANCE_2_SECRET_KEY": "binance-secret-2",
            "OKX_1_API_KEY": "okx-key-1",
            "OKX_1_SECRET_KEY": "okx-secret-1",
            "OKX_1_PASSPHRASE": "okx-pass-1",
            "BYBIT_1_API_KEY": "bybit-key-1",
            "BYBIT_1_SECRET_KEY": "bybit-secret-1",
        }
        env.update({str(key).upper(): str(value) for key, value in overrides.items()})
        return env

    return build


@pytest.fixture
def settings(env_factory: Callable[..., dict[str, str]]) -> Any:
    from p2pbot.config import load_settings

    return load_settings(env_path=None, env=env_factory(), dotenv=False)


BASE_RATE_SCENARIO = "base_rate"


def base_rate_scenario_data(name: str = BASE_RATE_SCENARIO) -> dict[str, Any]:
    """A valid UAH ``market_middle`` scenario priced entirely from stored base rates.

    Every platform of both pairs overrides its source to ``base_rate`` and the parser is
    disabled, so the scenario has no market sources at all. Tests that need base_rate
    pricing or "a scenario without market sources" use it (the shipped ``pln.json`` is
    market-driven).
    """
    accounts = ["Binance#1", "Binance#2", "Okx#1", "Bybit#1"]

    def platforms() -> dict[str, dict[str, str]]:
        return {platform: {"source": "base_rate"} for platform in ("binance", "okx", "bybit")}

    return {
        "version": 1,
        "name": name,
        "fiat": "UAH",
        "strategy": "market_middle",
        "parser": {"enabled": False, "interval_minutes": 25},
        "defaults": {
            "min_amount": "1000",
            "max_amount": "200000",
            "payment_methods": ["Monobank", "PrivatBank"],
            "price_offset": "0",
        },
        "pairs": [
            {"pair": "UAH/USDT", "anchor": True, "accounts": list(accounts), "platforms": platforms()},
            {
                "pair": "UAH/USDC",
                "anchor": False,
                "linked_to": "UAH/USDT",
                "accounts": list(accounts),
                "platforms": platforms(),
            },
        ],
    }


def write_base_rate_scenario(directory: Path, name: str = BASE_RATE_SCENARIO) -> Path:
    """Write :func:`base_rate_scenario_data` as ``<directory>/<name>.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.json"
    target.write_text(json.dumps(base_rate_scenario_data(name), indent=2), encoding="utf-8")
    return target


@pytest.fixture
def base_rate_blueprint() -> Any:
    from p2pbot.blueprint import parse_blueprint

    return parse_blueprint(base_rate_scenario_data())


@pytest.fixture
def pln_blueprint() -> Any:
    from p2pbot.blueprint import load_blueprint

    return load_blueprint(SCENARIOS_DIR / "pln.json")


# --------------------------------------------------------------------------------------
# stub façade (Telegram tests own this shape)
# --------------------------------------------------------------------------------------
class StubServices:
    """The bot façade, stubbed for Telegram tests (never talks to a venue)."""

    def __init__(
        self,
        settings: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.settings = settings
        self.version = version
        self.clock = clock or FakeClock()
        self.uah_steps = {"binance": Decimal("0.25"), "bybit": Decimal("0.01")}

        # recorded calls: {"market": "uah"|"pln", "rate": ..., "dry_run": ...}
        self.setrate_calls: list[dict[str, Any]] = []

        # scripted outcomes
        self.setrate_report: Any = types.SimpleNamespace(
            queued=(), unchanged=(), results=(), problems=()
        )
        self.setrate_error: BaseException | None = None
        self.own_ads: tuple[Any, ...] = ()

    # -- façade operations ----------------------------------------------------------
    def get_own_ads(self) -> tuple[Any, ...]:
        return self.own_ads

    def _set_rate(self, market: str, rate: Decimal, dry_run: bool) -> Any:
        self.setrate_calls.append({"market": market, "rate": rate, "dry_run": dry_run})
        if self.setrate_error is not None:
            raise self.setrate_error
        return self.setrate_report

    def set_uah_rate(self, rate: Decimal, *, dry_run: bool = False) -> Any:
        return self._set_rate("uah", rate, dry_run)

    def set_pln_rate(self, rate: Decimal, *, dry_run: bool = False) -> Any:
        return self._set_rate("pln", rate, dry_run)


@pytest.fixture
def stub_services(settings: Any, clock: FakeClock) -> StubServices:
    return StubServices(settings, clock=clock)


# --------------------------------------------------------------------------------------
# convenience builders shared by several modules
# --------------------------------------------------------------------------------------
def make_pair(symbol: str = "UAH/USDT") -> Any:
    from p2pbot.models import Pair

    return Pair.parse(symbol)


def make_ad(
    *,
    platform: str = "binance",
    pair: str = "UAH/USDT",
    price: str = "47.00",
    user_type: str = "merchant",
    month_order_count: str | None = "501",
    positive_rate: str | None = "0.975",
    month_finish_rate: str | None = "0.95",
    adv_no: str = "adv-1",
) -> Any:
    """A competitor ad passing the hardcoded Binance thresholds by default."""
    from p2pbot.models import CompetitorAd, Pair

    def _dec(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return CompetitorAd(
        platform=platform,
        pair=Pair.parse(pair),
        price=Decimal(price),
        advertiser=f"{platform}-merchant",
        user_type=user_type,
        month_order_count=_dec(month_order_count),
        positive_rate=_dec(positive_rate),
        month_finish_rate=_dec(month_finish_rate),
        adv_no=adv_no,
    )


def make_snapshot(
    *,
    platform: str = "binance",
    pair: str = "UAH/USDT",
    prices: Sequence[str] = ("46.90", "47.10"),
    fetched_at: datetime | None = None,
) -> Any:
    """A stored snapshot whose filtered ads carry exactly ``prices``."""
    from p2pbot.market import build_snapshot
    from p2pbot.models import Pair

    ads = [
        make_ad(platform=platform, pair=pair, price=price, adv_no=f"{platform}-{index}")
        for index, price in enumerate(prices)
    ]
    return build_snapshot(
        platform, Pair.parse(pair), ads, filters=None, fetched_at=fetched_at
    )


__all__ = [
    "EXCHANGE_ADAPTERS_READY",
    "FakeClock",
    "FakeResponse",
    "FakeTransport",
    "MonotonicFakeClock",
    "NetworkBlocked",
    "PROJECT_ROOT",
    "SCENARIOS_DIR",
    "SERVICES_READY",
    "StubJobRow",
    "StubMarketRow",
    "StubRateRow",
    "StubScenario",
    "StubScenarioManager",
    "StubServices",
    "StubStatusSnapshot",
    "empty_response",
    "json_response",
    "make_ad",
    "make_pair",
    "make_snapshot",
    "requires_exchanges",
    "text_response",
]
