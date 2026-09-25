"""Binance P2P adapter: public competitor search + C2C Agent SAPI ad management.

Two disjoint families are wrapped here (see ``docs/research/binance.md`` and
``docs/research/ground-truth-probes.md``):

1. **Public competitor search** — ``POST https://p2p.binance.com/bapi/c2c/v2/friendly/
   c2c/adv/search`` with a JSON body. Unauthenticated (no key, cookie or CSRF header is
   used). ``tradeType="SELL"`` returns the *ask* ads (the advertisers who sell crypto,
   i.e. the side we compete on); the ``adv.tradeType`` field of the response is mirrored —
   it reports the taker side — so this adapter never derives an ad's side from it.

2. **Own-advertisement management** — the documented *C2C Agent SAPI* family on
   ``https://api.binance.com``, authenticated with an API key (``X-MBX-APIKEY``) and a
   hex HMAC-SHA256 signature. Credentials read from the account: ``API_KEY``,
   ``SECRET_KEY``. No session bootstrap exists for this venue, so
   :meth:`BinanceAdapter.build_login_request` returns ``None``.

   ==========================================  ===========================================
   operation                                   request
   ==========================================  ===========================================
   update (full object)                        ``POST /sapi/v1/c2c/agent/ads/update``
   on/off                                      ``POST /sapi/v1/c2c/agent/ads/updateStatus``
   list own ads                                ``POST .../ads/listWithPagination``
   one own ad                                  ``POST .../ads/getDetailByNo?advNo=...``
   own payment methods (needed by SELL ads)    ``GET  .../ads/getPayMethodByUserId``
   ==========================================  ===========================================

Signing (see :meth:`BinanceAdapter._signed_request`): ``timestamp`` (ms, from the
injectable :meth:`~p2pbot.exchanges.base.ExchangeAdapter.now` clock) and ``signature`` are
*query-string* parameters while the JSON payload is the body; the signature is
``hex(HMAC-SHA256(secret, percent-encoded query string))`` computed over the parameters **in
insertion order** — Binance's own reference for this family explicitly says *do not sort*
parameters (unlike the standard REST API). ``recvWindow`` defaults to 60000 ms.

Known-unknowns (documented instead of silently assumed):

* **Quoted numerics.** The write schemas type ``price``/amounts as numbers, but money is
  never serialized from a ``float`` in this project, and the transport's ``json.dumps``
  cannot encode ``Decimal``: values we *set* are therefore sent as decimal **strings**
  (``"47.00"``). Binance's JSON parsers accept quoted numerics for numeric fields; a live
  merchant trial is the only way to confirm this for these endpoints (UNCONFIRMED).
  Values read back from the venue (the ``getDetailByNo`` detail) are echoed verbatim.
* **``tradeType`` on update.** Reads (``getDetailByNo``/``listWithPagination``) return
  ``"BUY"``/``"SELL"`` while writes take the *numeric* enum (``"0"``/``"1"``), so the
  merged update object normalizes that one field; everything else is echoed untouched.
* **``advStatus`` 2 vs 3.** Binance's API reference says ``3`` = Offline for writes while
  the same repository's display table says ``2``; this adapter sends ``3`` (as the
  reference states). ``1`` = Online, ``4`` = Closed.
* **Buy advertisements only.** Ad updates are built for BUY ads alone; a sell spec is
  refused before any request. BUY ads identify payment methods by ``identifier``: an update
  keeps the ad's own identifiers, ``AdSpec.payment_ids`` are sent verbatim, and display
  names in ``AdSpec.payment_methods`` are resolved against the account's own payment
  methods (cached per ``(account, fiat)``): an exact case-insensitive match first, then a
  substring match either way. Anything unmatched raises :class:`ApiError` naming the method.
* Nothing here can be verified without live Binance merchant credentials: the whole
  Agent SAPI family (update/status/list/pay-methods) is unauthenticated-untestable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import urllib.parse
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable, ClassVar, Iterable, Mapping, Sequence

from ..constants import SIDE_BUY, SIDE_SELL
from ..errors import ApiError, ConfigError
from ..models import (
    AD_STATUS_CLOSED,
    AD_STATUS_OFFLINE,
    AD_STATUS_ONLINE,
    AD_STATUS_UNKNOWN,
    Account,
    AdActionResult,
    AdSpec,
    CompetitorAd,
    OwnAd,
    Pair,
    parse_decimal,
)
from .base import ExchangeAdapter, HttpRequest, Transport

__all__ = ["BinanceAdapter"]

_log = logging.getLogger(__name__)

# -- public search ---------------------------------------------------------------------
SEARCH_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"

# -- C2C Agent SAPI (API-key + HMAC-SHA256) --------------------------------------------
API_BASE_URL = "https://api.binance.com"
ADS_UPDATE_PATH = "/sapi/v1/c2c/agent/ads/update"
ADS_UPDATE_STATUS_PATH = "/sapi/v1/c2c/agent/ads/updateStatus"
ADS_LIST_PATH = "/sapi/v1/c2c/agent/ads/listWithPagination"
ADS_DETAIL_PATH = "/sapi/v1/c2c/agent/ads/getDetailByNo"
PAY_METHODS_PATH = "/sapi/v1/c2c/agent/ads/getPayMethodByUserId"

#: Signature envelope: P2P endpoints accept up to 60000 ms (Binance's own reference).
RECV_WINDOW_MS = 60000
#: ``priceType`` of a fixed-price advertisement.
DEFAULT_PRICE_TYPE = 1
#: ``priceType`` of a floating-price advertisement (``priceFloatingRatio`` percent).
FLOATING_PRICE_TYPE = 2

#: ``tradeType`` spelling the ad update body expects: only buy ads are updated.
ADS_TRADE_TYPE: dict[str, str] = {SIDE_BUY: "BUY"}

#: Asset scale of the USDT/USDC amounts observed live (``adv.assetScale == 2``): a derived
#: ``initAmount`` is floored to this quantum so the venue never sees a sub-unit quantity.
DEFAULT_QUANTITY_QUANTUM = Decimal("0.01")
#: ``advStatus`` enum for writes (1 = Online, 3 = Offline, 4 = Closed).
ADV_STATUS_ONLINE = 1
ADV_STATUS_OFFLINE = 3
#: ``advStatus`` of a listed own ad -> :class:`~p2pbot.models.OwnAd` status. Both ``2`` and
#: ``3`` read as offline (Binance's two reference pages disagree, see the module docstring).
OWN_AD_STATUSES: dict[str, str] = {
    "1": AD_STATUS_ONLINE,
    "2": AD_STATUS_OFFLINE,
    "3": AD_STATUS_OFFLINE,
    "4": AD_STATUS_CLOSED,
}
#: ``tradeType`` of a listed own ad (reads say ``"SELL"``, writes use ``"1"``) -> side.
OWN_AD_SIDES: dict[str, str] = {"SELL": SIDE_SELL, "1": SIDE_SELL, "BUY": SIDE_BUY, "0": SIDE_BUY}
#: Envelope codes that mean "the venue reported success" ("000000" and numeric 0 shapes).
SUCCESS_CODES: tuple[str, ...] = ("000000", "0")

#: ``AdSpec.side`` -> public ``tradeType`` (the advertiser's own side).
PUBLIC_TRADE_TYPE: dict[str, str] = {SIDE_SELL: "SELL", SIDE_BUY: "BUY"}

#: Index of the points that cannot be verified without live Binance merchant credentials,
#: with the decision taken for each (the prose lives in the module docstring's
#: "Known-unknowns" section). Nothing here is silently guessed.
UNCONFIRMED: tuple[str, ...] = (
    "prices/amounts we set are sent as quoted decimals ('47.00') in ads/post|ads/update "
    "JSON bodies: Binance types them as numbers, but money is never serialized from a float",
    "the merged ads/update object normalizes tradeType from the read shape ('SELL') to the "
    "write enum ('1'), as documented in docs/research/binance.md",
    "advStatus=3 (not 2) for Offline writes; Binance's reference and its display table disagree",
    "the getPayMethodByUserId record shape (payId/payType/tradeMethodName field names)",
    "BUY ads address payment methods by 'identifier'; this adapter takes those from "
    "AdSpec.payment_ids (listAllTradeMethods is out of scope)",
    "recvWindow=60000 ms and the no-sorting rule for SAPI P2P signatures (single-sourced "
    "from Binance's own skill repository)",
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
#: Placeholder pair for :meth:`BinanceAdapter.parse_ad_response`, whose result identity
#: fields are overwritten by ``ExchangeAdapter.parse_ad_result`` (``XXX`` is ISO-4217 "no
#: currency", the conventional neutral placeholder).
_UNKNOWN_PAIR = Pair("XXX", "XXX")


def _text(value: Any) -> str:
    """Whitespace-stripped text for a venue field; ``""`` for ``None``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _decimal_text(value: Decimal) -> str:
    """Plain (never scientific) decimal string for a JSON body field."""
    return format(value, "f")


