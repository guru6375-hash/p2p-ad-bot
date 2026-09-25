"""HTTP plumbing (stdlib only) and the exchange adapter contract.

Design notes
------------
* ``Transport`` is the only place a socket is opened; adapters receive it by injection so
  the entire suite runs offline with a fake transport.
* Request *builders* are pure and deterministic, including signature timestamps, which
  makes signing testable against fixed vectors.
* HTTP error statuses are returned as :class:`HttpResponse` (venues return their error
  payloads with 4xx/5xx); :class:`TransportError` is reserved for connection failures and
  adapters translate venue error payloads into :class:`ApiError`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Mapping, Protocol, runtime_checkable

from ..constants import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    FILTERS_BY_PLATFORM,
    SIDE_SELL,
)
from ..errors import ApiError, TransportError
from ..market import build_snapshot
from ..models import (
    Account,
    AdActionResult,
    AdSpec,
    CompetitorAd,
    Filters,
    MarketSnapshot,
    OwnAd,
    Pair,
    utcnow,
)

__all__ = [
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "UrllibTransport",
    "ExchangeAdapter",
    "safe_json",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpRequest:
    """A fully described outbound request (already signed, if signing applies)."""

    method: str
    url: str
    params: Mapping[str, str] | None = None
    json_body: Any = None
    form_body: Mapping[str, str] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def with_headers(self, extra: Mapping[str, str]) -> "HttpRequest":
        merged = dict(self.headers)
        merged.update(extra)
        return HttpRequest(
            method=self.method,
            url=self.url,
            params=self.params,
            json_body=self.json_body,
            form_body=self.form_body,
            headers=merged,
        )


@dataclass(frozen=True)
class HttpResponse:
    """A raw response. ``headers`` keys are lower-cased for predictable lookup."""

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def json(self) -> Any:
        """Parsed JSON body. Raises :class:`ApiError` when the body is not JSON."""
        try:
            return json.loads(self.text)
        except (ValueError, UnicodeDecodeError) as exc:
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


def safe_json(response: HttpResponse) -> Any:
    """Parse a response body, returning ``None`` instead of raising."""
    try:
        return response.json
    except ApiError:
        return None


@runtime_checkable
class Transport(Protocol):
    """Opens the actual socket. Implemented by :class:`UrllibTransport` and test fakes."""

    def send(self, request: HttpRequest, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResponse:
        ...


class UrllibTransport:
    """Default :class:`Transport` built on :mod:`urllib.request`."""

    def __init__(self, opener: Any | None = None, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._user_agent = user_agent

    def send(self, request: HttpRequest, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResponse:
        url = request.url
        if request.params:
            query = urllib.parse.urlencode(
                {key: value for key, value in request.params.items() if value is not None}
            )
            if query:
                url = f"{url}&{query}" if "?" in url else f"{url}?{query}"

        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        headers.update(request.headers or {})

        data: bytes | None = None
        if request.json_body is not None:
            data = json.dumps(request.json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif request.form_body is not None:
            data = urllib.parse.urlencode(request.form_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        prepared = urllib.request.Request(
            url, data=data, headers=headers, method=(request.method or "GET").upper()
        )
        try:
            with self._opener.open(prepared, timeout=timeout) as response:
                return HttpResponse(
                    status=int(getattr(response, "status", 200)),
                    body=response.read(),
                    headers=_lower_headers(getattr(response, "headers", None)),
                )
        except urllib.error.HTTPError as exc:  # pragma: no cover - exercised via fake opener
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001 - defensive: body may be unreadable
                body = b""
            return HttpResponse(
                status=int(exc.code), body=body or b"", headers=_lower_headers(exc.headers)
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:  # pragma: no cover - network
            raise TransportError(f"{request.method} {url} failed: {exc}") from exc


def _lower_headers(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - defensive
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _venue_reason(payload: Any) -> str:
    """``" (code=..., msg=...)"`` from a venue error body, ``""`` when it names none."""
    if not isinstance(payload, Mapping):
        return ""
    code = next((payload[key] for key in ("code", "ret_code", "error_code") if payload.get(key) not in (None, "")), None)
    message = next((payload[key] for key in ("msg", "ret_msg", "message", "error_message") if payload.get(key)), None)
    parts = [f"code={code}" if code is not None else "", f"msg={message}" if message else ""]
    text = ", ".join(part for part in parts if part)
    return f" ({text})" if text else ""


class ExchangeAdapter(ABC):
    """Base class for the Binance / OKX / ByBit P2P adapters.

    Subclasses implement the venue-specific request builders and response parsers; the
    shared :meth:`search_ads` orchestration lives here.
    """

    platform: ClassVar[str] = ""

    #: Rows asked for per page by :meth:`fetch_own_ads`.
    own_ads_page_size: ClassVar[int] = 100
    #: Upper bound on the pages :meth:`fetch_own_ads` reads for one account.
    own_ads_max_pages: ClassVar[int] = 50

    def __init__(
        self,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport: Transport = transport if transport is not None else UrllibTransport()
        self._now = now or utcnow

    # -- clock ---------------------------------------------------------------------
    def now(self) -> datetime:
        return self._now()

    # -- shared orchestration ------------------------------------------------------
    def search_ads(
        self,
        pair: Pair,
        *,
        side: str = SIDE_SELL,
        page: int = 1,
        rows: int = 20,
        filters: Filters | None = None,
    ) -> MarketSnapshot:
        """Fetch competitor advertisements and apply the advertiser filter."""
        request = self.build_search_request(pair, side=side, page=page, rows=rows)
        response = self.transport.send(request)
        if response.status >= 400:
            raise ApiError(
                f"{self.platform} search for {pair.symbol} failed with HTTP {response.status}",
                payload=safe_json(response),
                status=response.status,
            )
        payload = response.json
        self.ensure_success(payload)
        ads = tuple(self.parse_search_response(payload, pair))
        effective = filters if filters is not None else FILTERS_BY_PLATFORM.get(self.platform, Filters())
        return build_snapshot(
            platform=self.platform,
            pair=pair,
            ads=ads,
            filters=effective,
            fetched_at=self.now(),
        )

    # -- venue hooks ---------------------------------------------------------------
    @abstractmethod
    def build_search_request(
        self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20
    ) -> HttpRequest:
        """Public competitor search request."""

    @abstractmethod
    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]:
        """Normalize a public search payload into :class:`CompetitorAd` objects."""

    @abstractmethod
    def ensure_success(self, payload: Any) -> None:
        """Raise :class:`ApiError` when a *public* payload reports its own failure."""

    @abstractmethod
    def build_login_request(self, account: Account) -> HttpRequest | None:
        """Session bootstrap request, or ``None`` when the venue needs no session."""

    @abstractmethod
    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest:
        """Request that lists this account's own advertisements for ``pair``."""

    @abstractmethod
    def build_update_ad_request(self, account: Account, spec: AdSpec, adv_no: str) -> HttpRequest:
        """Request that updates an existing advertisement."""

    @abstractmethod
    def parse_ad_response(self, payload: Any) -> AdActionResult:
        """Normalize a private update payload.

        The returned record only needs to carry what the venue tells us (``adv_no`` from
        ``data``/``result.itemId``, an echoed ``price``, ``raw``); the identity fields are
        filled in by :meth:`parse_ad_result`.
        """

    def parse_ad_result(
        self,
        payload: Any,
        *,
        account: Account,
        pair: Pair,
        spec: AdSpec,
        adv_no: str | None = None,
    ) -> AdActionResult:
        """Bridge a venue payload to a fully identified :class:`AdActionResult`.

        Adapters with a richer payload shape may override this; the default completes the
        identity fields (platform/account/pair/price) from the known request
        context so callers never have to patch them in. ``adv_no`` is the id the caller
        addressed; it is used when the venue's update response does not echo it.

        ``AdSpec.active`` is the single on/off control of the whole system: adapters map it
        onto whatever the venue offers (e.g. an ``updateStatus`` payload, an ``ACTIVE``
        action, or a take-down request).
        """
        parsed = self.parse_ad_response(payload)
        return AdActionResult(
            platform=self.platform,
            account_id=account.id,
            pair=pair,
            adv_no=parsed.adv_no or adv_no,
            price=parsed.price if parsed.price is not None else spec.price,
            raw=parsed.raw,
        )

    # -- private execution helpers -------------------------------------------------
    def send_private(self, account: Account, request: HttpRequest) -> Any:
        """Send a private request and return its parsed JSON, raising on venue errors."""
        response = self.transport.send(request)
        payload = safe_json(response)
        if response.status >= 400:
            raise ApiError(
                f"{self.platform} request for account {account.id} failed with HTTP "
                f"{response.status}{_venue_reason(payload)}",
                payload=payload,
                status=response.status,
            )
        self.ensure_success(payload)
        return payload

    def list_my_ads(self, account: Account, pair: Pair) -> tuple[Mapping[str, Any], ...]:
        """List this account's advertisements for ``pair`` (raw venue records)."""
        payload = self.send_private(account, self.build_list_ads_request(account, pair))
        return self.parse_ad_list(payload)

    def fetch_own_ads(self, account: Account) -> tuple[OwnAd, ...]:
        """Every advertisement of ``account`` on this venue: all pairs, online or not.

        Pages are read until one comes back empty, adds nothing new (a venue ignoring the
        page number), or is shorter than both the requested size and the pages before it, or
        until :attr:`own_ads_max_pages` is reached. A venue may silently cap the page size
        (Binance returns 20 rows when asked for 100), so a page shorter than requested only
        ends the listing once it is also shorter than the earlier pages. Rows the adapter
        cannot identify are skipped; the same ``adv_no`` is listed once.
        """
        ads: list[OwnAd] = []
        seen: set[str] = set()
        longest = 0
        for page in range(1, self.own_ads_max_pages + 1):
            payload = self.send_private(account, self.build_own_ads_request(account, page=page))
            rows = self.parse_ad_list(payload)
            added = 0
            for row in rows:
                ad = self.parse_own_ad(row, account)
                if ad is None or ad.adv_no in seen:
                    continue
                seen.add(ad.adv_no)
                ads.append(ad)
                added += 1
            if not rows or not added:
                return tuple(ads)
            if len(rows) < self.own_ads_page_size and len(rows) < longest:
                return tuple(ads)
            longest = max(longest, len(rows))
        _log.warning(
            "%s own-ad listing of %s stopped after %d pages; later ads are not listed",
            self.platform,
            account.id,
            self.own_ads_max_pages,
        )
        return tuple(ads)

    @abstractmethod
    def build_own_ads_request(self, account: Account, *, page: int) -> HttpRequest:
        """One page of the listing of every own advertisement, across pairs and states."""

    @abstractmethod
    def parse_own_ad(self, row: Mapping[str, Any], account: Account) -> OwnAd | None:
        """Normalize one own-ad listing row, or ``None`` when it cannot be identified."""

    def parse_ad_list(self, payload: Any) -> tuple[Mapping[str, Any], ...]:
        """Extract the raw advertisement records from a list payload.

        Non-mapping items are dropped so the declared return type holds even for a payload
        that mixes objects with scalars.
        """
        if not isinstance(payload, (list, tuple)):
            return ()
        return tuple(item for item in payload if isinstance(item, Mapping))
