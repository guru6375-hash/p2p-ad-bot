"""Bybit P2P adapter: public competitor search plus the signed V5 P2P ad-management API.

Public search
-------------
The requirement names the website route
``POST https://www.bybit.com/x-api/fiat/otc/item/recommend/online`` (:data:`WEB_SEARCH_URL`);
it is the class-level default of :attr:`BybitAdapter.search_url`, so an operator repoints it
in exactly one place.  The official documented equivalent is
``POST https://api.bybit.com/v5/p2p/item/online`` (:data:`DOCUMENTED_SEARCH_URL`), which
takes the same field *names*
(``tokenId``/``currencyId``/``side``/``page``/``size`` — documented as strings there).

**The website route is bot-protected and could not be exercised from this host.**  Evidence
(``docs/research/bybit.md``, ``docs/research/ground-truth-probes.md``): the architect
measured HTTP 403 (Akamai ``Access Denied``) for a POST carrying a desktop User-Agent, and
the investigator measured HTTP 404 for GET — Bybit answers 404 for a method mismatch, so the
route exists but refuses plain clients.  While writing this adapter the venue's own frontend
bundle (``/static/fiat-p2p/js/main.89788ca7.js``) was fetched: it does contain
``post(`${...}/fiat/otc/item/recommend/online`, {body: JSON.stringify(e), isBybit: true})``,
which corroborates the path but not the caller's field list (that chunk is behind the same
WAF).  :meth:`BybitAdapter.ensure_success` therefore treats a missing or non-zero
``ret_code`` as :class:`~p2pbot.errors.ApiError`, so a WAF block or an auth-required answer
fails loudly instead of silently yielding zero competitor ads.

Bybit pricing in this project never depends on that route: ``scenarios/pln.json`` resolves
``platforms.bybit.source = "copy:Binance"`` (SPEC §7 step 5), i.e. the Bybit advertisement
price is the Binance one, and the market parser never fetches Bybit (SPEC §8).

Private advertisement API (``POST https://api.bybit.com`` — :data:`API_BASE` — API-key HMAC)
-------------------------------------------------------------------------------------------
Credentials: ``API_KEY`` + ``SECRET_KEY`` (``.env`` ``BYBIT_1_API_KEY`` /
``BYBIT_1_SECRET_KEY``).  There is no session/CSRF bootstrap, so
:meth:`BybitAdapter.build_login_request` returns ``None``.

Signing (``X-BAPI-*`` headers, milliseconds since the epoch from the injected clock — never
``datetime.now()`` directly, so a fixed clock reproduces a fixed signature)::

    plain  = timestamp + api_key + recv_window + <literal request body string>
    X-BAPI-SIGN = HMAC-SHA256(secret, plain).hexdigest()        # lowercase hex
    headers = X-BAPI-API-KEY, X-BAPI-TIMESTAMP, X-BAPI-SIGN,
              X-BAPI-RECV-WINDOW ("5000"), Content-Type: application/json

The signature covers the **literal** body string, and
:class:`~p2pbot.exchanges.base.HttpRequest` cannot carry a pre-serialized body (its
``json_body`` is re-encoded by the transport).  The serializer is therefore pinned in
:data:`_JSON_DUMPS_OPTIONS` to exactly the options ``base.UrllibTransport`` uses
(``ensure_ascii=True`` and the separators ``", "`` / ``": "``) and every builder signs
:func:`_json_body`'s output while sending the same mapping as ``json_body``; the smoke for
this adapter captures the bytes the real transport writes and compares them to the signed
string (they are byte-identical).  If a future transport changes its encoding,
:data:`_JSON_DUMPS_OPTIONS` is the single place to follow it.

Endpoints
~~~~~~~~~
===========================  ==================================================
update (repricing)           ``POST /v5/p2p/item/update`` + ``actionType: "MODIFY"``
re-online                    ``POST /v5/p2p/item/update`` + ``actionType: "ACTIVE"``
pause / take the ad down     ``POST /v5/p2p/item/cancel`` ``{"itemId": adv_no}``
list own ads                 ``POST /v5/p2p/item/personal/list`` (``size`` ≤ 30)
===========================  ==================================================

``AdSpec.active`` mapping (Bybit has **no** offline/status-toggle endpoint — see
``docs/research/bybit.md`` §Open questions 4): ``active=True`` keeps the ad running and pushes the new money fields
(:data:`ACTION_MODIFY`); pass ``action_type=ACTION_ACTIVE`` to the same builder for the one
documented re-online action when the venue has taken the ad offline; ``active=False`` calls
``cancel`` (:meth:`BybitAdapter.build_cancel_ad_request`), which *removes* the advertisement
from Bybit instead of merely hiding it.

Normalization rules (see the task contract): ``user_type`` is lowercased
(``merchant``/``user``); ``positive_rate`` stays ``None`` because Bybit publishes no such
metric (the merchant's own "recent" figures are ``recentOrderNum`` / ``recentExecuteRate``,
which map to ``month_order_count`` / ``month_finish_rate``); rates are normalized to the
0..1 scale, and the venue's *counterparty requirements*
(``tradingPreferenceSet.orderFinishNumberDay30`` / ``completeRateDay30``) are deliberately
**not** used — they describe what a taker must have done, not the advertiser's own record.

UNCONFIRMED elements (never silently assumed)
----------------------------------------------
(The same list is exposed as :data:`UNCONFIRMED`; each item below also names the one place it
is centralized, so a correction is a one-line edit.)

1. **The website-route request body** (:meth:`build_search_request`): the field list
   ``userId / tokenId / currencyId / payment / side / size / page / action`` is the
   requirement's, not an observed request; ``size``/``page`` are sent as JSON *numbers* (the
   requirement's shape) while the documented V5 route specifies strings, and ``payment`` is
   always ``[]`` (a filter the adapter does not know how to populate for competitors).
2. **Response shape of the website route** (:meth:`parse_search_response`): the envelope
   ``ret_code``/``ret_msg``/``result{count, items[]}`` and the item fields
   (``price``/``minAmount``/``maxAmount``/``lastQuantity``/``payments``/``nickName``/
   ``userId``, all strings) come from the documented V5 sample; ``payments`` (an array of
   payment-type ids) is parsed only for ``raw`` — it cannot be mapped to display names
   without ``/v5/p2p/user/payment/list``, whose body is itself UNCONFIRMED.
3. **``paymentIds`` semantics** (:data:`DEFAULT_PAYMENT_IDS`): the documented sample value
   ``["-1"]`` is used when :attr:`~p2pbot.models.AdSpec.payment_ids` is empty; the meaning of
   ``-1`` (and of the ids in ad responses) is not documented.  Real ids must be supplied by
   the caller; the venue accepts at most five.
4. **``remark``, ``tradingPreferenceSet``, ``paymentPeriod``**: required on every update and
   replaced by what is sent, so an update echoes the ad's own values from ``item/info``
   (:data:`ECHOED_ITEM_FIELDS`). Whether ``item/update`` accepts the ``tradingPreferenceSet``
   object exactly as ``item/info`` returns it is not documented.
5. **``quantity`` on update** is the amount **left** on the ad (live-verified 2026-09-25:
   an update sent with the listing's total ``quantity`` reset ``lastQuantity`` to that total,
   adding back the executed amount). An edit that keeps the amount therefore sends
   ``lastQuantity`` (:attr:`~p2pbot.models.OwnAd.total_quantity` carries it for Bybit).
6. **``recentExecuteRate`` scale** (:func:`_normalize_rate`): the official sample shows ``0``,
   so the scale cannot be read off it; values above 1 are treated as percentages.
7. **Numeric P2P ``ret_code`` values** are not published; the adapter only distinguishes
   success (``0`` + ``OK``/``SUCCESS``/empty message) from failure and reports the venue's
   ``ret_msg``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, ClassVar

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
from .base import ExchangeAdapter, HttpRequest

__all__ = ["BybitAdapter"]

_log = logging.getLogger(__name__)

# -- public search ---------------------------------------------------------------------
#: Website route named by the requirement.  BOT-PROTECTED: 403 Akamai "Access Denied" for a
#: plain client (see the module docstring); :attr:`BybitAdapter.search_url` defaults to it.
WEB_SEARCH_URL = "https://www.bybit.com/x-api/fiat/otc/item/recommend/online"

#: Official documented equivalent (``POST /v5/p2p/item/online``), same field *names*.
DOCUMENTED_SEARCH_URL = "https://api.bybit.com/v5/p2p/item/online"

#: Value of the website route's ``action`` field (UNCONFIRMED, per the requirement).
SEARCH_ACTION = "recommend"

# -- private advertisement API ---------------------------------------------------------
#: V5 host; testnet is ``https://api-testnet.bybit.com`` (class-level, single switch).
API_BASE = "https://api.bybit.com"

UPDATE_PATH = "/v5/p2p/item/update"
CANCEL_PATH = "/v5/p2p/item/cancel"
LIST_PATH = "/v5/p2p/item/personal/list"
ITEM_INFO_PATH = "/v5/p2p/item/info"
#: Fields ``item/update`` requires and *replaces*: an update echoes the ad's own values.
ECHOED_ITEM_FIELDS: tuple[str, ...] = ("remark", "tradingPreferenceSet", "paymentPeriod")
#: The ``tradingPreferenceSet`` keys ``item/update`` documents, all typed as strings.
#: ``item/info`` answers with numbers and extra keys, which the update rejects (10001).
UPDATE_PREFERENCE_KEYS: tuple[str, ...] = (
    "hasUnPostAd",
    "isKyc",
    "isEmail",
    "isMobile",
    "hasRegisterTime",
    "registerTimeThreshold",
    "orderFinishNumberDay30",
    "completeRateDay30",
    "nationalLimit",
    "hasOrderFinishNumberDay30",
    "hasCompleteRateDay30",
    "hasNationalLimit",
)

#: Bybit encodes the *advertiser's* side: "1" = the advertiser sells, "0" = buys.
SIDE_CODE_SELL = "1"
SIDE_CODE_BUY = "0"

ACTION_MODIFY = "MODIFY"
ACTION_ACTIVE = "ACTIVE"
_ACTION_TYPES = (ACTION_MODIFY, ACTION_ACTIVE)

PRICE_TYPE_FIXED = "0"
#: ``priceType`` of a floating-price ad (priced by ``premium``); updates keep ads fixed.
PRICE_TYPE_FLOATING = "1"
PREMIUM_NONE = "0"
PAYMENT_PERIOD_MINUTES = "15"
LIST_PAGE_SIZE_MAX = 30
#: ``status`` of a listed own ad (``10`` online, ``20`` offline, ``30`` completed).
OWN_AD_STATUSES: dict[str, str] = {
    "10": AD_STATUS_ONLINE,
    "20": AD_STATUS_OFFLINE,
    "30": AD_STATUS_CLOSED,
}
#: ``side`` of a listed own ad (an int on reads) -> side.
OWN_AD_SIDES: dict[str, str] = {SIDE_CODE_SELL: SIDE_SELL, SIDE_CODE_BUY: SIDE_BUY}
PAYMENT_IDS_MAX = 5

#: UNCONFIRMED: ``["-1"]`` appears in Bybit's official post-ad example; its meaning (and how to
#: obtain real ids) is undocumented, so it is only used when the caller supplies none.
DEFAULT_PAYMENT_IDS: tuple[str, ...] = ("-1",)

#: ``X-BAPI-RECV-WINDOW`` in milliseconds (documented default).
RECV_WINDOW = "5000"

#: Points that cannot be verified without live Bybit credentials / a residential IP, with the
#: one place each is centralized in (the prose is the module docstring's "UNCONFIRMED
#: elements" section).  Nothing here is silently guessed.
UNCONFIRMED: tuple[str, ...] = (
    "website-route request body field list (userId/tokenId/currencyId/payment/side/size/"
    "page/action) and numeric size/page -> BybitAdapter.build_search_request",
    "website-route wrapper envelope + item/payments field types -> "
    "BybitAdapter.parse_search_response (uses the documented V5 sample shape)",
    "paymentIds semantics; the sample value ['-1'] -> DEFAULT_PAYMENT_IDS / "
    "BybitAdapter._payment_ids",
    "whether item/update accepts the tradingPreferenceSet object exactly as item/info "
    "returns it -> ECHOED_ITEM_FIELDS / build_update_ad_request",
    "the recentExecuteRate scale -> _normalize_rate",
    "numeric P2P ret_code values -> BybitAdapter.ensure_success (the venue ret_msg is "
    "reported verbatim, no code table is invented)",
)

#: ``json.dumps`` options for every signed body.  They are exactly the stdlib defaults that
#: :class:`~p2pbot.exchanges.base.UrllibTransport` applies to ``HttpRequest.json_body``, and
#: Bybit signs the literal body string, so signing and transmission must share this call.
_JSON_DUMPS_OPTIONS: dict[str, Any] = {"ensure_ascii": True, "separators": (", ", ": ")}

#: Identifies a field that is absent from a payload (``None`` is a legitimate value).
_MISSING = object()

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: Success is ``ret_code == 0`` with one of these (case-insensitive) messages: the P2P routes
#: answer ``SUCCESS``, the generic V5 routes ``OK``/empty (see docs/research/bybit.md).
_SUCCESS_MESSAGES = frozenset({"", "OK", "SUCCESS"})

_MERCHANT = "merchant"
_USER = "user"

#: Merchant/advertiser tags.  ``GA`` = General Advertiser, ``VA`` = Verified Advertiser,
#: ``BA`` = Block Advertiser (docs/research/bybit.md); ``userType == "ORG"`` is an
#: organisation account.  Any of them means "this row is an advertiser", which is exactly the
#: question the competitor filter asks, so they all count as ``merchant``; a row without a
#: tag and without ``ORG`` is a plain ``user``.
_ADVERTISER_AUTH_TAGS = frozenset({"GA", "VA", "BA"})
_ORGANISATION_USER_TYPE = "ORG"

#: Placeholder identity carried by :meth:`BybitAdapter.parse_ad_response`: no venue payload
#: says which account/pair a call belonged to.  ``ExchangeAdapter.parse_ad_result`` overwrites
#: ``platform``/``account_id``/``pair``/``price`` from the request context, so only ``adv_no``
#: and ``raw`` of that record are meaningful.
_UNKNOWN_PAIR = Pair(fiat="XXX", crypto="XXX")


# -- serialization and signing ---------------------------------------------------------
def _json_body(body: Mapping[str, Any]) -> str:
    """Serialize ``body`` for signing; byte-identical to what the transport transmits."""
    return json.dumps(body, **_JSON_DUMPS_OPTIONS)


def _sign(secret: str, timestamp: str, api_key: str, recv_window: str, body: str) -> str:
    """Lowercase hex HMAC-SHA256 over ``timestamp + api_key + recv_window + body``."""
    message = f"{timestamp}{api_key}{recv_window}{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _money(value: Decimal) -> str:
    """Render a :class:`Decimal` as a plain decimal string (never ``1E+3``)."""
    return format(value, "f")


def _side_code(side: str) -> str:
    """Bybit side code for a :data:`~p2pbot.constants.SIDE_SELL`/``SIDE_BUY`` value."""
    normalised = str(side).strip().lower()
    if normalised == SIDE_SELL:
        return SIDE_CODE_SELL
    if normalised == SIDE_BUY:
        return SIDE_CODE_BUY
    raise ConfigError(f"unknown side {side!r}; expected {SIDE_SELL!r} or {SIDE_BUY!r}")


def _as_sequence(value: Any) -> tuple[Any, ...]:
    """``value`` as a tuple of items; a scalar or ``None`` yields an empty tuple."""
    if isinstance(value, (str, bytes)) or value is None:
        return ()
    if isinstance(value, Iterable):
        return tuple(value)
    return ()


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """First value among ``keys`` that is present and not ``None``, else :data:`_MISSING`."""
    for key in keys:
        value = payload.get(key, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return _MISSING


def _result_mapping(payload: Any) -> Mapping[str, Any] | None:
    """The ``result`` object of a Bybit envelope, when it is a JSON object."""
    if not isinstance(payload, Mapping):
        return None
    result = payload.get("result")
    return result if isinstance(result, Mapping) else None


def _result_items(payload: Any) -> tuple[Mapping[str, Any], ...]:
    """``result.items`` of a Bybit envelope, as the JSON objects it actually contains."""
    result = _result_mapping(payload)
    items = result.get("items") if result is not None else None
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return ()
    return tuple(item for item in items if isinstance(item, Mapping))


def _optional_text(value: Any) -> str | None:
    """``value`` as a non-blank string, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _decimal(value: Any, field: str) -> Decimal | None:
    """Decimal for a venue value that may arrive as a JSON string or number.

    Used for money fields (documented as strings) and for the counters/rates Bybit returns
    as JSON numbers.  A float is converted through ``str()`` (the round-tripping decimal
    text) so a numeric rate survives; money fields never arrive as floats, and a float there
    is refused rather than silently rounded.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        value = repr(value)
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return parse_decimal(value, field)
    except ConfigError:
        _log.warning("bybit %s is not a decimal (%r); field ignored", field, value)
        return None


def _normalize_rate(value: Decimal | None) -> Decimal | None:
    """Normalize a venue rate to the 0..1 scale.

    UNCONFIRMED scale: Bybit reports no rate in the official sample (``recentExecuteRate`` is
    ``0``), so a value above ``1`` is read as a percentage (``97.5`` → ``0.975``) and a value
    at or below ``1`` is already a fraction (``0.95`` → ``0.95``).
    """
    if value is None:
        return None
    if value > 1:
        return value / 100
    return value


def _user_type(item: Mapping[str, Any]) -> str:
    """``merchant`` for an advertiser row, ``user`` for an ordinary account."""
    tags = {str(tag).strip().upper() for tag in _as_sequence(item.get("authTag"))}
    if tags & _ADVERTISER_AUTH_TAGS:
        return _MERCHANT
    if str(item.get("userType") or "").strip().upper() == _ORGANISATION_USER_TYPE:
        return _MERCHANT
    return _USER


class BybitAdapter(ExchangeAdapter):
    """Bybit P2P: public competitor search and the signed V5 advertisement API.

    All request builders are deterministic given ``(account, spec, now)``: the signature
    timestamp comes from the injected clock, so a fixed clock reproduces a fixed signature.
    No credential ever appears in a log line or an exception message.
    """

    platform: ClassVar[str] = "bybit"

    #: Public competitor-search endpoint.  Operator switch: point this at
    #: :data:`DOCUMENTED_SEARCH_URL` (or a proxy/regional host) without touching the builder.
    #: See the module docstring for why the default is bot-protected.
    search_url: ClassVar[str] = WEB_SEARCH_URL

    #: Private API host (``https://api-testnet.bybit.com`` for testnet).
    api_base: ClassVar[str] = API_BASE

    #: ``X-BAPI-RECV-WINDOW`` in milliseconds.
    recv_window: ClassVar[str] = RECV_WINDOW

    #: Default ``page``/``size`` of :meth:`build_list_ads_request` (venue maximum is 30).
    list_page_size: ClassVar[int] = LIST_PAGE_SIZE_MAX

    #: ``size`` of each :meth:`fetch_own_ads` page (the venue maximum).
    own_ads_page_size: ClassVar[int] = LIST_PAGE_SIZE_MAX

    # -- clock -------------------------------------------------------------------------
    def timestamp_ms(self) -> str:
        """Milliseconds since the epoch for the *injected* clock, as the venue wants it.

        Computed with integer arithmetic on the ``timedelta`` (no float round-tripping), and a
        naive clock is read as UTC so a test clock and the production clock agree.
        """
        now = self.now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        delta = now - _EPOCH
        milliseconds = delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
        return str(milliseconds)

    # -- public search -----------------------------------------------------------------
    def build_search_request(
        self, pair: Pair, *, side: str = SIDE_SELL, page: int = 1, rows: int = 20
    ) -> HttpRequest:
        """POST the requirement's website-route body for ``pair`` (UNCONFIRMED field list).

        ``side`` is mapped so that ``SIDE_SELL`` ("the advertiser sells", i.e. the ask side we
        compete on) becomes Bybit's ``"1"``.  ``size``/``page`` are sent as JSON numbers, the
        shape the requirement specifies; the documented V5 route documents them as strings.
        """
        body: dict[str, Any] = {
            "userId": "",
            "tokenId": pair.crypto,
            "currencyId": pair.fiat,
            "payment": [],
            "side": _side_code(side),
            "size": int(rows),
            "page": int(page),
            "action": SEARCH_ACTION,
        }
        return HttpRequest(
            method="POST",
            url=self.search_url,
            json_body=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    def parse_search_response(self, payload: Any, pair: Pair) -> tuple[CompetitorAd, ...]:
        """Normalize ``result.items[]`` into :class:`CompetitorAd` records.

        Every documented money field is a decimal *string*; a row without a usable price is
        skipped (a :class:`CompetitorAd` has no price-less form) and logged at ``WARNING``.
        ``payments`` is left in ``raw``: mapping payment-type ids to display names needs
        ``/v5/p2p/user/payment/list``, whose contract is UNCONFIRMED.
        """
        ads: list[CompetitorAd] = []
        for item in _result_items(payload):
            ad = self._parse_ad(item, pair)
            if ad is not None:
                ads.append(ad)
        return tuple(ads)

    def ensure_success(self, payload: Any) -> None:
        """Raise :class:`ApiError` unless ``payload`` is a Bybit success envelope.

        Accepts both spellings Bybit uses (``ret_code``/``ret_msg`` on the P2P routes,
        ``retCode``/``retMsg`` on the generic V5 routes).  A missing code is a failure: a WAF
        HTML page or an auth-required answer must never look like "zero advertisements".
        """
        if not isinstance(payload, Mapping):
            raise ApiError(
                "bybit answered without a JSON envelope "
                f"(HTTP body starts with {str(payload)[:120]!r})",
                payload=payload,
            )
        code = _first_present(payload, ("ret_code", "retCode"))
        message = _first_present(payload, ("ret_msg", "retMsg"))
        if code is _MISSING:
            raise ApiError(
                "bybit response carries no ret_code/retCode; treating it as a failure",
                payload=payload,
            )
        try:
            numeric = int(code)
        except (TypeError, ValueError) as exc:
            raise ApiError(f"bybit ret_code is not an integer: {code!r}", payload=payload) from exc
        text = "" if message is _MISSING else str(message)
        if numeric != 0:
            raise ApiError(
                f"bybit error {numeric}: {text.strip() or 'no ret_msg'}", payload=payload
            )
        if text.strip().upper() not in _SUCCESS_MESSAGES:
            raise ApiError(
                f"bybit returned ret_code 0 with unexpected ret_msg {text!r}", payload=payload
            )

    # -- private: session --------------------------------------------------------------
    def build_login_request(self, account: Account) -> HttpRequest | None:
        """``None``: Bybit authenticates every call with the API key, there is no session."""
        return None

    # -- private: own advertisements ---------------------------------------------------
    def build_list_ads_request(self, account: Account, pair: Pair) -> HttpRequest:
        """List this account's advertisements for ``pair`` (both sides, one venue page).

        ``page``/``size`` are documented as strings; ``size`` is capped at the venue maximum
        of 30.
        """
        body: dict[str, Any] = {
            "tokenId": pair.crypto,
            "currencyId": pair.fiat,
            "page": "1",
            "size": str(min(int(self.list_page_size), LIST_PAGE_SIZE_MAX)),
        }
        return self._private_request(account, LIST_PATH, body)

    def parse_ad_list(self, payload: Any) -> tuple[Mapping[str, Any], ...]:
        """``result.items`` of a ``personal/list`` payload (empty when there are none)."""
        return _result_items(payload)

    def build_own_ads_request(self, account: Account, *, page: int) -> HttpRequest:
        """One ``personal/list`` page with no token/currency/status filter: every own ad."""
        body = {
            "page": str(int(page)),
            "size": str(min(int(self.own_ads_page_size), LIST_PAGE_SIZE_MAX)),
        }
        return self._private_request(account, LIST_PATH, body)

    def parse_own_ad(self, row: Mapping[str, Any], account: Account) -> OwnAd | None:
        """Normalize one ``result.items[]`` row of the own-ad listing."""
        adv_no = _optional_text(row.get("id"))
        try:
            pair = Pair(
                fiat=_optional_text(row.get("currencyId")) or "",
                crypto=_optional_text(row.get("tokenId")) or "",
            )
        except ConfigError:
            pair = None
        if adv_no is None or pair is None:
            _log.debug("bybit %s: skipped an own-ad row without id/tokenId/currencyId", account.id)
            return None
        venue_status = _optional_text(row.get("status")) or ""
        payments = tuple(
            text for text in map(_optional_text, _as_sequence(row.get("payments"))) if text
        )
        return OwnAd(
            platform=self.platform,
            account_id=account.id,
            adv_no=adv_no,
            pair=pair,
            side=OWN_AD_SIDES.get(_optional_text(row.get("side")) or "", ""),
            status=OWN_AD_STATUSES.get(venue_status, AD_STATUS_UNKNOWN),
            price=_decimal(row.get("price"), "price"),
            min_amount=_decimal(row.get("minAmount"), "minAmount"),
            max_amount=_decimal(row.get("maxAmount"), "maxAmount"),
            quantity=_decimal(row.get("lastQuantity"), "lastQuantity"),
            payment_methods=payments,
            venue_status=venue_status,
            # item/update's quantity is the amount *left* on the ad (live-verified: sending the
            # listing's total ``quantity`` reset ``lastQuantity`` to it), so an edit that keeps
            # the amount must send ``lastQuantity``.
            total_quantity=_decimal(row.get("lastQuantity"), "lastQuantity"),
            price_floating_ratio=(
                _decimal(row.get("premium"), "premium")
                if _optional_text(row.get("priceType")) == PRICE_TYPE_FLOATING
                else None
            ),
            payment_ids=_payment_term_ids(row),
        )

    # -- private: update / re-online / pause -------------------------------------------
    def build_update_ad_request(
        self,
        account: Account,
        spec: AdSpec,
        adv_no: str,
        *,
        action_type: str = ACTION_MODIFY,
    ) -> HttpRequest:
        """Reprice (``MODIFY``) or re-list (``ACTIVE``) an existing advertisement.

        ``AdSpec.active is False`` short-circuits to :meth:`build_cancel_ad_request`: Bybit
        offers **no** offline/status-toggle endpoint, so "off" means the ad is taken down.
        ``active`` ``True`` with a known id is the ordinary repricing path (``MODIFY``); the
        only documented way back online is ``actionType: "ACTIVE"``
        (:data:`ACTION_ACTIVE`), which is what the caller passes for a re-list.

        Bybit requires ``remark``, ``tradingPreferenceSet``, ``paymentPeriod`` and ``quantity``
        on every update and *replaces* them with what is sent, so the ad's current item is
        fetched first (``POST /v5/p2p/item/info``) and :data:`ECHOED_ITEM_FIELDS` are sent back
        unchanged; only the spec's price, amounts, payment ids and quantity are written. Like
        Binance's update, this builder therefore performs I/O, and it refuses to build a body
        when the item lacks one of those fields rather than overwrite it with a default.
        """
        if not spec.active:
            return self.build_cancel_ad_request(account, adv_no)
        if spec.price_floating_ratio is not None:
            raise ConfigError("bybit: floating-price updates are not supported; pass a fixed price")
        resolved = str(action_type).strip().upper()
        if resolved not in _ACTION_TYPES:
            raise ConfigError(
                f"unknown bybit update action {action_type!r}; expected one of "
                f"{', '.join(_ACTION_TYPES)}"
            )
        item = self._fetch_item(account, _adv_no_or_raise(adv_no))
        body = {
            "id": _adv_no_or_raise(adv_no),
            "actionType": resolved,
            "priceType": PRICE_TYPE_FIXED,
            "premium": PREMIUM_NONE,
            "price": _money(spec.price),
            "minAmount": _money(spec.min_amount),
            "maxAmount": _money(spec.max_amount),
            "remark": str(item["remark"]),
            "tradingPreferenceSet": _update_preferences(item["tradingPreferenceSet"], adv_no),
            "paymentIds": list(self._update_payment_ids(spec, item, adv_no)),
            "quantity": _money(self._quantity(spec)),
            "paymentPeriod": str(item["paymentPeriod"]),
        }
        return self._private_request(account, UPDATE_PATH, body)

    def _update_payment_ids(
        self, spec: AdSpec, item: Mapping[str, Any], adv_no: str
    ) -> tuple[str, ...]:
        """The account's own payment-method ids: the spec's, else the ad's ``paymentTerms``.

        ``item/update`` wants the ids of the account's payment methods (``paymentTerms[].id``),
        not the payment *type* ids of ``payments`` (those fail with ``912300013``). An ad that
        reports none is refused rather than sent the documented sample ``["-1"]``, which would
        replace its payment methods.
        """
        if spec.payment_ids:
            return self._payment_ids(spec)
        ids = _payment_term_ids(item)
        if not ids:
            raise ApiError(
                f"bybit advertisement {adv_no} reports no payment methods (paymentTerms); "
                "refusing to replace them"
            )
        if len(ids) > PAYMENT_IDS_MAX:
            raise ConfigError(f"bybit accepts at most {PAYMENT_IDS_MAX} payment ids, got {len(ids)}")
        return ids

    def _fetch_item(self, account: Account, adv_no: str) -> Mapping[str, Any]:
        """The ad's current ``item/info`` result, checked to carry :data:`ECHOED_ITEM_FIELDS`."""
        payload = self.send_private(
            account, self._private_request(account, ITEM_INFO_PATH, {"itemId": adv_no})
        )
        item = _result_mapping(payload)
        if item is None:
            raise ApiError(
                f"bybit advertisement {adv_no} of account {account.id} has no detail object",
                payload=payload,
            )
        missing = [
            name
            for name in ECHOED_ITEM_FIELDS
            if item.get(name) is None
            or (name == "tradingPreferenceSet" and not isinstance(item.get(name), Mapping))
        ]
        if missing:
            raise ApiError(
                f"bybit advertisement {adv_no} of account {account.id} lacks {', '.join(missing)}; "
                "refusing to overwrite them with defaults",
                payload=payload,
            )
        return item

    def build_cancel_ad_request(self, account: Account, adv_no: str) -> HttpRequest:
        """Take an advertisement down (``POST /v5/p2p/item/cancel``).

        Bybit has no offline endpoint: cancelling *removes* the advertisement, so a later
        "resume" needs the re-online action (or a new ad made on Bybit when the id is gone).  This is
        the request ``AdSpec.active is False`` maps to.
        """
        body = {"itemId": _adv_no_or_raise(adv_no)}
        return self._private_request(account, CANCEL_PATH, body)

    # -- private: response -------------------------------------------------------------
    def parse_ad_response(self, payload: Any) -> AdActionResult:
        """``adv_no`` (``result.itemId`` when echoed) and ``raw``; nothing else is said.

        ``parse_ad_result`` (base) fills platform/account/pair/price from the request context,
        so the identity fields here are placeholders and ``price`` is ``None``: the update
        and cancel payloads echo no price.
        """
        result = _result_mapping(payload)
        adv_no = _optional_text(result.get("itemId")) if result is not None else None
        return AdActionResult(
            platform=self.platform,
            account_id="",
            pair=_UNKNOWN_PAIR,
            adv_no=adv_no,
            price=None,  # type: ignore[arg-type]  # base fills spec.price when it is None
            raw=payload if isinstance(payload, Mapping) else {},
        )

    # -- spec helpers ------------------------------------------------------------------
    def _payment_ids(self, spec: AdSpec) -> tuple[str, ...]:
        """``spec.payment_ids`` when present, else the documented sample ``["-1"]``.

        UNCONFIRMED: Bybit's documentation only shows ``["-1"]``; real ids come from
        ``/v5/p2p/user/payment/list`` (not implemented — its body is not documented).
        """
        ids = tuple(str(value).strip() for value in spec.payment_ids if str(value).strip())
        if not ids:
            _log.debug(
                "bybit %s: no payment ids supplied; using the documented sample value %s",
                spec.pair.symbol,
                list(DEFAULT_PAYMENT_IDS),
            )
            ids = DEFAULT_PAYMENT_IDS
        if len(ids) > PAYMENT_IDS_MAX:
            raise ConfigError(
                f"bybit accepts at most {PAYMENT_IDS_MAX} payment ids, got {len(ids)}"
            )
        return ids

    def _quantity(self, spec: AdSpec) -> Decimal:
        """Token amount of the ad: ``spec.quantity``, else ``floor(max_amount / price)``."""
        if spec.quantity is not None:
            return spec.quantity
        if spec.price <= 0:
            raise ConfigError(
                f"cannot derive a quantity for {spec.pair.symbol}: price must be positive, "
                f"got {spec.price}"
            )
        quantity = (spec.max_amount / spec.price).to_integral_value(rounding=ROUND_DOWN)
        if quantity <= 0:
            raise ConfigError(
                f"cannot derive a quantity for {spec.pair.symbol}: max_amount "
                f"{spec.max_amount} is below the price {spec.price}"
            )
        return quantity

    # -- internals ---------------------------------------------------------------------
    def _private_request(self, account: Account, path: str, body: Mapping[str, Any]) -> HttpRequest:
        """Signed ``POST`` to ``path``; the same body mapping is both signed and sent."""
        api_key = account.require("API_KEY")
        secret = account.require("SECRET_KEY")
        timestamp = self.timestamp_ms()
        body_string = _json_body(body)
        return HttpRequest(
            method="POST",
            url=f"{self.api_base}{path}",
            json_body=body,
            headers={
                "X-BAPI-API-KEY": api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-SIGN": _sign(secret, timestamp, api_key, self.recv_window, body_string),
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "Content-Type": "application/json",
            },
        )

    def _parse_ad(self, item: Mapping[str, Any], pair: Pair) -> CompetitorAd | None:
        """One ``result.items[]`` row, or ``None`` when the row carries no usable price."""
        price = _decimal(item.get("price"), "price")
        if price is None:
            _log.warning(
                "bybit ad %s for %s has no usable price; skipped",
                _optional_text(item.get("id")) or "-",
                pair.symbol,
            )
            return None
        return CompetitorAd(
            platform=self.platform,
            pair=pair,
            price=price,
            advertiser=_optional_text(item.get("nickName"))
            or _optional_text(item.get("userId"))
            or "",
            user_type=_user_type(item),
            # The merchant's own "recent" figures; orderNum/finishNum are all-time counts and
            # tradingPreferenceSet.orderFinishNumberDay30 is a counterparty requirement.
            month_order_count=_decimal(item.get("recentOrderNum"), "recentOrderNum"),
            # Bybit publishes no "positive rate" (the Binance-only metric) — stays None.
            positive_rate=None,
            month_finish_rate=_normalize_rate(
                _decimal(item.get("recentExecuteRate"), "recentExecuteRate")
            ),
            adv_no=_optional_text(item.get("id")),
            raw=item,
        )


