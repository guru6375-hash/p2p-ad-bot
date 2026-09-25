"""``p2pbot/exchanges/okx.py``: public marketplace search + the v5 API-key ad surface.

The public fixtures are the real captures of ``GET /v3/c2c/tradingOrders/getMarketplaceAdsPrelogin``
from ``docs/research/okx.md``. The private update body *field names* are UNCONFIRMED
(no merchant credentials exist), so those tests assert the shape the adapter produces - the
fields it selects, the values derived from the ``AdSpec`` and the signing scheme - and never
claim that OKX would accept the names.

OKX's own body-field names are centralized in the module's ``*_FIELDS`` mappings; the literal
names asserted below are the current values of those mappings.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from p2pbot.errors import ApiError, ConfigError, TransportError
from p2pbot.exchanges.base import HttpRequest
from p2pbot.exchanges.okx import (
    AD_BODY_FIELDS,
    AD_STATUS_VALUES,
    BASE_URL,
    LIST_ADS_FIELDS,
    LIST_ADS_PATH,
    OWN_ADS_FIELDS,
    SEARCH_PATH,
    UPDATE_AD_FIELDS,
    UPDATE_AD_PATH,
    OkxAdapter,
)
from p2pbot.models import Pair

from _fake_transport import (
    FIXED_NOW_ISO,
    FIXED_NOW_MS,
    FakeTransport,
    FixedClock,
    json_response,
    make_account,
    make_spec,
    text_response,
)

UAH_USDT = Pair.parse("UAH/USDT")
ACCOUNT = make_account("okx", 1, {"API_KEY": "okx-key", "SECRET_KEY": "okx-secret", "PASSPHRASE": "okx-passphrase"})

#: Real trimmed UAH ``side=sell`` capture (``docs/research/okx.md``).
UAH_SELL_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "buy": [],
        "sell": [
            {
                "availableAmount": "5399.28",
                "completedOrderQuantity": 680,
                "completedRate": "0.9483",
                "creatorType": "diamond",
                "id": "260924231439195",
                "merchantId": "a5e7627aea",
                "nickName": "Anticorupcioner",
                "paymentMethods": [
                    "Oschad bank (CARD)",
                    "PUMB (CARD)",
                    "Monobank (Card)",
                    "PrivatBank (CARD)",
                ],
                "paymentTimeoutMinutes": 15,
                "posReviewPercentage": "-1",
                "price": "46.96",
                "publicUserId": "8be31f0944",
                "quoteCurrency": "uah",
                "quoteMaxAmountPerOrder": "253550.18",
                "quoteMinAmountPerOrder": "3999.00",
                "quoteScale": 2,
                "quoteSymbol": "₴",
                "side": "sell",
                "userType": "all",
                "badgeInfo": {"badgeList": [{"badgeId": -1000, "title": "Diamond Merchant", "type": 1}]},
            },
            {
                "availableAmount": "1200.00",
                "completedOrderQuantity": 12,
                "completedRate": "0.5",
                "creatorType": "common",
                "id": "260924231439196",
                "merchantId": "",
                "nickName": "ordinary-trader",
                "paymentMethods": ["PrivatBank (CARD)"],
                "price": "46.10",
                "side": "sell",
                "userType": "all",
            },
        ],
        "total": 100,
    },
    "detailMsg": "",
    "error_code": "0",
    "error_message": "",
    "msg": "",
    "requestId": "3121002658036220002",
}

#: Real trimmed PLN ``side=buy`` capture - the mirror-array behaviour.
PLN_BUY_PAYLOAD: dict[str, Any] = {
    "code": 0,
    "data": {
        "buy": [
            {
                "availableAmount": "94558.72",
                "completedOrderQuantity": 115,
                "completedRate": "0.9426",
                "creatorType": "certified",
                "id": "260924234850818",
                "merchantId": "01fdc4bf15",
                "nickName": "arxfatalis",
                "paymentMethods": ["bank", "Bank Pekao", "PKO Bank", "BLIK", "Santander"],
                "price": "3.79",
                "quoteMaxAmountPerOrder": "8000.00",
                "quoteMinAmountPerOrder": "300.00",
                "quoteSymbol": "zł",
                "side": "buy",
                "userType": "common",
            }
        ],
        "sell": [],
        "total": 88,
    },
    "error_code": "0",
    "msg": "",
    "requestId": "3130802658075430012",
}


def make_okx(*scripted: Any) -> OkxAdapter:
    return OkxAdapter(FakeTransport(*scripted), now=FixedClock())


def assert_okx_signature(request: HttpRequest, secret: str = "okx-secret") -> None:
    """Independent recomputation of ``base64(HMAC-SHA256(ts + "POST" + path + body))``."""
    body = json.dumps(request.json_body)
    path = urllib.parse.urlsplit(request.url).path
    timestamp = request.headers["OK-ACCESS-TIMESTAMP"]
    prehash = f"{timestamp}POST{path}{body}"
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    assert request.headers["OK-ACCESS-SIGN"] == expected
    # The signed bytes are exactly what ``UrllibTransport`` serializes from ``json_body``.
    assert body == json.dumps(request.json_body)


# --------------------------------------------------------------------------------------
# public search
# --------------------------------------------------------------------------------------
def test_search_request_carries_the_documented_query_parameters() -> None:
    request = make_okx().build_search_request(UAH_USDT, side="sell", page=2, rows=5)

    assert request.method == "GET"
    assert request.url == f"{BASE_URL}{SEARCH_PATH}"
    assert request.json_body is None
    assert request.headers == {}
    assert dict(request.params) == {
        "paymentMethod": "all",
        "side": "sell",
        "userType": "all",
        "sortType": "price_asc",
        "limit": "100",
        "cryptoCurrency": "USDT",
        "fiatCurrency": "UAH",
        "currentPage": "2",
        "numberPerPage": "5",
        "t": str(FIXED_NOW_MS),
    }


@pytest.mark.parametrize(("side", "expected"), [("sell", "sell"), ("SELL", "sell"), ("buy", "buy")])
def test_search_request_forwards_the_advertisers_side_verbatim(side: str, expected: str) -> None:
    request = make_okx().build_search_request(UAH_USDT, side=side)

    assert request.params["side"] == expected


def test_parse_search_response_normalizes_the_uah_sell_capture() -> None:
    ads = make_okx().parse_search_response(UAH_SELL_PAYLOAD, UAH_USDT)

    assert len(ads) == 2
    merchant, ordinary = ads
    assert merchant.platform == "okx"
    assert merchant.pair is UAH_USDT
    assert merchant.price == Decimal("46.96")
    assert merchant.advertiser == "Anticorupcioner"
    assert merchant.user_type == "merchant"  # creatorType "diamond" (userType says "all")
    assert merchant.positive_rate == Decimal("0.9483")
    # OKX publishes neither a rolling-30-day order count nor a completion-window metric.
    assert merchant.month_order_count is None
    assert merchant.month_finish_rate is None
    assert merchant.adv_no == "260924231439195"
    assert merchant.raw["paymentMethods"] == [
        "Oschad bank (CARD)",
        "PUMB (CARD)",
        "Monobank (Card)",
        "PrivatBank (CARD)",
    ]
    assert merchant.raw["completedOrderQuantity"] == 680
    assert merchant.raw["badgeInfo"]["badgeList"][0]["title"] == "Diamond Merchant"

    assert ordinary.advertiser == "ordinary-trader"
    assert ordinary.user_type == "common"
    assert ordinary.positive_rate == Decimal("0.5")


def test_parse_search_response_reads_the_mirrored_array_when_the_requested_one_is_empty() -> None:
    ads = make_okx().parse_search_response(PLN_BUY_PAYLOAD, Pair.parse("PLN/USDT"))

    assert len(ads) == 1
    assert ads[0].price == Decimal("3.79")
    assert ads[0].user_type == "merchant"  # creatorType "certified"
    assert ads[0].positive_rate == Decimal("0.9426")
    assert ads[0].raw["side"] == "buy"


def test_parse_search_response_prefers_the_sell_array_when_both_carry_rows() -> None:
    payload = {
        "code": 0,
        "data": {"sell": [{"price": "46.96", "creatorType": "diamond"}], "buy": [{"price": "44.00"}]},
    }

    ads = make_okx().parse_search_response(payload, UAH_USDT)

    assert [ad.price for ad in ads] == [Decimal("46.96")]


@pytest.mark.parametrize(
    ("creator_type", "merchant_id", "expected"),
    [
        ("diamond", "a5e7627aea", "merchant"),
        ("certified", "01fdc4bf15", "merchant"),
        ("super", "", "merchant"),
        ("common", "", "common"),
        ("DIAMOND", "x", "merchant"),  # normalized to lower case
        ("", "abc123", "merchant"),  # a merchant id without a declared "common" tier
        ("", "", "common"),
        ("unknown", "", "common"),
    ],
)
def test_advertiser_class_comes_from_creator_type_and_merchant_id(
    creator_type: str, merchant_id: str, expected: str
) -> None:
    payload = {
        "code": 0,
        "data": {
            "sell": [
                {
                    "price": "46.96",
                    "creatorType": creator_type,
                    "merchantId": merchant_id,
                    "userType": "all",
                    "nickName": "N",
                }
            ]
        },
    }

    ad = make_okx().parse_search_response(payload, UAH_USDT)[0]

    assert ad.user_type == expected


@pytest.mark.parametrize(
    ("completed_rate", "expected"),
    [
        ("0.9483", "0.9483"),
        ("0.5", "0.5"),
        (0, "0"),
        ("97.5", "0.975"),  # a percentage is normalized to the 0..1 scale
        ("-1", None),  # documented "not available" sentinel
        ("", None),
        (None, None),
        ("n/a", None),
    ],
)
def test_completed_rate_is_normalized_to_the_fraction_scale(
    completed_rate: Any, expected: str | None
) -> None:
    payload = {"code": 0, "data": {"sell": [{"price": "46.96", "completedRate": completed_rate}]}}

    ad = make_okx().parse_search_response(payload, UAH_USDT)[0]

    assert ad.positive_rate == (None if expected is None else Decimal(expected))


@pytest.mark.parametrize(
    "row",
    [
        "not-an-object",
        {"completedRate": "0.9"},  # no price
        {"price": ""},
        {"price": None},
        {"price": "46,96"},  # a malformed venue value is skipped, never raised
    ],
)
def test_rows_without_a_usable_price_are_skipped(row: Any) -> None:
    payload = {"code": 0, "data": {"sell": [row]}}

    assert make_okx().parse_search_response(payload, UAH_USDT) == ()


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        {"code": 0},
        {"code": 0, "data": "nope"},
        {"code": 0, "data": {"sell": "nope"}},
        {"code": 0, "data": {"sell": [], "buy": []}},
    ],
)
def test_parse_search_response_returns_nothing_for_an_unusable_envelope(payload: Any) -> None:
    assert make_okx().parse_search_response(payload, UAH_USDT) == ()


# --------------------------------------------------------------------------------------
# public search: envelope failures
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "error_code": "0"},
        {"code": "0", "error_code": "0"},
        {"code": 0},  # error_code defaults to "0"
        {"code": 0, "error_code": ""},
        {"code": 0, "error_code": 0},
    ],
)
def test_ensure_success_accepts_the_zero_status_shapes(payload: Any) -> None:
    assert make_okx().ensure_success(payload) is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"code": 1}, "okx API error (code=1, error_code='0')"),
        (
            {"code": 0, "error_code": "51000", "error_message": "Parameter adId error"},
            "okx API error (code=0, error_code='51000'): Parameter adId error",
        ),
        (
            {"code": 0, "error_code": "51001", "detailMsg": "Merchant not found"},
            "okx API error (code=0, error_code='51001'): Merchant not found",
        ),
        ({"code": 0, "error_code": "1", "msg": "boom"}, "okx API error (code=0, error_code='1'): boom"),
        ({"code": True}, "okx API error (code=True, error_code='0')"),
        ({"code": None}, "okx API error (code=None, error_code='0')"),
    ],
)
def test_ensure_success_reports_the_venue_error(payload: Any, message: str) -> None:
    with pytest.raises(ApiError) as excinfo:
        make_okx().ensure_success(payload)

    assert str(excinfo.value) == message
    assert excinfo.value.payload == payload


def test_ensure_success_rejects_a_non_object_payload() -> None:
    with pytest.raises(ApiError) as excinfo:
        make_okx().ensure_success("<html>Access Denied</html>")

    assert str(excinfo.value) == "okx returned an unexpected payload of type str"
    assert excinfo.value.payload == "<html>Access Denied</html>"


def test_search_ads_end_to_end_applies_the_merchant_only_filter() -> None:
    adapter = make_okx(json_response(UAH_SELL_PAYLOAD))

    snapshot = adapter.search_ads(UAH_USDT)

    assert adapter.transport.last.params["side"] == "sell"
    assert [ad.price for ad in snapshot.ads] == [Decimal("46.96"), Decimal("46.10")]
    assert [ad.advertiser for ad in snapshot.filtered] == ["Anticorupcioner"]
    assert snapshot.middle == Decimal("46.96")


def test_search_ads_propagates_a_connection_failure_as_transport_error() -> None:
    adapter = make_okx(TransportError("GET https://www.okx.com/v3/c2c/... failed: dns"))

    with pytest.raises(TransportError):
        adapter.search_ads(UAH_USDT)


def test_search_ads_raises_with_the_status_and_payload_on_an_http_failure() -> None:
    adapter = make_okx(json_response({"code": "50103", "msg": "header missing"}, status=401))

    with pytest.raises(ApiError) as excinfo:
        adapter.search_ads(UAH_USDT)

    assert excinfo.value.status == 401
    assert excinfo.value.payload == {"code": "50103", "msg": "header missing"}
    assert "okx search for UAH/USDT failed with HTTP 401" in str(excinfo.value)


# --------------------------------------------------------------------------------------
# private requests
# --------------------------------------------------------------------------------------
def test_build_login_request_is_none_because_okx_signs_every_call() -> None:
    assert make_okx().build_login_request(ACCOUNT) is None


def test_list_ads_request_uses_the_listing_fields_and_signs_them() -> None:
    request = make_okx().build_list_ads_request(ACCOUNT, UAH_USDT)

    assert request.method == "POST"
    assert request.url == f"{BASE_URL}{LIST_ADS_PATH}"
    assert request.headers["OK-ACCESS-KEY"] == "okx-key"
    assert request.headers["OK-ACCESS-PASSPHRASE"] == "okx-passphrase"
    assert request.headers["OK-ACCESS-TIMESTAMP"] == FIXED_NOW_ISO
    assert request.headers["Content-Type"] == "application/json"
    assert set(request.json_body) == set(LIST_ADS_FIELDS.values())
    assert request.json_body == {
        AD_BODY_FIELDS["crypto"]: "USDT",
        AD_BODY_FIELDS["fiat"]: "UAH",
        AD_BODY_FIELDS["side"]: "sell",
        AD_BODY_FIELDS["current_page"]: 1,
        AD_BODY_FIELDS["number_per_page"]: 100,
    }
    assert_okx_signature(request)


def test_update_ad_request_derives_the_quantity_and_sends_the_method_names() -> None:
    spec = make_spec(
        price="47.00",
        min_amount="1000.00",
        max_amount="47000.00",
        payment_methods=("PrivatBank (CARD)",),
    )

    body = make_okx().build_update_ad_request(ACCOUNT, spec, "1").json_body

    # floor(max_amount / price) at QUANTITY_STEP
    assert body[AD_BODY_FIELDS["quantity"]] == "1000.00000000"
    assert body[AD_BODY_FIELDS["payment_methods"]] == ["PrivatBank (CARD)"]
    assert body[AD_BODY_FIELDS["side"]] == "sell"


def test_update_ad_request_uses_the_explicit_quantity_and_never_scientific_notation() -> None:
    spec = make_spec(price="1E+2", min_amount="100", max_amount="10000", quantity="3.5", payment_methods=("BLIK",))

    body = make_okx().build_update_ad_request(ACCOUNT, spec, "1").json_body

    assert body[AD_BODY_FIELDS["price"]] == "100"
    assert body[AD_BODY_FIELDS["quantity"]] == "3.5"


def test_update_ad_request_prefers_payment_ids_and_normalizes_the_side() -> None:
    spec = make_spec(
        side="SELL",
        payment_methods=("PrivatBank (CARD)",),
        payment_ids=("pm-1", "pm-2"),
    )

    body = make_okx().build_update_ad_request(ACCOUNT, spec, "1").json_body

    assert body[AD_BODY_FIELDS["side"]] == "sell"
    assert body[AD_BODY_FIELDS["payment_methods"]] == ["pm-1", "pm-2"]


@pytest.mark.parametrize(("active", "expected"), [(True, "active"), (False, "inactive")])
def test_update_ad_request_carries_the_on_off_state(active: bool, expected: str) -> None:
    body = make_okx().build_update_ad_request(ACCOUNT, make_spec(active=active), "1").json_body

    assert body[AD_BODY_FIELDS["status"]] == expected


def test_update_ad_request_refuses_a_derived_quantity_without_a_positive_price() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_okx().build_update_ad_request(ACCOUNT, make_spec(price="0"), "1")

    assert str(excinfo.value) == (
        "cannot derive an advertisement quantity for UAH/USDT: the price must be positive"
    )


def test_update_ad_request_carries_the_address_and_the_same_payload() -> None:
    spec = make_spec(price="47.25", min_amount="900", max_amount="40000", quantity="2", active=False)

    request = make_okx().build_update_ad_request(ACCOUNT, spec, "260924231439195")

    assert request.url == f"{BASE_URL}{UPDATE_AD_PATH}"
    body = request.json_body
    assert set(body) == set(UPDATE_AD_FIELDS.values())
    assert body[AD_BODY_FIELDS["adv_no"]] == "260924231439195"
    assert body[AD_BODY_FIELDS["price"]] == "47.25"
    assert body[AD_BODY_FIELDS["min_amount"]] == "900"
    assert body[AD_BODY_FIELDS["max_amount"]] == "40000"
    assert body[AD_BODY_FIELDS["quantity"]] == "2"
    assert body[AD_BODY_FIELDS["status"]] == "inactive"
    assert_okx_signature(request)


def test_a_naive_clock_is_read_as_utc_and_an_aware_clock_is_converted() -> None:
    naive = OkxAdapter(FakeTransport(), now=FixedClock(datetime(2026, 9, 24, 12, 0, 0, 123_000)))

    request = naive.build_list_ads_request(ACCOUNT, UAH_USDT)
    assert request.headers["OK-ACCESS-TIMESTAMP"] == FIXED_NOW_ISO
    assert naive.build_search_request(UAH_USDT).params["t"] == str(FIXED_NOW_MS)

    elsewhere = OkxAdapter(
        FakeTransport(),
        now=FixedClock(datetime(2026, 9, 24, 15, 0, 0, 123_000, tzinfo=timezone(timedelta(hours=3)))),
    )
    assert elsewhere.build_list_ads_request(ACCOUNT, UAH_USDT).headers["OK-ACCESS-TIMESTAMP"] == FIXED_NOW_ISO
    assert elsewhere.build_search_request(UAH_USDT).params["t"] == str(FIXED_NOW_MS)


def test_signature_is_reproducible_from_a_fixed_clock_and_a_different_secret_differs() -> None:
    adapter = make_okx()
    request = adapter.build_list_ads_request(ACCOUNT, UAH_USDT)
    repeated = make_okx().build_list_ads_request(ACCOUNT, UAH_USDT)
    other = make_account(
        "okx", 1, {"API_KEY": "okx-key", "SECRET_KEY": "another-secret", "PASSPHRASE": "okx-passphrase"}
    )

    assert request == repeated  # deterministic given (account, now)
    assert adapter.build_list_ads_request(other, UAH_USDT).headers["OK-ACCESS-SIGN"] != request.headers["OK-ACCESS-SIGN"]


# --------------------------------------------------------------------------------------
# private responses
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"adId": "260924231439195"}, "260924231439195"),
        ({"advNo": "260924231439196"}, "260924231439196"),
        ({"id": "260924231439197"}, "260924231439197"),
        ({"adId": "", "advNo": "42"}, "42"),  # a blank id falls through to the next alias
        ({"adId": "  "}, None),
        ({"price": "47.00"}, None),
    ],
)
def test_parse_ad_response_reads_the_id_aliases(record: dict[str, Any], expected: str | None) -> None:
    result = make_okx().parse_ad_response({"code": "0", "data": [record]})

    assert result.adv_no == expected
    assert result.platform == "okx"
    assert result.raw == record


def test_parse_ad_response_echoes_a_price_when_the_venue_reports_one() -> None:
    result = make_okx().parse_ad_response({"code": "0", "data": [{"adId": "1", "price": "47.05"}]})

    assert result.price == Decimal("47.05")


@pytest.mark.parametrize(
    "payload",
    [{}, {"code": "0"}, {"code": "0", "msg": ""}, {"code": "0", "data": []}, "nope"],
)
def test_parse_ad_response_without_a_record_yields_placeholders(payload: Any) -> None:
    result = make_okx().parse_ad_response(payload)

    assert result.adv_no is None
    assert result.price is None
    assert result.pair == Pair.parse("USD/USDT")
    assert result.account_id == ""


def test_parse_ad_response_keeps_a_single_record_object_directly() -> None:
    result = make_okx().parse_ad_response({"adId": "7", "price": "47.10"})

    assert result.adv_no == "7"
    assert result.raw == {"adId": "7", "price": "47.10"}


def test_parse_ad_result_bridges_the_response_into_the_identity_fields() -> None:
    adapter = make_okx()

    result = adapter.parse_ad_result(
        {"code": "0", "data": [{"adId": "260924231439195"}]},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.00"),
    )

    assert result.platform == "okx"
    assert result.account_id == "Okx#1"
    assert result.pair is UAH_USDT
    assert result.adv_no == "260924231439195"
    assert result.price == Decimal("47.00")  # no price in the payload -> the spec's price
    assert result.raw == {"adId": "260924231439195"}


def test_parse_ad_result_uses_the_addressed_adv_no_and_the_echoed_price() -> None:
    adapter = make_okx()

    result = adapter.parse_ad_result(
        {"code": "0", "data": [{"adId": "9", "price": "47.99"}]},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.00"),
    )

    assert result.adv_no == "9"
    assert result.price == Decimal("47.99")

    fallback = adapter.parse_ad_result(
        {"code": "0"},
        account=ACCOUNT,
        pair=UAH_USDT,
        spec=make_spec(price="47.00"),
        adv_no="777",
    )
    assert fallback.adv_no == "777"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"code": "0", "data": [{"adId": "1"}]}, ({"adId": "1"},)),
        ({"code": "0", "list": [{"adId": "1"}]}, ({"adId": "1"},)),
        ({"code": "0", "ads": [{"adId": "1"}]}, ({"adId": "1"},)),
        ({"code": "0", "data": [{"adId": "1"}, "junk", 5]}, ({"adId": "1"},)),
        ({"code": "0", "data": {"adId": "1"}}, ()),
        ({"code": "0"}, ()),
        (None, ()),
    ],
)
def test_parse_ad_list_reads_the_envelope_rows(payload: Any, expected: tuple[Any, ...]) -> None:
    assert make_okx().parse_ad_list(payload) == expected


def test_list_my_ads_returns_the_venue_records() -> None:
    adapter = make_okx(json_response({"code": "0", "data": [{"adId": "1", "price": "47.00"}]}))

    records = adapter.list_my_ads(ACCOUNT, UAH_USDT)

    assert records == ({"adId": "1", "price": "47.00"},)
    assert adapter.transport.last.url == f"{BASE_URL}{LIST_ADS_PATH}"


def test_send_private_surfaces_a_v5_error_payload() -> None:
    adapter = make_okx(
        json_response({"code": "51000", "msg": "Parameter adId error"}, status=200)
    )

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert str(excinfo.value) == "okx API error (code='51000', error_code='0'): Parameter adId error"
    assert excinfo.value.payload == {"code": "51000", "msg": "Parameter adId error"}


def test_send_private_surfaces_an_http_401_before_parsing_the_envelope() -> None:
    adapter = make_okx(text_response("Unauthorized", status=401))

    with pytest.raises(ApiError) as excinfo:
        adapter.send_private(ACCOUNT, adapter.build_list_ads_request(ACCOUNT, UAH_USDT))

    assert excinfo.value.status == 401
    assert "okx request for account Okx#1 failed with HTTP 401" in str(excinfo.value)


# -- every own advertisement (wire names UNCONFIRMED, read through AD_BODY_FIELDS) --------
def _own_row(ad_id: str, **fields: Any) -> dict[str, Any]:
    row = {
        AD_BODY_FIELDS["adv_no"]: ad_id,
        AD_BODY_FIELDS["crypto"]: "USDT",
        AD_BODY_FIELDS["fiat"]: "UAH",
        AD_BODY_FIELDS["side"]: "sell",
        AD_BODY_FIELDS["price"]: "47.00",
        AD_BODY_FIELDS["min_amount"]: "1000",
        AD_BODY_FIELDS["max_amount"]: "200000",
        AD_BODY_FIELDS["quantity"]: "300",
        AD_BODY_FIELDS["payment_methods"]: ["Monobank", " ", 7],
        AD_BODY_FIELDS["status"]: AD_STATUS_VALUES[True],
    }
    row.update(fields)
    return row


def test_own_ads_request_carries_only_pagination_and_is_signed() -> None:
    request = make_okx().build_own_ads_request(ACCOUNT, page=2)

    assert request.url == f"{BASE_URL}{LIST_ADS_PATH}"
    assert request.json_body == {
        OWN_ADS_FIELDS["current_page"]: 2,
        OWN_ADS_FIELDS["number_per_page"]: 100,
    }
    assert_okx_signature(request)


def test_fetch_own_ads_normalizes_active_and_inactive_rows() -> None:
    offline = _own_row(
        "2",
        **{
            AD_BODY_FIELDS["status"]: AD_STATUS_VALUES[False].upper(),
            AD_BODY_FIELDS["side"]: "BUY",
            AD_BODY_FIELDS["fiat"]: "PLN",
        },
    )
    adapter = make_okx(
        json_response(
            {
                "code": "0",
                "data": [
                    _own_row("1"),
                    offline,
                    _own_row("3", **{AD_BODY_FIELDS["status"]: "??", AD_BODY_FIELDS["side"]: "x"}),
                    _own_row(""),
                    _own_row("4", **{AD_BODY_FIELDS["crypto"]: None}),
                ],
            }
        ),
        json_response({"code": "0", "data": []}),
    )

    ads = adapter.fetch_own_ads(ACCOUNT)

    assert [(ad.adv_no, ad.pair.symbol, ad.side, ad.status) for ad in ads] == [
        ("1", "UAH/USDT", "sell", "online"),
        ("2", "PLN/USDT", "buy", "offline"),
        ("3", "UAH/USDT", "", "unknown"),
    ]
    first = ads[0]
    assert first.account_id == "Okx#1"
    assert first.price == Decimal("47.00")
    assert first.quantity == Decimal("300")
    assert (first.min_amount, first.max_amount) == (Decimal("1000"), Decimal("200000"))
    assert first.payment_methods == ("Monobank",)


def test_own_ad_reports_its_amount_and_methods_for_an_update() -> None:
    ad = make_okx().parse_own_ad(_own_row("1"), ACCOUNT)

    assert ad.total_quantity == Decimal("300")
    assert ad.payment_ids == ("Monobank",)
    assert ad.price_floating_ratio is None


def test_update_ad_request_refuses_a_floating_ratio() -> None:
    with pytest.raises(ConfigError, match="okx: floating-price updates are not supported"):
        make_okx().build_update_ad_request(ACCOUNT, make_spec(price_floating_ratio="91"), "1")
