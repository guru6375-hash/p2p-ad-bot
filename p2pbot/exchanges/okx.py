"""OKX P2P adapter: public competitor search + API-key advertisement management.

Public search (live-verified, no credentials)
---------------------------------------------
``GET https://www.okx.com/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin`` needs no cookie,
no CSRF token and no anti-bot header.  ``side`` names the *advertiser's* side, so
``side=SIDE_SELL`` ("we compete on the offers of advertisers who sell crypto") is passed
straight through; the envelope returns the requested rows in ``data.sell`` and leaves
``data.buy`` empty, so the parser reads whichever of the two arrays carries rows.

Advertiser class: the ``userType`` query parameter is echoed back and has **no** filtering
effect, so it is sent as ``all`` and the merchant tier is derived from ``creatorType`` /
``merchantId`` (see :func:`_advertiser_type`).

Metrics: OKX publishes only a *lifetime* completed-order count (``completedOrderQuantity``)
and a completion-rate fraction (``completedRate``); there is no rolling-30-day order count
and no completion-window metric, so ``month_order_count`` and ``month_finish_rate`` are left
``None`` and :data:`p2pbot.constants.OKX_FILTERS` (merchant tier only) is the filter that
applies.  Amounts, payment methods and the remaining fields are preserved verbatim in
``CompetitorAd.raw``.

Private advertisement management (API-key, merchant gated)
----------------------------------------------------------
Paths (research-verified: ``GET`` on the two write paths answers ``405``, i.e. they are
POST-only routes) against ``https://www.okx.com``:

* create -> ``POST /api/v5/p2p/ad/create``
* update -> ``POST /api/v5/p2p/ad/update``
* list own ads -> ``POST /api/v5/p2p/ad/list``

Authentication is the standard OKX v5 API-key scheme: ``OK-ACCESS-KEY``,
``OK-ACCESS-SIGN`` = base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body)),
``OK-ACCESS-TIMESTAMP`` (ISO-8601 UTC with milliseconds) and ``OK-ACCESS-PASSPHRASE``, with
``Content-Type: application/json``.  Signature inputs come from the injected clock
(:meth:`OkxAdapter.now`), never from :func:`datetime.now`, so a fixed clock reproduces a
fixed signature.  The signature covers ``json.dumps(body)`` -- byte-for-byte the bytes
:class:`~p2pbot.exchanges.base.UrllibTransport` serializes from ``json_body`` (``json.dumps``
defaults, insertion order preserved).  No request logs a key, secret or passphrase.

UNCONFIRMED -- the single place to correct (see also ``docs/research/okx.md`` §Open questions)
---------------------------------------------------------------------------------------------
OKX gates ``/api/v5/p2p/*`` behind Super/Diamond merchant whitelisting and publishes no
public reference for it, so the *shape* of the private requests could not be verified: no
merchant credentials exist on this box, and the API-guide page truncates before the P2P
sections.  What is unverified, and where it is centralized:

* wire names of the body fields -> :data:`AD_BODY_FIELDS` / :data:`CREATE_AD_FIELDS` /
  :data:`UPDATE_AD_FIELDS` / :data:`LIST_ADS_FIELDS`;
* the own-ad listing path and its pagination/target fields -> :data:`LIST_ADS_PATH` and
  ``LIST_ADS_FIELDS``;
* the field carrying the on/off state and its two values -> ``*_AD_FIELDS["status"]`` and
  :data:`AD_STATUS_VALUES`;
* the field naming an existing advertisement in an update -> ``UPDATE_AD_FIELDS["adv_no"]``;
* the id key echoed by create/update responses -> :data:`AD_ID_RESPONSE_FIELDS`;
* the per-asset quantity precision used by the derived quantity -> :data:`QUANTITY_STEP`.

Everything else (paths, envelope handling, signing, normalization) follows verified
evidence.  Correcting a wire name is a one-line edit in ``AD_BODY_FIELDS``; correcting which
fields a request carries is a one-line edit in that request's ``*_FIELDS`` selection.  The
values themselves are sent as strings because the public rows expose every money/rate field
as a JSON string; if OKX expects numbers, ``_wire_decimal`` is the single place to change.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, ClassVar, Mapping

from ..constants import SIDE_SELL
from ..errors import ApiError, ConfigError
from ..models import Account, AdActionResult, AdSpec, CompetitorAd, Pair, parse_decimal
from .base import ExchangeAdapter, HttpRequest

__all__ = [
    "OkxAdapter",
    "BASE_URL",
    "SEARCH_PATH",
    "CREATE_AD_PATH",
    "UPDATE_AD_PATH",
    "LIST_ADS_PATH",
    "AD_BODY_FIELDS",
    "CREATE_AD_FIELDS",
    "UPDATE_AD_FIELDS",
    "LIST_ADS_FIELDS",
    "AD_ID_RESPONSE_FIELDS",
    "AD_STATUS_VALUES",
    "QUANTITY_STEP",
]

_log = logging.getLogger(__name__)

#: Origin shared by the public search endpoint and the v5 API-key surface.
BASE_URL = "https://www.okx.com"

#: Public competitor search (live-verified, unauthenticated).
SEARCH_PATH = "/v3/c2c/tradingOrders/getMarketplaceAdsPrelogin"

#: Private write paths (live-verified to exist: ``GET`` answers ``405``).
CREATE_AD_PATH = "/api/v5/p2p/ad/create"
UPDATE_AD_PATH = "/api/v5/p2p/ad/update"
#: UNCONFIRMED path: every other candidate (``ad/list``, ``ad/my-ads``, ``ad/query-ads``,
#: …) answered ``404``; ``ad/list`` is the closest name to the documented ``ad/create`` /
#: ``ad/update`` siblings and is the target for the account's own advertisements.
LIST_ADS_PATH = "/api/v5/p2p/ad/list"

#: Envelope arrays of the public search; the requested side is populated, the other is empty.
_SEARCH_ARRAYS: tuple[str, ...] = ("sell", "buy")

#: ``creatorType`` values that always denote a P2P merchant (non-``common`` values always
#: carry a non-empty ``merchantId``; ``"common"`` rows always carry ``""``).
_MERCHANT_CREATOR_TYPES: frozenset[str] = frozenset({"diamond", "certified", "super"})
#: ``creatorType`` of a private/ordinary trader.
_COMMON_CREATOR_TYPE = "common"

#: The single on/off control (``AdSpec.active``) mapped onto OKX's state mechanism.
#: UNCONFIRMED values; candidates were ``"active"``/``"inactive"``, ``"on"``/``"off"``,
#: ``1``/``0``.  The *field name* lives in ``AD_BODY_FIELDS["status"]``.
AD_STATUS_VALUES: dict[bool, str] = {True: "active", False: "inactive"}

#: Quantization step of a quantity derived from ``max_amount / price`` when the spec carries
#: no quantity.  UNCONFIRMED (OKX's per-asset precision is not published).
QUANTITY_STEP = Decimal("0.00000001")

#: Logical field -> OKX wire name.  UNCONFIRMED: derived from the *public* row schema
#: (``cryptoCurrency``, ``fiatCurrency``, ``side``, ``price``, ``quoteMinAmountPerOrder``,
#: ``quoteMaxAmountPerOrder``, ``availableAmount``, ``paymentMethods``), which is the only
#: evidence available without merchant credentials.  This is the one place to fix a name.
AD_BODY_FIELDS: dict[str, str] = {
    "adv_no": "adId",  # UNCONFIRMED: candidate "advNo"/"id"
    "crypto": "cryptoCurrency",
    "fiat": "fiatCurrency",
    "side": "side",
    "price": "price",
    "min_amount": "quoteMinAmountPerOrder",
    "max_amount": "quoteMaxAmountPerOrder",
    "quantity": "availableAmount",
    "payment_methods": "paymentMethods",
    "status": "status",  # UNCONFIRMED: candidate "adStatus"/"state"/"online"
    "current_page": "currentPage",  # UNCONFIRMED
    "number_per_page": "numberPerPage",  # UNCONFIRMED
}

#: Fields published by ``POST /api/v5/p2p/ad/create`` (wire names from AD_BODY_FIELDS).
CREATE_AD_FIELDS: dict[str, str] = {
    key: AD_BODY_FIELDS[key]
    for key in (
        "crypto",
        "fiat",
        "side",
        "price",
        "min_amount",
        "max_amount",
        "quantity",
        "payment_methods",
        "status",
    )
}

#: Fields published by ``POST /api/v5/p2p/ad/update`` (the address plus the same payload).
UPDATE_AD_FIELDS: dict[str, str] = {
    key: AD_BODY_FIELDS[key]
    for key in (
        "adv_no",
        "crypto",
        "fiat",
        "side",
        "price",
        "min_amount",
        "max_amount",
        "quantity",
        "payment_methods",
        "status",
    )
}

#: Body of the own-ad listing request (UNCONFIRMED names, see AD_BODY_FIELDS).
LIST_ADS_FIELDS: dict[str, str] = {
    key: AD_BODY_FIELDS[key]
    for key in ("crypto", "fiat", "side", "current_page", "number_per_page")
}

#: Keys a create/update response may use to report the advertisement id.  UNCONFIRMED.
AD_ID_RESPONSE_FIELDS: tuple[str, ...] = ("adId", "advNo", "id")

#: Identity placeholders of a bare :meth:`OkxAdapter.parse_ad_response` result; the real
#: values are filled in by :meth:`ExchangeAdapter.parse_ad_result`.
_PLACEHOLDER_PAIR = Pair.parse("USD/USDT")

#: Rows of an own-ad listing may arrive as ``data`` itself or under a sub-object.
_LIST_ROW_KEYS: tuple[str, ...] = ("data", "list", "ads")


def _utc(moment: datetime) -> datetime:
    """``moment`` in UTC; a naive timestamp is taken as UTC (never as local time)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_timestamp(moment: datetime) -> str:
    """OKX ``OK-ACCESS-TIMESTAMP``: ISO-8601 UTC with millisecond precision."""
    value = _utc(moment)
    return f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond // 1000:03d}Z"