def _payment_term_ids(item: Mapping[str, Any]) -> tuple[str, ...]:
    """``paymentTerms[].id`` of an ad: the account's own payment-method ids."""
    ids = (
        _optional_text(term.get("id"))
        for term in _as_sequence(item.get("paymentTerms"))
        if isinstance(term, Mapping)
    )
    return tuple(value for value in ids if value)


def _update_preferences(preferences: Mapping[str, Any], adv_no: str) -> dict[str, str]:
    """The live ``tradingPreferenceSet`` in the shape ``item/update`` accepts.

    Documented keys keep their value, as strings (``0`` -> ``"0"``). An undocumented key
    that holds a real setting (anything but ``0``/``""``/``None``) cannot be sent back, so
    the update is refused instead of silently dropping that requirement.
    """
    for key, value in preferences.items():
        if key not in UPDATE_PREFERENCE_KEYS and value not in (0, "0", "", None):
            raise ConfigError(
                f"bybit advertisement {adv_no} has trading preference {key}={value!r}, which "
                "item/update cannot carry; refusing to drop it"
            )
    return {
        key: "" if preferences[key] is None else str(preferences[key])
        for key in UPDATE_PREFERENCE_KEYS
        if key in preferences
    }


def _adv_no_or_raise(adv_no: str) -> str:
    """The advertisement id a private write needs, or a loud :class:`ConfigError`."""
    text = _optional_text(adv_no)
    if text is None:
        raise ConfigError("bybit needs the advertisement id (itemId) to update or cancel an ad")
    return text