def _optional_decimal(value: Any, field_name: str) -> Decimal | None:
    """``Decimal`` for a venue value, ``None`` when absent/blank.

    Binance reports rates as JSON numbers, so a ``float`` may legitimately arrive here; it
    is routed through :func:`str` (Python's shortest round-trip representation) and then
    parsed as a decimal, so no binary float ever reaches a model field.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be a decimal value, got a boolean")
    if isinstance(value, float):
        value = str(value)
    text = _text(value)
    if not text:
        return None
    return parse_decimal(text, field_name)


def _lenient_decimal(value: Any, field_name: str) -> Decimal | None:
    """:func:`_optional_decimal` for a display-only field: junk reads as ``None``."""
    try:
        return _optional_decimal(value, field_name)
    except ConfigError:
        _log.debug("binance %s is not a decimal (%r); field ignored", field_name, value)
        return None


def _trade_method_names(methods: Any) -> tuple[str, ...]:
    """Display names (``identifier`` as fallback) of an ad's ``tradeMethods``."""
    if not isinstance(methods, list):
        return ()
    names = (
        _text(method.get("tradeMethodName") or method.get("identifier"))
        for method in methods
        if isinstance(method, Mapping)
    )
    return tuple(name for name in names if name)


def _normalized_rate(value: Any, field_name: str) -> Decimal | None:
    """A ``positiveRate``/``monthFinishRate`` on the shared ``0..1`` scale.

    Binance already reports fractions (``0.98979591``); a hypothetical percentage
    (``97.5``) is divided by 100 so the filter thresholds stay venue-independent.
    """
    rate = _optional_decimal(value, field_name)
    if rate is None:
        return None
    return rate / 100 if rate > 1 else rate