def _epoch_millis(moment: datetime) -> str:
    """Cache-buster ``t`` parameter, as integer epoch milliseconds (no float math)."""
    value = _utc(moment)
    seconds = calendar.timegm(value.utctimetuple())
    return str(seconds * 1000 + value.microsecond // 1000)


def _wire_decimal(value: Decimal) -> str:
    """Render a Decimal for the wire without scientific notation (``1E+2`` -> ``100``)."""
    return format(value, "f")


def _optional_decimal(value: Any) -> Decimal | None:
    """Parse a venue value, returning ``None`` when it is absent or not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return parse_decimal(value)
    except ConfigError:
        return None


def _fraction(value: Any) -> Decimal | None:
    """Normalize a venue rate to the ``0..1`` scale (``97.5`` -> ``0.975``).

    OKX reports ``completedRate`` as a fraction already; a value above ``1`` therefore has
    to be the percent scale.  Negative sentinels (``"-1"`` = "not available") stay ``None``.
    """
    rate = _optional_decimal(value)
    if rate is None or rate < 0:
        return None
    return rate / 100 if rate > 1 else rate


def _advertiser_type(creator_type: str, merchant_id: str) -> str:
    """Advertiser class for the ``OKX_FILTERS`` merchant check.

    Evidence (``docs/research/ground-truth-probes.md``): ``userType`` echoes the query and
    is useless as a flag, while ``creatorType`` non-``common`` values always carry a
    non-empty ``merchantId`` and every ``common`` row carries ``""``.  So an advertiser is
    a merchant when its ``creatorType`` is a merchant tier, or when it exposes a merchant id
    without declaring itself ``common``.
    """
    if creator_type in _MERCHANT_CREATOR_TYPES:
        return "merchant"
    if merchant_id and creator_type != _COMMON_CREATOR_TYPE:
        return "merchant"
    return "common"


def _first_value(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    """First non-empty value among ``keys``."""
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _first_text(record: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    """``str`` of the first non-empty value among ``keys``, else ``None``."""
    value = _first_value(record, keys)
    return None if value is None else str(value)


def _first_record(payload: Any) -> dict[str, Any]:
    """The venue record inside a v5 envelope (``{"code": "0", "data": [{...}]}``)."""
    data: Any = payload
    if isinstance(payload, Mapping) and "data" in payload:
        data = payload["data"]
    if isinstance(data, Mapping):
        return dict(data)
    if isinstance(data, (list, tuple)) and data and isinstance(data[0], Mapping):
        return dict(data[0])
    return {}


class OkxAdapter(ExchangeAdapter):
    """Adapter for OKX P2P (``platform = "okx"``).

    The public competitor search is unauthenticated; the private create/update/list calls
    sign with the account's ``API_KEY`` / ``SECRET_KEY`` / ``PASSPHRASE`` (never logged) and
    need no session bootstrap, so :meth:`build_login_request` returns ``None``.
    """

    platform: ClassVar[str] = "okx"

    #: Origin used by every builder; a subclass or test may point it at a mirror.
    base_url: ClassVar[str] = BASE_URL

    # -- public search -------------------------------------------------------------
    def build_search_request(
        self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20
    ) -> HttpRequest:
        """Competitor search request.

        ``side`` is the *advertiser's* side and is forwarded verbatim: ``side="sell"`` asks
        for the advertisers who sell crypto, i.e. the offers we compete against.  ``t`` is a
        cache-buster derived from the injected clock, so a fixed clock yields a fixed URL.
        """
        params = {
            "paymentMethod": "all",
            "side": str(side).strip().lower(),
            # Echoed back by the venue and without filtering effect; kept cosmetic.
            "userType": "all",
            "sortType": "price_asc",
            "limit": "100",
            "cryptoCurrency": pair.crypto,
            "fiatCurrency": pair.fiat,
            "currentPage": str(int(page)),
            "numberPerPage": str(int(rows)),
            "t": _epoch_millis(self.now()),
        }
        # No cookie, CSRF or anti-bot header is required (live-verified); the transport adds
        # the User-Agent/Accept pair.
        return HttpRequest(method="GET", url=f"{self.base_url}{SEARCH_PATH}", params=params)

    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]:
        """Normalize the search envelope; rows without a usable price are skipped."""
        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows: Any = ()
        if isinstance(data, Mapping):
            for name in _SEARCH_ARRAYS:
                candidate = data.get(name)
                if isinstance(candidate, (list, tuple)) and candidate:
                    rows = candidate
                    break
        if not isinstance(rows, (list, tuple)):  # pragma: no cover - rows is only ever a list/tuple
            return ()
        ads: list[CompetitorAd] = []
        for row in rows:
            ad = self._parse_ad_row(row, pair)
            if ad is not None:
                ads.append(ad)
        return tuple(ads)

    def ensure_success(self, payload: Any) -> None:
        """Raise :class:`ApiError` unless the envelope reports success (``code == 0``)."""
        if not isinstance(payload, Mapping):
            raise ApiError(
                f"okx returned an unexpected payload of type {type(payload).__name__}",
                payload=payload,
            )
        code = payload.get("code")
        error_code = payload.get("error_code", "0")
        if _is_zero(code) and _is_zero(error_code):
            return
        detail = _first_value(payload, ("error_message", "detailMsg", "msg"))
        message = f"okx API error (code={code!r}, error_code={error_code!r})"
        if detail is not None:
            message = f"{message}: {str(detail)[:300]}"
        raise ApiError(message, payload=payload)

    # -- private advertisement management ------------------------------------------
    def build_login_request(self, account: Account) -> HttpRequest | None:
        """``None``: the OKX API-key surface needs no session bootstrap."""
        return None

    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest:
        """List this account's own advertisements for ``pair``.

        The listing path and every field name here are UNCONFIRMED (see the module
        docstring); ``side`` is pinned to the side we advertise.
        """
        body: dict[str, Any] = {
            LIST_ADS_FIELDS["crypto"]: pair.crypto,
            LIST_ADS_FIELDS["fiat"]: pair.fiat,
            LIST_ADS_FIELDS["side"]: SIDE_SELL,
            LIST_ADS_FIELDS["current_page"]: 1,
            LIST_ADS_FIELDS["number_per_page"]: 100,
        }
        return self._signed_post(account, LIST_ADS_PATH, body)

    def build_create_ad_request(
        self, account: Account, spec: AdSpec, adv_no: str | None = None
    ) -> HttpRequest:
        """Publish ``spec`` as a new advertisement.

        ``adv_no`` is accepted for interface compatibility and ignored: a create always
        allocates a fresh advertisement id.  ``AdSpec.active`` travels in the body's status
        field (a disabled spec creates the ad in its off state), which for the publisher's
        create path is ``True``.
        """
        return self._signed_post(account, CREATE_AD_PATH, self._ad_body(spec, CREATE_AD_FIELDS))

    def build_update_ad_request(self, account: Account, spec: AdSpec, adv_no: str) -> HttpRequest:
        """Update the advertisement ``adv_no`` to ``spec`` (including its on/off state)."""
        body: dict[str, Any] = {UPDATE_AD_FIELDS["adv_no"]: str(adv_no)}
        body.update(self._ad_body(spec, UPDATE_AD_FIELDS))
        return self._signed_post(account, UPDATE_AD_PATH, body)

    def parse_ad_response(self, payload: Any) -> AdActionResult:
        """Read only what the create/update payload says (advertisement id, echoed price).

        Identity fields (platform/account/pair/created) stay placeholders; the inherited
        :meth:`ExchangeAdapter.parse_ad_result` completes them from the request context.
        """
        record = _first_record(payload)
        return AdActionResult(
            platform=self.platform,
            account_id="",
            pair=_PLACEHOLDER_PAIR,
            adv_no=_first_text(record, AD_ID_RESPONSE_FIELDS),
            # ``None`` when the venue does not echo a price: parse_ad_result then uses the
            # spec's price.  AdActionResult.price is not Optional, hence the cast comment.
            price=_optional_decimal(record.get("price")),  # type: ignore[arg-type]
            created=False,
            raw=record,
        )

    def parse_ad_list(self, payload: Any) -> tuple[Mapping[str, Any], ...]:
        """Extract the advertisement records from a v5 list envelope (rows under ``data``)."""
        for key in _LIST_ROW_KEYS:
            rows = payload.get(key) if isinstance(payload, Mapping) else None
            if isinstance(rows, (list, tuple)):
                return tuple(row for row in rows if isinstance(row, Mapping))
        return ()

    # -- internals -----------------------------------------------------------------
    def _parse_ad_row(self, row: Any, pair: Pair) -> CompetitorAd | None:
        """Normalize one public row, or ``None`` when it carries no usable price."""
        if not isinstance(row, Mapping):
            return None
        price = _optional_decimal(row.get("price"))
        if price is None:
            _log.debug("okx %s: skipping an advertisement without a price", pair.symbol)
            return None
        creator_type = str(row.get("creatorType") or "").strip().lower()
        merchant_id = str(row.get("merchantId") or "").strip()
        return CompetitorAd(
            platform=self.platform,
            pair=pair,
            price=price,
            advertiser=str(row.get("nickName") or ""),
            user_type=_advertiser_type(creator_type, merchant_id),
            # OKX publishes no rolling-30-day order count and no completion-window metric:
            # these two stay None (and a None metric fails OKX_FILTERS' checks closed).
            month_order_count=None,
            positive_rate=_fraction(row.get("completedRate")),
            month_finish_rate=None,
            adv_no=_first_text(row, ("id",)),
            raw=dict(row),
        )

    def _ad_body(self, spec: AdSpec, fields: Mapping[str, str]) -> dict[str, Any]:
        """The create/update payload of ``spec`` using this request's wire names."""
        quantity = spec.quantity if spec.quantity is not None else _default_quantity(spec)
        return {
            fields["crypto"]: spec.pair.crypto,
            fields["fiat"]: spec.pair.fiat,
            fields["side"]: str(spec.side or SIDE_SELL).strip().lower(),
            fields["price"]: _wire_decimal(spec.price),
            fields["min_amount"]: _wire_decimal(spec.min_amount),
            fields["max_amount"]: _wire_decimal(spec.max_amount),
            fields["quantity"]: _wire_decimal(quantity),
            fields["payment_methods"]: [
                str(item) for item in (spec.payment_ids or spec.payment_methods)
            ],
            fields["status"]: AD_STATUS_VALUES[bool(spec.active)],
        }

    def _signed_post(self, account: Account, path: str, body: Mapping[str, Any]) -> HttpRequest:
        """Sign and build a v5 ``POST``.

        The pre-hash is ``timestamp + "POST" + requestPath + body`` with ``body`` exactly the
        JSON text the transport will put on the wire: both sides call ``json.dumps`` with the
        default separators on the same insertion-ordered mapping, so the signed bytes and the
        sent bytes are identical.
        """
        payload = dict(body)
        serialized = json.dumps(payload)
        timestamp = _iso_timestamp(self.now())
        secret = account.require("SECRET_KEY")
        prehash = f"{timestamp}POST{path}{serialized}"
        signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        headers = {
            "OK-ACCESS-KEY": account.require("API_KEY"),
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": account.require("PASSPHRASE"),
            "Content-Type": "application/json",
        }
        return HttpRequest(
            method="POST",
            url=f"{self.base_url}{path}",
            json_body=payload,
            headers=headers,
        )


def _is_zero(value: Any) -> bool:
    """Whether an OKX status field reports success (``0`` / ``"0"`` / ``""``)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, Decimal)):
        return value == 0
    return isinstance(value, str) and value.strip() in ("", "0")


def _default_quantity(spec: AdSpec) -> Decimal:
    """Token amount covering the advertisement's largest order: ``max_amount / price``.

    ``AdSpec.quantity`` is optional and no venue default is published, so the fallback keeps
    the advertisement able to serve its own ``max_amount``, truncated to
    :data:`QUANTITY_STEP`.
    """
    if spec.price <= 0:
        raise ConfigError(
            f"cannot derive an advertisement quantity for {spec.pair.symbol}: "
            "the price must be positive"
        )
    return (spec.max_amount / spec.price).quantize(QUANTITY_STEP, rounding=ROUND_DOWN)