class BinanceAdapter(ExchangeAdapter):
    """Binance P2P adapter (public search + C2C Agent SAPI own-ad management)."""

    platform: ClassVar[str] = "binance"
    #: ``listWithPagination`` returns at most 20 rows per page whatever ``rows`` asks for.
    own_ads_page_size: ClassVar[int] = 20

    def __init__(
        self,
        transport: Transport | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(transport, now=now)
        #: ``(account id, fiat)`` -> the account's own payment-method records.
        self._pay_methods_cache: dict[tuple[str, str], tuple[Mapping[str, str], ...]] = {}

    # -- public search -------------------------------------------------------------
    def build_search_request(
        self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20
    ) -> HttpRequest:
        """Unauthenticated competitor-ads search (``side`` is the *advertiser's* side)."""
        body = {
            "page": max(1, int(page)),
            "rows": max(1, int(rows)),
            "payTypes": [],
            "publisherType": None,
            "asset": pair.crypto,
            "fiat": pair.fiat,
            "tradeType": self._public_trade_type(side),
        }
        return HttpRequest(
            method="POST",
            url=SEARCH_URL,
            json_body=body,
            headers={"Content-Type": "application/json"},
        )

    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]:
        """Normalize ``data[]`` items into :class:`CompetitorAd` records.

        Metrics the venue does not publish stay ``None`` (never a made-up zero) so the
        filter drops them; the fields with no home in the model (``minSingleTransAmount``,
        ``maxSingleTransAmount``, ``tradableQuantity``, ``surplusAmount``,
        ``tradeMethods[].tradeMethodName``, ``payTimeLimit``, …) stay reachable through
        ``CompetitorAd.raw``, which holds the item exactly as the venue sent it.
        """
        items = self._search_items(payload)
        ads: list[CompetitorAd] = []
        for item in items:
            ad = self._parse_ad_item(item, pair)
            if ad is not None:
                ads.append(ad)
        return tuple(ads)

    def ensure_success(self, payload: Any) -> None:
        """Raise :class:`ApiError` unless the envelope reports success.

        Accepts the string ``"000000"`` and the numeric ``0`` code shapes; any other code
        (or a missing code, or ``success: false``) is a venue failure and carries the
        payload on the error.
        """
        if not isinstance(payload, Mapping):
            raise ApiError(
                f"binance returned a non-object payload ({type(payload).__name__})",
                payload=payload,
            )
        code = payload.get("code")
        if isinstance(code, str):
            ok = code.strip() in SUCCESS_CODES
        elif isinstance(code, bool):
            ok = False
        elif isinstance(code, int):
            ok = code == 0
        else:
            ok = False
        if payload.get("success") is False:
            # The venue can also flag failure through the boolean alone.
            ok = False
        if ok:
            return
        detail = _text(payload.get("message") or payload.get("messageDetail") or payload.get("msg"))
        raise ApiError(
            f"binance reported failure (code={code!r}{f', message={detail!r}' if detail else ''})",
            payload=payload,
        )

    # -- private: shaping ----------------------------------------------------------
    def build_login_request(self, account: Account) -> HttpRequest | None:
        """``None``: the Agent SAPI family is API-key/HMAC authenticated, no session."""
        return None

    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest:
        """Own-ads listing; the endpoint takes no asset/fiat filter, so ``pair`` is unused.

        The caller (``list_my_ads``) reconciles the returned records against the pair.
        """
        return self._signed_request(
            account, "POST", ADS_LIST_PATH, json_body={"page": 1, "rows": 100}
        )

    def build_own_ads_request(self, account: Account, *, page: int) -> HttpRequest:
        """One ``listWithPagination`` page: every own ad, whatever its pair or status."""
        return self._signed_request(
            account,
            "POST",
            ADS_LIST_PATH,
            json_body={"page": int(page), "rows": self.own_ads_page_size},
        )

    def parse_own_ad(self, row: Mapping[str, Any], account: Account) -> OwnAd | None:
        """Normalize one ``AgentAdDetailResp`` row of the own-ad listing."""
        adv_no = _text(row.get("advNo"))
        try:
            pair = Pair(fiat=_text(row.get("fiatUnit")), crypto=_text(row.get("asset")))
        except ConfigError:
            pair = None
        if not adv_no or pair is None:
            _log.debug("binance %s: skipped an own-ad row without advNo/asset/fiatUnit", account.id)
            return None
        venue_status = _text(row.get("advStatus"))
        return OwnAd(
            platform=self.platform,
            account_id=account.id,
            adv_no=adv_no,
            pair=pair,
            side=OWN_AD_SIDES.get(_text(row.get("tradeType")).upper(), ""),
            status=OWN_AD_STATUSES.get(venue_status, AD_STATUS_UNKNOWN),
            price=_lenient_decimal(row.get("price"), "price"),
            min_amount=_lenient_decimal(row.get("minSingleTransAmount"), "minSingleTransAmount"),
            max_amount=_lenient_decimal(row.get("maxSingleTransAmount"), "maxSingleTransAmount"),
            quantity=_lenient_decimal(row.get("surplusAmount"), "surplusAmount"),
            payment_methods=_trade_method_names(row.get("tradeMethods")),
            venue_status=venue_status,
            total_quantity=_lenient_decimal(row.get("initAmount"), "initAmount"),
            price_floating_ratio=(
                _lenient_decimal(row.get("priceFloatingRatio"), "priceFloatingRatio")
                if _text(row.get("priceType")) == str(FLOATING_PRICE_TYPE)
                else None
            ),
        )

    def build_update_ad_request(self, account: Account, spec: AdSpec, adv_no: str) -> HttpRequest:
        """Update an existing advertisement.

        ``AdSpec.active`` selects the mechanism:

        * ``False`` -> :meth:`build_status_request` (``updateStatus`` ``advStatus=3``); the
          ad is taken offline and nothing else is touched.
        * ``True`` -> the **full-object** workflow Binance demands: ``getDetailByNo`` is
          sent through the transport first (Binance answers ``-9000`` when a partial
          object reaches ``ads/update``), and its ``data`` object is re-posted with the
          price, amounts, status and normalized ``tradeType`` overwritten.

        Unlike the other builders this one performs I/O; it is deterministic given
        ``(account, spec, adv_no, now)`` plus the venue's own detail payload. Only buy ads
        are handled: any other side is refused before a request is built.
        """
        if str(spec.side).strip().lower() != SIDE_BUY:
            raise ConfigError(f"binance: only buy advertisements are handled, not {spec.side!r}")
        if not spec.active:
            return self.build_status_request(account, adv_no, active=False)
        detail = self._fetch_ad_detail(account, adv_no)
        body = self._merged_update_body(account, detail, spec, adv_no)
        return self._signed_request(account, "POST", ADS_UPDATE_PATH, json_body=body)

    def build_status_request(self, account: Account, adv_no: str, *, active: bool) -> HttpRequest:
        """Take one ad online (``advStatus=1``) or offline (``advStatus=3``)."""
        return self._signed_request(
            account,
            "POST",
            ADS_UPDATE_STATUS_PATH,
            json_body={
                "advNos": [str(adv_no)],
                "advStatus": ADV_STATUS_ONLINE if active else ADV_STATUS_OFFLINE,
            },
        )

    # -- private: parsing ----------------------------------------------------------
    def parse_ad_list(self, payload: Any) -> tuple[Mapping[str, Any], ...]:
        """The raw own-advertisement records of a paginated list payload."""
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                for key in ("items", "list", "rows"):
                    candidate = data.get(key)
                    if isinstance(candidate, list):
                        data = candidate
                        break
            if isinstance(data, list):
                return tuple(item for item in data if isinstance(item, Mapping))
        return super().parse_ad_list(payload)

    def parse_ad_response(
        self, payload: Any, *, pair: Pair | None = None
    ) -> AdActionResult:
        """Normalize an update payload, carrying only what the venue says.

        ``data`` is ``true`` on update and the ``{status, failList}`` object on
        ``updateStatus``; everything else (identity fields, price) is filled in by
        ``parse_ad_result``.
        """
        data = payload.get("data") if isinstance(payload, Mapping) else None
        adv_no: str | None = None
        if isinstance(data, str):
            adv_no = data.strip() or None
        elif isinstance(data, Mapping):
            adv_no = _text(data.get("advNo") or data.get("adNo")) or None
        return AdActionResult(
            platform=self.platform,
            account_id="",
            pair=pair if pair is not None else _UNKNOWN_PAIR,
            adv_no=adv_no,
            price=None,
            raw=payload if isinstance(payload, Mapping) else {"payload": payload},
        )

    # -- internals: search ---------------------------------------------------------
    @staticmethod
    def _public_trade_type(side: str) -> str:
        try:
            return PUBLIC_TRADE_TYPE[str(side).strip().lower()]
        except KeyError as exc:
            raise ConfigError(f"binance: unsupported side {side!r}") from exc

    @staticmethod
    def _ads_trade_type(side: str) -> str:
        """``tradeType`` for the ad endpoints (``"SELL"``/``"BUY"``)."""
        try:
            return ADS_TRADE_TYPE[str(side).strip().lower()]
        except KeyError as exc:
            raise ConfigError(f"binance: unsupported side {side!r}") from exc

    @staticmethod
    def _search_items(payload: Any) -> Sequence[Any]:
        if not isinstance(payload, Mapping):
            raise ApiError(
                f"binance search payload was not an object ({type(payload).__name__})",
                payload=payload,
            )
        items = payload.get("data")
        if items is None:
            return ()
        if not isinstance(items, list):
            raise ApiError("binance search payload carried a non-list data field", payload=payload)
        return items

    def _parse_ad_item(self, item: Any, pair: Pair) -> CompetitorAd | None:
        """One ``data[]`` item, or ``None`` when it is unusable.

        A row whose numeric fields do not parse is *skipped* (logged), never raised: the venue
        ships free-form strings, and one junk advertisement must not abort the parse pass for
        every platform and pair — the sibling adapters skip such rows too.
        """
        if not isinstance(item, Mapping):
            _log.debug("binance search: skipping non-object item %r", type(item).__name__)
            return None
        adv = item.get("adv")
        advertiser = item.get("advertiser")
        adv = adv if isinstance(adv, Mapping) else {}
        advertiser = advertiser if isinstance(advertiser, Mapping) else {}
        try:
            price = _optional_decimal(adv.get("price"), "adv.price")
            if price is None or price <= 0:
                _log.debug("binance search: skipping ad without a positive price")
                return None
            return CompetitorAd(
                platform=self.platform,
                pair=pair,
                price=price,
                advertiser=_text(advertiser.get("nickName")) or _text(advertiser.get("userNo")),
                # The venue ships "merchant"/"user"; normalized lowercase either way.
                user_type=_text(advertiser.get("userType")).lower(),
                month_order_count=_optional_decimal(
                    advertiser.get("monthOrderCount"), "advertiser.monthOrderCount"
                ),
                positive_rate=_normalized_rate(
                    advertiser.get("positiveRate"), "advertiser.positiveRate"
                ),
                month_finish_rate=_normalized_rate(
                    advertiser.get("monthFinishRate"), "advertiser.monthFinishRate"
                ),
                adv_no=_text(adv.get("advNo")) or None,
                # The raw item, so the fields CompetitorAd has no room for stay available.
                raw=item,
            )
        except ConfigError as exc:
            _log.warning(
                "binance search: skipping malformed advertisement %s: %s", adv.get("advNo"), exc
            )
            return None

    # -- internals: signing --------------------------------------------------------
    def _timestamp_ms(self) -> int:
        """Millisecond epoch from the injectable clock (integer math, no float)."""
        moment = self.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        delta = moment - _EPOCH
        return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000

    @staticmethod
    def _sign(secret: str, params: Mapping[str, str]) -> str:
        """Hex HMAC-SHA256 over the percent-encoded query string, insertion order kept."""
        query = urllib.parse.urlencode(list(params.items()))
        return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _signed_request(
        self,
        account: Account,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> HttpRequest:
        """A private request: ``X-MBX-APIKEY`` header, signed query string, JSON body."""
        query: dict[str, str] = {}
        for key, value in (params or {}).items():
            query[str(key)] = str(value)
        query["timestamp"] = str(self._timestamp_ms())
        query["recvWindow"] = str(RECV_WINDOW_MS)
        query["signature"] = self._sign(account.require("SECRET_KEY"), query)
        headers = {"X-MBX-APIKEY": account.require("API_KEY")}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        return HttpRequest(
            method=method,
            url=f"{API_BASE_URL}{path}",
            params=query,
            json_body=json_body,
            headers=headers,
        )

    # -- internals: private ad shaping ---------------------------------------------
    def _ad_quantity(self, account: Account, spec: AdSpec) -> Decimal:
        """Advertised token amount: ``spec.quantity``, else ``floor(max_amount / price)``."""
        if spec.quantity is not None:
            if spec.quantity <= 0:
                raise ConfigError(
                    f"binance: advertisement quantity for {account.id} {spec.pair.symbol} "
                    f"must be positive, got {spec.quantity}"
                )
            return spec.quantity
        if spec.price <= 0:
            raise ConfigError(
                f"binance: cannot derive an advertisement quantity for {account.id} "
                f"{spec.pair.symbol} from price {spec.price}"
            )
        quantity = (spec.max_amount / spec.price).quantize(
            DEFAULT_QUANTITY_QUANTUM, rounding=ROUND_FLOOR
        )
        if quantity <= 0:
            raise ConfigError(
                f"binance: derived advertisement quantity for {account.id} {spec.pair.symbol} "
                f"is {quantity}; raise max_amount or lower the price"
            )
        return quantity

    def _fetch_ad_detail(self, account: Account, adv_no: str) -> Mapping[str, Any]:
        """``data`` of ``getDetailByNo`` — the object ``ads/update`` expects back."""
        request = self._signed_request(
            account, "POST", ADS_DETAIL_PATH, params={"advNo": str(adv_no)}
        )
        payload = self.send_private(account, request)
        detail = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(detail, Mapping):
            raise ApiError(
                f"binance advertisement {adv_no} of account {account.id} has no detail object",
                payload=payload,
            )
        return detail

    def _merged_update_body(
        self, account: Account, detail: Mapping[str, Any], spec: AdSpec, adv_no: str
    ) -> dict[str, Any]:
        """The detail object with the values this adapter changes overwritten.

        Every field the detail returned is echoed verbatim; only ``advNo`` (pinned to the
        ad we addressed), ``tradeType`` (normalized to the venue's own ``"SELL"``/``"BUY"`` spelling),
        ``priceType``/``price`` (``priceType``/``priceFloatingRatio`` for a spec with a
        ``price_floating_ratio``, keeping the venue's own ``price``), the amounts,
        ``initAmount`` and ``advStatus`` are set.
        Payment methods posted are the ones the ad already carries.
        """
        body: dict[str, Any] = {str(key): value for key, value in detail.items()}
        body["advNo"] = str(adv_no)
        body["tradeType"] = self._ads_trade_type(spec.side)
        if spec.price_floating_ratio is None:
            body["priceType"] = DEFAULT_PRICE_TYPE
            body["price"] = _decimal_text(spec.price)
            # a floating ad's echoed ratio keeps it floating: Binance answers success and
            # ignores the fixed price, so the ratio goes when the ad becomes fixed-price
            body.pop("priceFloatingRatio", None)
        else:
            body["priceType"] = FLOATING_PRICE_TYPE
            body["priceFloatingRatio"] = _decimal_text(spec.price_floating_ratio)
        body["initAmount"] = _decimal_text(self._ad_quantity(account, spec))
        body["minSingleTransAmount"] = _decimal_text(spec.min_amount)
        body["maxSingleTransAmount"] = _decimal_text(spec.max_amount)
        body["advStatus"] = ADV_STATUS_ONLINE if spec.active else ADV_STATUS_OFFLINE
        body["tradeMethods"] = self._update_trade_methods(account, detail, spec)
        return body

    def _update_trade_methods(
        self, account: Account, detail: Mapping[str, Any], spec: AdSpec
    ) -> list[dict[str, Any]]:
        """Write-shape ``tradeMethods`` for a buy-ad update.

        ``getDetailByNo`` answers with the *read* shape (``identifier``/``tradeMethodName``/
        ``iconUrlColor``); a buy ad is written back with its ``identifier`` values only. The
        spec's methods, when it carries any, replace the ad's own.
        """
        if spec.payment_methods or spec.payment_ids:
            return self._resolve_trade_methods(account, spec)
        entries: list[dict[str, Any]] = []
        for current in detail.get("tradeMethods") or []:
            if not isinstance(current, Mapping):
                continue
            wanted = _text(current.get("identifier")) or _text(current.get("payType"))
            if wanted:
                entries.append({"identifier": wanted})
        if not entries:
            raise ApiError(
                f"binance: advertisement {detail.get('advNo')} carries no payment method and "
                "the spec supplies none; refusing to update it"
            )
        return entries

    # -- internals: payment methods ------------------------------------------------
    def _resolve_trade_methods(self, account: Account, spec: AdSpec) -> list[dict[str, Any]]:
        """Buy-ad ``tradeMethods`` entries for the spec's payment methods.

        ``payment_ids`` are venue identifiers and are used as they are; display names are
        resolved to an identifier through the account's own payment methods. Names that
        resolve to nothing raise :class:`ApiError` naming the method (never a credential).
        """
        names = [_text(name) for name in spec.payment_methods]
        names = [name for name in names if name]
        ids = [_text(value) for value in spec.payment_ids]
        ids = [value for value in ids if value]
        if not names and not ids:
            raise ApiError(
                f"binance: account {account.id} has no payment method configured for "
                f"{spec.pair.symbol}; a {spec.side} advertisement cannot be published without one"
            )
        entries: list[dict[str, Any]] = [{"identifier": value} for value in ids]
        if names:
            records = self._own_pay_methods(account, spec.pair)
            for name in names:
                record = self._find_pay_method(records, name)
                if record is None:
                    raise ApiError(
                        f"binance: account {account.id} has no payment method matching "
                        f"{name!r} for {spec.pair.symbol}"
                    )
                entries.append(self._trade_method_entry(account, record))
        return _dedupe(entries)

    def _trade_method_entry(self, account: Account, record: Mapping[str, str]) -> dict[str, Any]:
        """One buy-ad ``tradeMethods[]`` element: the method's venue ``identifier``."""
        label = record.get("name") or record.get("pay_id") or "?"
        identifier = record.get("identifier") or record.get("pay_type") or record.get("pay_id", "")
        if not identifier:
            raise ApiError(
                f"binance: payment method {label!r} of account {account.id} carries no "
                "identifier; it cannot be used on a buy advertisement"
            )
        return {"identifier": identifier}

    def _own_pay_methods(
        self, account: Account, pair: Pair
    ) -> tuple[Mapping[str, str], ...]:
        """The account's own payment methods, fetched once per ``(account, fiat)``."""
        cache_key = (account.id, pair.fiat.upper())
        cached = self._pay_methods_cache.get(cache_key)
        if cached is not None:
            return cached
        request = self._signed_request(account, "GET", PAY_METHODS_PATH)
        records = self._pay_method_records(self.send_private(account, request))
        self._pay_methods_cache[cache_key] = records
        return records

    @staticmethod
    def _pay_method_records(payload: Any) -> tuple[Mapping[str, str], ...]:
        """Normalize ``getPayMethodByUserId`` into ``{pay_id, name, pay_type, identifier}``."""
        data = payload.get("data") if isinstance(payload, Mapping) else payload
        if isinstance(data, Mapping):
            for key in ("items", "list", "rows", "payMethods"):
                candidate = data.get(key)
                if isinstance(candidate, list):
                    data = candidate
                    break
            else:
                data = [data] if data else []
        if not isinstance(data, list):
            raise ApiError(
                "binance payment-method payload was not a list of records", payload=payload
            )
        records: list[Mapping[str, str]] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            pay_id = _text(item.get("payId"))
            identifier = _text(item.get("identifier")) or _text(item.get("payMethodId"))
            pay_type = _text(item.get("payType")) or identifier or _text(item.get("payMethodId"))
            name = (
                _text(item.get("tradeMethodName"))
                or _text(item.get("payMethodName"))
                or _text(item.get("name"))
                or pay_type
                or pay_id
            )
            records.append(
                {"pay_id": pay_id, "name": name, "pay_type": pay_type, "identifier": identifier}
            )
        if not records:
            raise ApiError("binance reported no payment methods for the account", payload=payload)
        return tuple(records)

    @staticmethod
    def _find_pay_method(
        records: Iterable[Mapping[str, str]], requested: str
    ) -> Mapping[str, str] | None:
        """Exact case-insensitive match first, then a substring match either way."""
        wanted = requested.strip().casefold()
        if not wanted:  # pragma: no cover - the sole caller pre-filters blank method names
            return None
        exact: Mapping[str, str] | None = None
        loose: Mapping[str, str] | None = None
        for record in records:
            fields = [
                record.get(field, "")
                for field in ("name", "pay_type", "identifier", "pay_id")
            ]
            folded = [field.strip().casefold() for field in fields if field.strip()]
            if wanted in folded:
                exact = record
                break
            if loose is None and any(wanted in field or field in wanted for field in folded):
                loose = record
        return exact if exact is not None else loose

def _dedupe(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated ``tradeMethods`` entries, keeping the first occurrence."""
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[dict[str, Any]] = []
    for entry in entries:
        key = tuple(sorted((str(name), str(value)) for name, value in entry.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(entry))
    return unique
